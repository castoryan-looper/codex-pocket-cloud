"""Authenticated Codex app-server/WebRTC voice and web research bridge for Voxi.

Uses the official CLI's existing CODEX_HOME login. Does not read/export auth.json,
run user shell commands or reuse Pocket's task conversation. Native realtime
delegations run on the dedicated, ephemeral voice thread and return automatically.
The /voxi endpoint is Docker-internal only, not added to the public Caddy routes.
"""
import asyncio
import contextlib
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import time
from collections import deque

from aiohttp import web, WSMsgType
from voxi_config import VoiceSettings, effective_prompt, VOICE_TASK_INSTRUCTIONS
from voxi_memory import MemoryStore, device_id
from voice_recovery import offer_audio_nack, configure_audio_recovery
from voxi_voices import validate_voice
LOG = logging.getLogger('pocket.voxi')


def diagnostic_reason(value):
    """Retain error categories, never arbitrary RPC text, SDP or credentials."""
    text = str(value).lower()
    labels = []
    groups = {
        'authentication': ('unauthorized', 'authentication', 'login', '401'),
        'access-denied': ('forbidden', 'permission', 'entitlement', '403'),
        'rate-limit': ('rate limit', 'quota', '429'),
        'invalid-parameters': ('invalid param', 'invalid request', '-32602'),
        'unsupported': ('unsupported', 'not supported', 'unknown variant', 'unknown field'),
        'voice': ('voice',), 'version': ('version',), 'transport': ('transport',),
        'timeout': ('timed out', 'timeout'),
        'network': ('connection', 'connect error', 'dns', 'tls', 'certificate', 'network'),
        'service-error': ('500', '502', '503', '504'),
        'sdp': ('sdp',), 'protocol-header': ('quicksilver', 'alpha_header'), 'feature-disabled': ('not enabled', 'feature', 'disabled'),
    }
    for label, matches in groups.items():
        if any(word in text for word in matches): labels.append(label)
    statuses = sorted(set(re.findall(r'(?<![0-9])(?:400|401|403|404|408|429|500|502|503|504)(?![0-9])', text)))
    return ','.join(labels + ['http-'+code for code in statuses]) or 'unclassified'


class VoiceFailure(RuntimeError):
    def __init__(self, source, code, detail):
        self.source = source if source in ('rpc', 'notification') else 'runtime'
        self.code = code if isinstance(code, int) else None
        self.reason = diagnostic_reason(detail)
        super().__init__(f'{self.source} code={self.code} reason={self.reason}')


def frameless_session_update(voice='cove'):
    # Exact Frameless Bidi session shape used by Codex 0.163.0.
    # PTT is enforced locally by withholding all input until release.
    return {'type': 'session.update', 'session': {
        'audio': {'output': {'voice': validate_voice(voice)}}, 'delegation': {'type': 'client'}}}


class VoiceRpc:
    def __init__(self, binary, notify):
        self.binary, self.notify = binary, notify
        self.process = None
        self.pending = {}
        self.counter = 0
        self.tasks = []

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(
            self.binary, 'app-server', '--enable', 'realtime_conversation', '--listen', 'stdio://',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=8*1024*1024)
        self.tasks = [asyncio.create_task(self.read()), asyncio.create_task(self.errors())]
        await self.call('initialize', {'clientInfo': {'name': 'voxi_voice', 'version': '1'},
                                     'capabilities': {'experimentalApi': True}})
        await self.send({'method': 'initialized', 'params': {}})
        account = await self.call('account/read', {'refreshToken': False})
        if not account.get('account'):
            raise RuntimeError('Existing Codex login is unavailable')

    async def send(self, packet):
        self.process.stdin.write((json.dumps(packet)+'\n').encode())
        await self.process.stdin.drain()

    async def call(self, method, params):
        self.counter += 1
        key = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        try:
            await self.send({'id': key, 'method': method, 'params': params})
            return await asyncio.wait_for(future, 40)
        finally:
            self.pending.pop(key, None)

    async def read(self):
        try:
            while raw := await self.process.stdout.readline():
                try:
                    packet = json.loads(raw)
                except ValueError:
                    continue
                if 'id' in packet and 'method' not in packet:
                    future = self.pending.get(packet['id'])
                    if future and not future.done():
                        if 'error' in packet:
                            error = packet.get('error') or {}
                            future.set_exception(VoiceFailure('rpc', error.get('code'), error.get('message', '')))
                        else:
                            future.set_result(packet.get('result', {}))
                elif 'id' in packet:
                    # No tools/approvals are delegated by this voice-only client.
                    await self.send({'id': packet['id'], 'error': {'code': -32601, 'message': 'Voice-only client'}})
                else:
                    await self.notify(packet)
        finally:
            for future in list(self.pending.values()):
                if not future.done(): future.set_exception(RuntimeError('Codex app-server disconnected'))

    async def errors(self):
        while await self.process.stderr.readline():
            pass  # Do not relay account details or arbitrary model/runtime logs.

    async def close(self):
        if self.process and self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError): self.process.terminate()
            try: await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        for task in self.tasks: task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


def create_input_track():
    from aiortc import AudioStreamTrack
    from av import AudioFrame
    from fractions import Fraction
    class InputTrack(AudioStreamTrack):
        def __init__(self):
            super().__init__()
            self.payload=b'';self.offset=0;self.samples=0;self.clock=None;self.emitted_bytes=0
        def submit(self, data):
            if len(data)>30*48000+38400 or len(data)%2:raise ValueError('Invalid/overlong RTC audio')
            if self.offset<len(self.payload):raise RuntimeError('Previous RTC upload is still active')
            self.payload=bytes(data);self.offset=0
        async def recv(self):
            loop=asyncio.get_running_loop()
            if self.clock is None:self.clock=loop.time()
            await asyncio.sleep(max(0,self.clock+self.samples/24000-loop.time()))
            block=self.payload[self.offset:self.offset+960]
            self.offset+=len(block);self.emitted_bytes+=len(block)
            if self.offset>=len(self.payload):self.payload=b'';self.offset=0
            frame=AudioFrame(format='s16',layout='mono',samples=480)
            frame.planes[0].update(block.ljust(960,b'\0'))
            frame.sample_rate=24000;frame.pts=self.samples;frame.time_base=Fraction(1,24000)
            self.samples+=480
            return frame
    return InputTrack()


class VoiceSession:
    def __init__(self, ws, binary):
        self.ws, self.binary = ws, binary
        self.memory_store = None
        self.memory_user = None
        self.memory_device = None
        self.memory_capture = None
        self.settings_source = None
        self.settings = VoiceSettings().snapshot()
        self.session_id = secrets.token_hex(4)
        self.rpc = self.peer = self.events = self.workspace = self.input_track = None
        self.answer = None
        self.ready = self.recording = self.allow_output = self.outputting = False
        self.bytes = self.played = 0
        self.pending_audio = bytearray()
        self.thread = self.item = None
        self.channel = 0
        self.tasks = set()
        self.lookback = deque(maxlen=2)
        self.generation_done = False
        self.last_voice = time.monotonic()
        self.turn = 0
        self.response_id = None
        self.phase = 'new'
        self.release_at=None
        self.emitted_baseline=0
        self.last_output_at = None
        self.media_last_end = None
        self.media_gap_max_ms = 0
        self.media_last_receive = None
        self.media_receive_gap_max_ms = 0
        self.media_pcm_bytes = 0
        self.media_report_at = 0
        self.audio_nack = False
        self.research_pending = False
        self.research_turn = None
        self.continuation_until = 0
        self.research_watchdog = None
        self.research_result_waiting = False
        self.audio_segment = 0
        self.agent_radio = None
        self.agent_request_id = None
        self.voice_override=None
        self.voice='cove'
        self.preview_mode=False
        self.preview_text=''
        self.preview_language=''
        self.preview_error_reason=''

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def send(self, packet):
        if packet.get('type') == 'ready' and self.agent_radio:
            packet = {**packet, **self.agent_radio.channel_info(self)}
        if not self.ws.closed: await self.ws.send_json(packet)

    async def say_agent(self, text):
        """Use the existing native speech transport; never feed task text to its tool loop."""
        if self.ws.closed or not self.rpc:return
        self.ready=False;self.allow_output=True;self.generation_done=False
        self.release_at=time.monotonic();self.last_output_at=None
        await self.send({'type':'busy'})
        try:
            if self.preview_mode:
                # A fixed script, no [BACKEND] wrapper or mixed-language context.
                if not self.events or self.events.readyState!='open':raise RuntimeError('Preview data channel unavailable')
                self.event({'type':'session.context.append','channel':'speakable','content':[{'type':'input_text','text':text}]})
            else:
                await asyncio.wait_for(self.rpc.call('thread/realtime/appendSpeech',{'threadId':self.thread,'text':text[:1200]}),15)
        except Exception:
            self.allow_output=False;self.ready=False
            await self.send({'type':'error','message':'Task state is saved; native speech failed. Reconnect and check task status.'})
            await self.ws.close()
            raise
        self.spawn(self.watch_response(self.turn))

    def event(self, packet):
        if self.events and self.events.readyState == 'open':
            self.events.send(json.dumps(packet))

    async def notification(self, packet):
        method, params = packet.get('method'), packet.get('params', {})
        if params.get('threadId') != self.thread: return
        if method == 'thread/realtime/sdp' and self.answer and not self.answer.done():
            self.answer.set_result(params['sdp'])
        elif method == 'thread/realtime/itemAdded' and params.get('item', {}).get('type') == 'handoff_request':
            await self.begin_research()
        elif method == 'turn/started':
            await self.begin_research()
            self.research_turn = params['turn']['id']
        elif method == 'turn/completed' and params.get('turn', {}).get('id') == self.research_turn:
            self.research_turn = None
            self.research_pending = False
            self.research_result_waiting = not self.outputting
            # Native audio can finish before the backend turn. Preserve its
            # completion marker so trailing RTP silence can end playback.
            LOG.info('Native voice research completed sessionId=%s turn=%s speechDone=%s outputting=%s',
                     self.session_id, self.turn, self.generation_done, self.outputting)
            self.last_output_at = time.monotonic()
            if params['turn'].get('status') != 'completed':
                # Deliver a speakable failure through the existing native voice transport.
                self.spawn(self.rpc.call('thread/realtime/appendSpeech', {
                    'threadId': self.thread, 'text': '这次查询没有完成，请稍后重试。'}))
        elif method in ('thread/realtime/error', 'thread/realtime/closed'):
            if self.preview_mode:self.preview_error_reason=diagnostic_reason(params.get('error',params.get('message',params.get('reason',''))))
            LOG.warning('Native voice upstream %s reason=%s',method,diagnostic_reason(params.get('error',params.get('message',params.get('reason','')))))
            if self.answer and not self.answer.done():
                self.answer.set_exception(VoiceFailure('notification', params.get('code'), params.get('error', params.get('message', params.get('reason', '')))))
            else:
                await self.send({'type': 'error', 'message': 'Codex native voice session closed; check account support'})
                await self.ws.close()

    async def start(self, channel=0):
        from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration
        await self.stop()
        self.channel = channel
        if self.settings_source: self.settings = self.settings_source.snapshot()
        personal=self.agent_radio.agent.store.preferences(self.memory_user).get('voice') if self.agent_radio and self.agent_radio.agent and self.memory_user else None
        self.voice=validate_voice(self.voice_override or personal or self.settings['voice'])
        await self.send({'type': 'connecting', 'channel': channel})
        self.workspace = tempfile.TemporaryDirectory(prefix='voxi-chat-')
        self.rpc = VoiceRpc(self.binary, self.notification)
        self.phase = 'account'
        await self.rpc.start()
        self.phase = 'thread'
        self.thread = (await self.rpc.call('thread/start', {
            'cwd': self.workspace.name, 'approvalPolicy': 'never', 'sandbox': 'read-only',
            'ephemeral': True, 'developerInstructions': '只朗读提供的试听句子，不调用工具，不执行任务。' if self.preview_mode else VOICE_TASK_INSTRUCTIONS,
            'config': {'web_search': 'disabled' if self.preview_mode else 'live', 'features.shell_tool': False}
        }))['thread']['id']
        self.peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.input_track=create_input_track()
        self.peer.addTrack(self.input_track)
        self.events = self.peer.createDataChannel('oai-events')
        @self.events.on('open')
        def opened():
            # Only configure fields defined by the Frameless Bidi protocol.
            # Audio is buffered until PTT release; do not disable server VAD.
            update=frameless_session_update(self.voice)
            if self.preview_mode:update['session']['instructions']=self.preview_instructions()
            self.event(update)
        @self.events.on('message')
        def message(raw):
            try: packet = json.loads(raw)
            except (ValueError, TypeError): return
            self.spawn(self.on_event(packet))
        @self.peer.on('track')
        def track_received(track):
            if track.kind == 'audio': self.spawn(self.receive_audio(track))
        @self.peer.on('connectionstatechange')
        async def state_changed():
            if self.peer and self.peer.connectionState == 'failed':
                await self.send({'type': 'error', 'message': 'WebRTC connection failed; check server UDP egress'})
                await self.ws.close()
        offer = await self.peer.createOffer()
        await self.peer.setLocalDescription(offer_audio_nack(offer) if self.settings['audioRetransmission'] else offer)
        self.answer = asyncio.get_running_loop().create_future()
        self.phase = 'webrtc-sdp'
        prompt = effective_prompt(self.settings, channel)
        route=self.agent_radio.channel_info(self) if self.agent_radio else {}
        if route:
            if route['channelMode']=='fast':
                prompt=self.settings['systemPrompt']+'\n当前频道：'+route['name']+'。这是快问频道：直接回答，必要时只读查询。需要持久执行或修改数据时，请用户切换 Agent 频道。'
                if self.memory_user and self.agent_radio.agent:
                    preference=self.agent_radio.agent.store.preferences(self.memory_user)['fastPrompt']
                    if preference:prompt+='\n当前用户的回答偏好：'+preference
            else:
                prompt='你是 Voxi 的任务语音播报入口。按提供的播报文本简洁朗读，不添加任务执行过程，不自行调用工具或声称完成其他工作。'
        if self.memory_store and self.memory_user:
            try:prompt += self.memory_store.context(self.memory_user)
            except Exception:LOG.warning('Voxi memory context unavailable')
        if self.preview_mode:prompt=self.preview_instructions()
        await self.rpc.call('thread/realtime/start', {'threadId': self.thread,
            'transport': {'type': 'webrtc', 'sdp': self.peer.localDescription.sdp},
            # Codex v3 maps to the AVAS-required quicksilver=v2 alpha header.
            'outputModality': 'audio', 'version': 'v3', 'voice': self.voice, 'prompt': prompt,
            'includeStartupContext': False, 'clientManagedHandoffs': False})
        answer = await asyncio.wait_for(self.answer, 40)
        await self.peer.setRemoteDescription(RTCSessionDescription(sdp=answer, type='answer'))
        self.audio_nack = configure_audio_recovery(self.peer, self.settings['audioRecoveryPackets']) if self.settings['audioRetransmission'] else False
        LOG.info('Native voice audio recovery sessionId=%s nackNegotiated=%s', self.session_id, self.audio_nack)
        self.phase = 'session-configuration'
        # Do not announce READY until the backend accepts the Frameless Bidi session configuration.
        for _ in range(200):
            if self.ready:
                self.phase = 'ready'
                return
            if self.ws.closed: raise RuntimeError('Voice setup failed')
            await asyncio.sleep(.1)
        raise RuntimeError('Codex voice setup timed out; session configuration was not acknowledged')

    async def on_event(self, event):
        kind = event.get('type')
        if kind in ('session.updated', 'session.started'):
            # An ACK to our supported update confirms an established session.
            # No microphone input is submitted before PTT release.
            self.ready = True
            await self.send({'type': 'ready', 'channel': self.channel})
        elif kind == 'turn.done' and event.get('turn', {}).get('role') == 'assistant':
            self.generation_done = True
        elif kind == 'response.created':
            response = event.get('response', {})
            if response.get('metadata', {}).get('turn') == str(self.turn): self.response_id = response.get('id')
        elif kind == 'response.output_item.added' and event.get('response_id') == self.response_id:
            self.item = event.get('item', {}).get('id')
        elif kind == 'delegation.created':
            item = event.get('item', {})
            if item.get('type') == 'delegation' and item.get('target') == 'client':
                await self.begin_research()
        elif kind == 'output_audio_buffer.started' and not self.recording and (
                self.allow_output or time.monotonic() < self.continuation_until):
            self.allow_output = True
            self.ready = False
            self.generation_done = False
            await self.begin_audio()
        elif kind == 'output_audio_buffer.stopped':
            # Separate RTC media/control channels may deliver the final packet later.
            turn = self.turn
            segment = self.audio_segment
            await asyncio.sleep(.25)
            if turn == self.turn and segment == self.audio_segment and self.outputting:
                await self.end_audio()
        elif kind == 'response.done' and event.get('response', {}).get('id') == self.response_id:
            if event.get('response', {}).get('status') in ('failed', 'incomplete'):
                await self.send({'type': 'error', 'message': 'Codex native voice response failed or was incomplete'})
                await self.ws.close()
                return
            self.generation_done = True
        elif kind == 'error':
            if event.get('error', {}).get('code') in ('response_cancel_not_active', 'input_audio_buffer_commit_empty'): return
            if self.preview_mode:self.preview_error_reason=diagnostic_reason(event.get('error',{}))
            await self.send({'type': 'error', 'message': 'Codex voice rejected an audio/control event'})
            await self.ws.close()

    def preview_instructions(self):
        language='普通话中文' if self.preview_language=='zh-CN' else 'English' if self.preview_language=='en' else 'the script language'
        return ('You are a script reader, not a conversational assistant. Read ONLY the exact script below once, '
                'verbatim, in '+language+'. Do not reply to it, translate, paraphrase, introduce yourself, '
                'add words, or call tools. Keep a natural neutral pace. Stop after the last word.\n'
                '<script>'+self.preview_text+'</script>')

    async def begin_research(self):
        if self.preview_mode:
            await self.send({'type':'error','message':'Voice preview cannot execute research or tasks'})
            await self.ws.close()
            return
        if self.recording or self.ws.closed:
            return
        self.research_pending = True
        self.ready = False
        self.allow_output = True
        self.generation_done = False
        if not self.research_watchdog or self.research_watchdog.done():
            self.continuation_until = time.monotonic() + 180
            self.research_watchdog = self.spawn(self.watch_research(self.turn))
        await self.send({'type': 'thinking'})

    async def watch_research(self, turn):
        await asyncio.sleep(max(0, self.continuation_until - time.monotonic()))
        if turn != self.turn or self.ws.closed:
            return
        if self.research_pending or self.research_result_waiting:
            LOG.warning('Native voice timeout sessionId=%s turn=%s phase=research pending=%s waitingAudio=%s',
                        self.session_id, self.turn, self.research_pending, self.research_result_waiting)
            await self.send({'type': 'error', 'message': '查询或结果播报超时，请重试'})
            await self.ws.close()

    async def end_audio(self):
        self.outputting = False
        self.allow_output = self.research_pending
        self.generation_done = False
        await self.send({'type': 'audio_end'})
        if self.research_pending:
            await self.send({'type': 'thinking'})
        else:
            self.research_result_waiting = False
            self.ready = True
            await self.send({'type': 'ready', 'channel': self.channel})

    async def begin_audio(self):
        if self.outputting: return
        self.audio_segment += 1
        self.research_result_waiting = False
        self.outputting = True
        self.last_output_at = time.monotonic()
        if self.release_at is not None and time.monotonic() < self.continuation_until:
            self.spawn(self.watch_response(self.turn))
        self.played = 0
        if self.release_at is not None:
            metrics={'type':'timing','sessionId':self.session_id,'recordedMs':self.bytes//48,
                     'releaseToAudioStartMs':round((time.monotonic()-self.release_at)*1000),
                     'rtpEmittedMs':max(0,getattr(self.input_track,'emitted_bytes',0)-self.emitted_baseline)//48,
                     'rtpQueuedMs':self.bytes//48+800}
            LOG.info('Native voice timing %s',json.dumps(metrics))
            await self.send(metrics)
        await self.send({'type': 'audio_start'})

    async def report_media_stats(self):
        if not self.peer: return
        try:
            stats = await self.peer.getStats()
            rows = [value for value in stats.values() if value.type == 'inbound-rtp' and getattr(value, 'kind', 'audio') == 'audio']
            LOG.info('Native voice media stats %s', json.dumps({
                'sessionId': self.session_id, 'turn': self.turn, 'audioNack': self.audio_nack, 'packetsReceived': sum(getattr(row, 'packetsReceived', 0) for row in rows),
                'packetsLost': sum(getattr(row, 'packetsLost', 0) for row in rows),
                'maxFrameTimelineGapMs': self.media_gap_max_ms,
                'maxReceiveGapMs': self.media_receive_gap_max_ms, 'pcmBytes': self.media_pcm_bytes}))
        except Exception as error:
            LOG.info('Native voice media stats unavailable (%s)', type(error).__name__)

    async def receive_audio(self, track):
        from av import AudioResampler
        resampler = AudioResampler(format='s16', layout='mono', rate=24000)
        try:
            while True:
                frame = await track.recv()
                now = time.monotonic()
                if self.allow_output and not self.recording:
                    if self.media_last_receive is not None:
                        self.media_receive_gap_max_ms = max(self.media_receive_gap_max_ms, round((now-self.media_last_receive)*1000))
                    self.media_last_receive = now
                    if frame.pts is not None and frame.time_base is not None:
                        start = float(frame.pts*frame.time_base)
                        if self.media_last_end is not None:
                            self.media_gap_max_ms = max(self.media_gap_max_ms, round((start-self.media_last_end)*1000))
                        self.media_last_end = start+frame.samples/frame.sample_rate
                    if now-self.media_report_at >= self.settings['mediaStatsIntervalSeconds']:
                        self.media_report_at = now
                        self.spawn(self.report_media_stats())
                for mono in resampler.resample(frame):
                    data = bytes(mono.planes[0])[:mono.samples*2]
                    # Fall back to a small PCM onset detector when the transport
                    # omits output_audio_buffer.started. Never play idle RTP silence.
                    voiced = any(abs(int.from_bytes(data[i:i+2], 'little', signed=True)) > 32 for i in range(0,len(data),2))
                    if voiced: self.last_voice = time.monotonic()
                    if (self.allow_output or time.monotonic() < self.continuation_until) and not self.recording:
                        if voiced: self.last_output_at = time.monotonic()
                        if not self.outputting and voiced:
                            self.allow_output = True
                            self.ready = False
                            await self.begin_audio()
                            for prior in self.lookback: await self.ws.send_bytes(prior)
                        if self.outputting:
                            self.media_pcm_bytes += len(data)
                            for i in range(0,len(data),960): await self.ws.send_bytes(data[i:i+960])
                            if self.generation_done and time.monotonic()-self.last_voice > self.settings['replyEndSilenceMs']/1000:
                                await self.end_audio()
                    self.lookback.append(data)
        except asyncio.CancelledError: raise
        except Exception as error:
            # Do not log arbitrary exception text, URLs or credentials.
            state = self.peer.connectionState if self.peer else 'none'
            LOG.warning('Native voice audio receive failed (%s) peer=%s outputting=%s generation_done=%s',
                        type(error).__name__, state, self.outputting, self.generation_done)
            if not self.ws.closed:
                await self.send({'type': 'error', 'message': f'WebRTC audio receive failed ({type(error).__name__}; peer={state})'})
                await self.ws.close()

    async def control(self, event):
        kind = event.get('type')
        if kind == 'played': self.played = max(0,min(60000,int(event.get('ms',0))))
        elif kind == 'channel':
            channel = event.get('channel')
            if type(channel) is int and 0 <= channel < 4: await self.start(channel)
        elif kind == 'ptt_down' and self.ready and not self.recording and not self.allow_output:
            self.continuation_until = 0
            self.research_result_waiting = False
            if self.research_watchdog:
                self.research_watchdog.cancel()
                self.research_watchdog = None
            self.turn += 1
            request_id=event.get('requestId')
            self.agent_request_id=request_id if isinstance(request_id,str) and 1<=len(request_id)<=128 else self.session_id+':'+str(self.turn)
            self.response_id = None
            self.recording = True
            self.allow_output = self.outputting = False
            self.bytes = 0
            self.release_at=None
            self.last_output_at = None
            self.media_last_end = self.media_last_receive = None
            self.media_gap_max_ms = self.media_receive_gap_max_ms = self.media_pcm_bytes = 0
            self.media_report_at = 0
            self.lookback.clear()
            self.pending_audio.clear()
            self.item = None
            await self.send({'type': 'flush'})
            await self.send({'type': 'recording'})
        elif kind == 'ptt_up' and self.recording:
            self.recording = False
            if self.bytes < 4800:
                self.pending_audio.clear()
                await self.send({'type': 'idle', 'reason': 'too_short'})
                return
            if self.agent_radio and self.agent_radio.channel_info(self).get('channelMode') in ('agent','openclaw','disabled'):
                self.ready=False
                await self.send({'type':'thinking'})
                pcm=bytes(self.pending_audio);self.pending_audio.clear()
                self.spawn(self.agent_radio.agent_voice(self,pcm))
                return
            self.allow_output = True
            self.generation_done = False
            self.lookback.clear()
            self.ready = False
            await self.send({'type': 'busy'})
            # AVAS WebRTC accepts microphone media over RTP, never audio
            # append JSON on its data channel. Release queues real-time media.
            self.release_at=time.monotonic()
            self.emitted_baseline=getattr(self.input_track,'emitted_bytes',0)
            if self.memory_capture: self.memory_capture(self, bytes(self.pending_audio))
            self.input_track.submit(bytes(self.pending_audio)+bytes(38400))
            self.pending_audio.clear()
            await self.send({'type': 'thinking'})
            turn = self.turn
            self.spawn(self.watch_response(turn))

    async def watch_response(self, turn):
        # Bound stalls, not total story length. Idle RTP silence is not progress.
        stall = self.settings['responseIdleTimeoutSeconds']
        first_deadline = self.release_at + self.bytes / 48000 + stall
        while turn == self.turn and self.allow_output and not self.ws.closed:
            if self.research_pending:
                await asyncio.sleep(1)
                continue
            deadline = first_deadline if self.last_output_at is None else self.last_output_at + stall
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                LOG.warning('Native voice timeout sessionId=%s turn=%s phase=response_idle seconds=%s',
                            self.session_id, self.turn, stall)
                await self.send({'type': 'error', 'message': f'Codex voice response stalled for {stall} seconds'})
                await self.ws.close()
                return
            await asyncio.sleep(remaining)

    async def pcm(self, data):
        if not self.ready or not self.recording: return
        self.bytes += len(data)
        if len(data)%2 or self.bytes > 30*48000: raise ValueError('Invalid/overlong PCM')
        self.pending_audio.extend(data)


    async def stop(self):
        self.ready = self.recording = self.allow_output = self.outputting = False
        # Ignore closure notifications from the old peer while changing channel.
        self.thread = self.item = self.response_id = None
        self.lookback.clear()
        self.pending_audio.clear()
        self.generation_done = False
        self.research_pending = False
        self.research_turn = None
        self.continuation_until = 0
        self.research_watchdog = None
        self.research_result_waiting = False
        for task in self.tasks: task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        if self.peer:
            peer, self.peer = self.peer, None
            await peer.close()
        if self.input_track:
            self.input_track.stop();self.input_track=None
        if self.rpc:
            rpc, self.rpc = self.rpc, None
            await rpc.close()
        if self.workspace:
            self.workspace.cleanup()
            self.workspace = None


class VoxiRadio:
    def __init__(self, owner, binary):
        self.owner, self.binary, self.active = owner, binary, set()
        self.max_sessions = max(1,min(4,int(os.environ.get("VOXI_MAX_SESSIONS","2"))))
        self.retry_after = 0
        self.settings = VoiceSettings(getattr(owner, 'runtime', None))
        try:self.memory = MemoryStore(getattr(owner, 'runtime', None))
        except Exception:self.memory = None
        self.memory_tasks = set()
        self.memory_lock = asyncio.Lock()
        self.memory_revision = {}
        self.memory_error = '' if self.memory else '记忆存储无法打开'
        self.memory_model = None
        self.agent = None
        from voxi_voice_preview import VoicePreview
        self.voice_preview=VoicePreview(self)
        if getattr(owner,'runtime',None) and getattr(owner,'config',{}).get('cwd'):
            from voxi_agent import AgentHub
            from bridge import Rpc
            self.agent=AgentHub(owner.runtime,owner.config['cwd'],Rpc)
            self.agent.context_provider=lambda uid:self.memory.context(uid) if self.memory else ''
            self.agent.listeners.add(self.agent_event)

    def channel_info(self, session):
        if not self.agent:return {}
        cfg=self.agent.store.settings(session.memory_user)
        if not cfg['enabled']:return {}
        route=cfg['channels'][session.channel]
        latest=self.agent.store.list_tasks(session.memory_user,session.channel,1) if session.memory_user else []
        task=latest[0] if latest else None
        return {'name':route['name'],'channelMode':'agent' if route['mode']=='openclaw' else route['mode'],
                'agentProvider':'openclaw' if route['mode']=='openclaw' else 'codex','agentTasks':True,
                'taskId':task['id'] if task else '', 'taskState':task['state'] if task else ''}

    async def agent_event(self, task):
        for session in tuple(self.active):
            if session.memory_user==task['user_id'] and session.channel==task['channel'] and not session.ws.closed:
                await session.send({'type':'task_state','taskId':task['id'],'state':task['state'],'summary':task['summary'][:120]})

    async def agent_voice(self, session, pcm):
        try:
            if self.channel_info(session).get('channelMode')=='disabled':
                await session.say_agent('这个频道尚未启用，请切换到其他频道。');return
            uid=session.memory_user
            if not uid or not self.memory or self.memory.owner(session.memory_device)!=uid:
                await session.say_agent('设备还没有绑定用户，请先在管理后台完成绑定。');return
            async with self.memory_lock:
                text=await asyncio.to_thread(self.transcribe_memory,pcm)
            if not text:
                await session.say_agent('没有听清任务，请再说一次。');return
            if self.memory.owner(session.memory_device)!=uid:return
            # ASR work can finish after a channel change. Never submit its old input.
            if not self.agent.store.settings(uid)['enabled']:raise ValueError('Agent 未启用')
            self.memory.record(uid,session.memory_device,session.agent_request_id,text)
            normalized=text.strip().rstrip('。！？!?')
            tasks=self.agent.store.list_tasks(uid,session.channel,1)
            task=tasks[0] if tasks else None
            if normalized in ('查看进度','查询进度','任务进度','读结果','查看结果','任务结果'):
                if task:
                    await session.send({'type':'task_state','taskId':task['id'],'state':task['state'],'summary':task['summary'][:120]})
                    await session.say_agent(task['summary'][:900])
                else:await session.say_agent('这个频道还没有任务。')
                return
            if normalized in ('取消任务','停止任务'):
                if not task:await session.say_agent('当前没有任务。');return
                result=await self.agent.action(uid,task['id'],'cancel')
                await session.say_agent(result['summary']);return
            if normalized in ('同意执行','批准操作','拒绝操作') and task and task['state']=='awaiting_approval':
                # Scope confirmation to the selected channel's actual pending request.
                await self.agent.action(uid,task['id'],'decline' if normalized=='拒绝操作' else 'approve')
                await session.say_agent('决定已提交。');return
            if normalized.startswith('继续任务') and task:
                result=await self.agent.action(uid,task['id'],'continue',request_id=session.agent_request_id,
                                               text=normalized[4:].strip('，,：: ') or '继续完成此前尚未完成的任务；先核对已有结果，不重复执行已完成的操作。')
                await session.send({'type':'task_state','taskId':result['id'],'state':result['state'],'summary':result['summary'][:120]})
            else:
                result,_=self.agent.submit(uid,session.memory_device,session.channel,session.agent_request_id,text)
                await session.send({'type':'task_state','taskId':result['id'],'state':result['state'],'summary':result['summary'][:120]})
            await session.say_agent('任务已保存，我会在后台处理。可以说查看进度、读结果，或者取消任务。')
        except asyncio.CancelledError:raise
        except Exception:
            await session.say_agent('本次操作未能确认，请在管理后台检查任务状态后再重试。')

    async def agent_configuration(self, request):
        if request.headers.get('Origin') or not hmac.compare_digest(request.headers.get('Authorization',''),'Bearer '+self.owner.config['device_token']):raise web.HTTPForbidden()
        if not self.agent:raise web.HTTPServiceUnavailable(text='Agent storage unavailable')
        if request.method=='GET':
            return web.json_response(self.agent.snapshot(self.memory),headers={'Cache-Control':'no-store'})
        try:
            if request.content_length and request.content_length>262144:raise ValueError('请求过大')
            raw=await request.read()
            if len(raw)>262144:raise ValueError('请求过大')
            body=json.loads(raw)
            if not isinstance(body,dict):raise ValueError('无效请求')
            action=body.get('action')
            if action=='relay':
                try:result=await self.agent.openclaw.relay(body)
                except ValueError:return web.json_response({'error':'连接器请求被拒绝'},status=400)
                return web.json_response(result,headers={'Cache-Control':'no-store'})
            if action=='create_profile':
                return web.json_response({'profile':self.memory.create_user(body.get('name'))},headers={'Cache-Control':'no-store'})
            if action=='settings':
                self.agent.openclaw.check_routes((body.get('settings') or {}).get('channels'))
                self.agent.store.save_settings(body.get('settings'))
            elif action=='credential':self.agent.store.save_secret(body.get('provider'),body.get('key'))
            else:
                uid=body.get('userId')
                if not isinstance(uid,str) or not self.memory or not self.memory.user(uid):raise ValueError('用户不存在')
                if action=='channels':
                    self.agent.openclaw.check_routes(body.get('channels'),uid)
                    self.agent.store.save_settings(body.get('channels'),uid)
                elif action=='reset_channels':self.agent.store.reset_channels(uid)
                elif action=='connect_openclaw':
                    result=self.agent.openclaw.create(uid,body.get('name'),body.get('transport'),body.get('url',''),body.get('agent','main'),body.get('token',''))
                    return web.json_response(result,headers={'Cache-Control':'no-store'})
                elif action=='test_openclaw':
                    return web.json_response(await self.agent.openclaw.probe(uid,body.get('connectionId')),headers={'Cache-Control':'no-store'})
                elif action=='delete_openclaw':self.agent.openclaw.delete(uid,body.get('connectionId'))
                elif action=='detail':
                    task=self.agent.store.task(body.get('taskId'),uid)
                    if not task:raise ValueError('任务不存在')
                    return web.json_response({'task':self.agent.store.public_task(task,True)},headers={'Cache-Control':'no-store'})
                elif action=='submit':
                    task,_=self.agent.submit(uid,None,body.get('channel'),body.get('requestId'),body.get('text'))
                    return web.json_response({'task':task},headers={'Cache-Control':'no-store'})
                elif action in ('cancel','approve','decline','answer','continue'):
                    task=await self.agent.action(uid,body.get('taskId'),action,text=body.get('text'),request_id=body.get('requestId'),answers=body.get('answers'))
                    return web.json_response({'task':task},headers={'Cache-Control':'no-store'})
                else:raise ValueError('不支持的操作')
        except (ValueError,TypeError,KeyError) as error:
            return web.json_response({'error':str(error)},status=400,headers={'Cache-Control':'no-store'})
        except OSError:
            return web.json_response({'error':'配置未能持久保存，请检查存储权限'},status=500)
        return web.json_response(self.agent.snapshot(self.memory),headers={'Cache-Control':'no-store'})

    def capture_memory(self, session, pcm):
        uid, identifier = session.memory_user, session.memory_device
        if not uid or not self.memory:return
        try:
            user=self.memory.user(uid)
            if not user or not user['enabled']:return
        except Exception:return
        if len(self.memory_tasks)>=8:
            self.memory_error = '记忆转写队列已满，本轮未保存'
            return
        revision=self.memory_revision.get(uid,0)
        key=session.session_id+':'+str(session.turn)
        async def capture():
            try:
                async with self.memory_lock:
                    text=await asyncio.to_thread(self.transcribe_memory,pcm)
                if self.memory_revision.get(uid,0)==revision:
                    self.memory.record(uid,identifier,key,text)
                    self.memory_error=''
            except Exception as error:
                self.memory_error='记忆转写失败（'+type(error).__name__+'），语音对讲不受影响'
                LOG.warning('Voxi memory transcription failed (%s)',type(error).__name__)
        task=asyncio.create_task(capture());self.memory_tasks.add(task);task.add_done_callback(self.memory_tasks.discard)

    def transcribe_memory(self, pcm, language='zh', details=False):
        import numpy as np
        from pathlib import Path
        model_path=Path(os.environ.get('POCKET_MODEL_DIR','/models/whisper-small'))
        if not (model_path/'model.bin').exists():raise RuntimeError('Whisper model unavailable')
        if self.memory_model is None:
            from faster_whisper import WhisperModel
            self.memory_model=WhisperModel(str(model_path),device='cpu',compute_type='int8',cpu_threads=1,num_workers=1,local_files_only=True)
        source=np.frombuffer(pcm,dtype='<i2').astype(np.float32)/32768
        audio=np.interp(np.arange(0,len(source),1.5),np.arange(len(source)),source).astype(np.float32)
        segments,info=self.memory_model.transcribe(audio,language=language,beam_size=1,vad_filter=True,condition_on_previous_text=False)
        text=''.join(segment.text for segment in segments if segment.no_speech_prob<.6).strip()[:4000]
        if details:return {'text':text,'language':info.language,'probability':info.language_probability}
        return text

    async def memory_configuration(self, request):
        if request.headers.get('Origin') or not hmac.compare_digest(request.headers.get('Authorization',''),'Bearer '+self.owner.config['device_token']):raise web.HTTPForbidden()
        if not self.memory:raise web.HTTPServiceUnavailable(text='Memory storage unavailable')
        if request.method=='GET' and request.path.endswith('/devices'):return web.json_response(self.memory.registry(),headers={'Cache-Control':'no-store'})
        if request.method=='POST':
            try:
                body=await request.json()
                if not isinstance(body,dict):raise ValueError('无效请求')
                result=self.memory.change(body)
                uid=body.get('userId')
                if body.get('action')!='rename_device':self.memory_revision[uid]=self.memory_revision.get(uid,0)+1
                if body.get('action') in ('profile','memory','delete_memory','delete_turn','clear'):
                    for session in list(self.active):
                        if session.memory_user==uid:await session.ws.close(code=1012,message=b'Memory context updated')
                if body.get('action')=='rebind':
                    # Existing sessions retain no access after ownership changes.
                    for session in list(self.active):
                        if session.memory_device==body.get('deviceId'):await session.ws.close(code=1012,message=b'Device owner changed')
            except (ValueError,TypeError) as error:raise web.HTTPBadRequest(text=str(error))
        else:result=self.memory.snapshot()
        result.update({'pendingTranscriptions':len(self.memory_tasks),'lastError':self.memory_error})
        return web.json_response(result,headers={'Cache-Control':'no-store'})

    async def configuration(self, request):
        if request.headers.get('Origin') or not hmac.compare_digest(request.headers.get('Authorization', ''), 'Bearer '+self.owner.config['device_token']):
            raise web.HTTPForbidden()
        if request.method == 'POST':
            try:
                value = await request.json()
                self.settings.save(value)
            except (ValueError, TypeError):
                raise web.HTTPBadRequest(text='Invalid voice configuration')
            except OSError:
                raise web.HTTPInternalServerError(text='Could not persist voice configuration')
        value=self.settings.public()
        value['voices']=self.voice_preview.enrich(value['voices'])
        return web.json_response(value, headers={'Cache-Control':'no-store'})

    async def websocket(self, request):
        if request.headers.get('Origin') or not hmac.compare_digest(
                request.headers.get('Authorization', ''), 'Bearer '+self.owner.config['device_token']):
            raise web.HTTPForbidden()
        if len(self.active)>=self.max_sessions:
            raise web.HTTPServiceUnavailable(text='Native voice session capacity reached', headers={'Retry-After':'5','X-Voxi-Rejection':'session-limit'})
        if time.monotonic() < self.retry_after:
            raise web.HTTPServiceUnavailable(text='Native voice retry cooldown', headers={'Retry-After':'30'})
        ws = web.WebSocketResponse(heartbeat=20,max_msg_size=8192,writer_limit=32768)
        session = VoiceSession(ws, self.binary)
        session.settings_source = self.settings
        identifier=device_id(request.headers.get('X-Voxi-Device-Id'))
        session.memory_device=identifier
        try:session.memory_user=self.memory.bind(identifier) if self.memory else None
        except Exception:session.memory_user=None;self.memory_error='设备用户绑定失败'
        session.memory_store=self.memory
        session.memory_capture=self.capture_memory
        session.agent_radio=self
        self.active.add(session) # reserve before the first await; concurrent admission is bounded
        try:
            await ws.prepare(request)
            await session.start()
            if self.agent and self.agent.store.settings()['enabled']:
                self.agent.start()
            async for packet in ws:
                if packet.type == WSMsgType.BINARY: await session.pcm(packet.data)
                elif packet.type == WSMsgType.TEXT:
                    event = json.loads(packet.data)
                    if not isinstance(event,dict): raise ValueError('Expected object')
                    await session.control(event)
        except Exception as error:
            self.retry_after = time.monotonic()+30
            detail = str(error) if isinstance(error, VoiceFailure) else diagnostic_reason(error)
            LOG.warning('Native voice failed during %s (%s) %s; retry delayed 30s', session.phase, type(error).__name__, detail)
            with contextlib.suppress(Exception):
                await session.send({'type': 'error', 'message': f'Native voice failed at {session.phase}: {detail}'})
        finally:
            try:
                await session.stop()
            finally:
                self.active.discard(session)
                await ws.close()
        return ws
