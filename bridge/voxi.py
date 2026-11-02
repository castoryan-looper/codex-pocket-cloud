"""Authenticated, voice-only Codex app-server/WebRTC bridge for Voxi.

Uses the official CLI's existing CODEX_HOME login. Does not read/export auth.json,
run user shell commands, create Codex turns or reuse Pocket's task conversation.
The /voxi endpoint is Docker-internal only, not added to the public Caddy routes.
"""
import asyncio
import contextlib
import hmac
import json
import logging
import re
import tempfile
import time
from collections import deque

from aiohttp import web, WSMsgType
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


def manual_ptt_update():
    return {'type': 'session.update', 'session': {
        'tools': [], 'input_audio_format': 'pcm16', 'turn_detection': None}}


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


class VoiceSession:
    def __init__(self, ws, binary):
        self.ws, self.binary = ws, binary
        self.rpc = self.peer = self.events = self.workspace = None
        self.answer = None
        self.ready = self.recording = self.allow_output = self.outputting = False
        self.bytes = self.played = 0
        self.thread = self.item = None
        self.channel = 0
        self.tasks = set()
        self.lookback = deque(maxlen=2)
        self.generation_done = False
        self.last_voice = time.monotonic()
        self.turn = 0
        self.response_id = None
        self.phase = 'new'

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def send(self, packet):
        if not self.ws.closed: await self.ws.send_json(packet)

    def event(self, packet):
        if self.events and self.events.readyState == 'open':
            self.events.send(json.dumps(packet))

    async def notification(self, packet):
        method, params = packet.get('method'), packet.get('params', {})
        if params.get('threadId') != self.thread: return
        if method == 'thread/realtime/sdp' and self.answer and not self.answer.done():
            self.answer.set_result(params['sdp'])
        elif method in ('thread/realtime/error', 'thread/realtime/closed'):
            if self.answer and not self.answer.done():
                self.answer.set_exception(VoiceFailure('notification', params.get('code'), params.get('error', params.get('message', params.get('reason', '')))))
            else:
                await self.send({'type': 'error', 'message': 'Codex native voice session closed; check account support'})
                await self.ws.close()

    async def start(self, channel=0):
        from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration
        await self.stop()
        self.channel = channel
        await self.send({'type': 'connecting', 'channel': channel})
        self.workspace = tempfile.TemporaryDirectory(prefix='voxi-chat-')
        self.rpc = VoiceRpc(self.binary, self.notification)
        self.phase = 'account'
        await self.rpc.start()
        self.phase = 'thread'
        self.thread = (await self.rpc.call('thread/start', {
            'cwd': self.workspace.name, 'approvalPolicy': 'never', 'sandbox': 'read-only',
            'ephemeral': True, 'developerInstructions': 'Voice conversation only. Do not use tools, files, agents or shell commands.'
        }))['thread']['id']
        self.peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.peer.addTransceiver('audio', direction='recvonly')
        self.events = self.peer.createDataChannel('oai-events')
        @self.events.on('open')
        def opened():
            # Input travels as ordered PCM events over the RTC data channel.
            # PTT explicitly commits; no VAD timeout and no silent RTP input.
            # AVAS accepts the classic flat session schema, not the public
            # Realtime GA session.type/audio.input shape. pcm16 is 24kHz mono.
            self.event(manual_ptt_update())
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
        await self.peer.setLocalDescription(await self.peer.createOffer())
        self.answer = asyncio.get_running_loop().create_future()
        self.phase = 'webrtc-sdp'
        names = ['营地一号：沉稳值守员', '巡逻二号：干练的搭档', '夜航三号：安静的老朋友', '探索四号：好奇的队友']
        prompt = ('你是虚构对讲机里的AI伙伴，呼号'+names[channel]+'。用简短自然中文直接回应，通常一到三句，十秒内说完。'
                  '偶尔说收到或完毕，不要每句重复。不要模拟电流声，不要执行任务或使用工具。听不清就请求重说。身份被问及时如实说明是AI。')
        await self.rpc.call('thread/realtime/start', {'threadId': self.thread,
            'transport': {'type': 'webrtc', 'sdp': self.peer.localDescription.sdp},
            # Codex v3 maps to the AVAS-required quicksilver=v2 alpha header.
            # cove is supported by v3; marin belongs to the separate v2 API.
            'outputModality': 'audio', 'version': 'v3', 'voice': 'cove', 'prompt': prompt,
            'includeStartupContext': False, 'clientManagedHandoffs': True})
        answer = await asyncio.wait_for(self.answer, 40)
        await self.peer.setRemoteDescription(RTCSessionDescription(sdp=answer, type='answer'))
        self.phase = 'manual-ptt-configuration'
        # Do not announce READY until the backend accepts manual PTT configuration.
        for _ in range(200):
            if self.ready:
                self.phase = 'ready'
                return
            if self.ws.closed: raise RuntimeError('Voice setup failed')
            await asyncio.sleep(.1)
        raise RuntimeError('Codex voice setup timed out; manual PTT was not acknowledged')

    async def on_event(self, event):
        kind = event.get('type')
        if kind == 'session.updated':
            session = event.get('session', {})
            input_config = session.get('audio', {}).get('input', {})
            manual = ('turn_detection' in input_config and input_config['turn_detection'] is None) or ('turn_detection' in session and session['turn_detection'] is None)
            if manual and session.get('tools') == []:
                self.ready = True
                await self.send({'type': 'ready', 'channel': self.channel})
        elif kind == 'response.created':
            response = event.get('response', {})
            if response.get('metadata', {}).get('turn') == str(self.turn): self.response_id = response.get('id')
        elif kind == 'response.output_item.added' and event.get('response_id') == self.response_id:
            self.item = event.get('item', {}).get('id')
        elif kind == 'output_audio_buffer.started' and self.allow_output and not self.recording:
            await self.begin_audio()
        elif kind == 'output_audio_buffer.stopped':
            # Separate RTC media/control channels may deliver the final packet later.
            turn = self.turn
            await asyncio.sleep(.25)
            if turn == self.turn and self.outputting:
                self.outputting = self.allow_output = False
                await self.send({'type': 'audio_end'})
        elif kind == 'response.done' and event.get('response', {}).get('id') == self.response_id:
            if event.get('response', {}).get('status') in ('failed', 'incomplete'):
                await self.send({'type': 'error', 'message': 'Codex native voice response failed or was incomplete'})
                await self.ws.close()
                return
            self.generation_done = True
        elif kind == 'error':
            if event.get('error', {}).get('code') in ('response_cancel_not_active', 'input_audio_buffer_commit_empty'): return
            await self.send({'type': 'error', 'message': 'Codex voice rejected an audio/control event'})
            await self.ws.close()

    async def begin_audio(self):
        if self.outputting: return
        self.outputting = True
        self.played = 0
        await self.send({'type': 'audio_start'})

    async def receive_audio(self, track):
        from av import AudioResampler
        resampler = AudioResampler(format='s16', layout='mono', rate=24000)
        try:
            while True:
                frame = await track.recv()
                for mono in resampler.resample(frame):
                    data = bytes(mono.planes[0])[:mono.samples*2]
                    # Fall back to a small PCM onset detector when the transport
                    # omits output_audio_buffer.started. Never play idle RTP silence.
                    voiced = any(abs(int.from_bytes(data[i:i+2], 'little', signed=True)) > 32 for i in range(0,len(data),2))
                    if voiced: self.last_voice = time.monotonic()
                    if self.allow_output and not self.recording:
                        if not self.outputting and voiced:
                            await self.begin_audio()
                            for prior in self.lookback: await self.ws.send_bytes(prior)
                        if self.outputting:
                            for i in range(0,len(data),960): await self.ws.send_bytes(data[i:i+960])
                            if self.generation_done and time.monotonic()-self.last_voice > 1:
                                self.outputting = self.allow_output = False
                                await self.send({'type': 'audio_end'})
                    self.lookback.append(data)
        except asyncio.CancelledError: raise
        except Exception:
            if not self.ws.closed:
                await self.send({'type': 'error', 'message': 'WebRTC audio receive failed'})
                await self.ws.close()

    async def control(self, event):
        kind = event.get('type')
        if kind == 'played': self.played = max(0,min(60000,int(event.get('ms',0))))
        elif kind == 'channel':
            channel = event.get('channel')
            if type(channel) is int and 0 <= channel < 4: await self.start(channel)
        elif kind == 'ptt_down' and self.ready and not self.recording:
            self.turn += 1
            self.response_id = None
            self.recording = True
            self.allow_output = self.outputting = False
            self.bytes = 0
            self.lookback.clear()
            self.event({'type': 'response.cancel'})
            self.event({'type': 'output_audio_buffer.clear'})
            if self.item:
                self.event({'type': 'conversation.item.truncate', 'item_id': self.item, 'content_index': 0, 'audio_end_ms': self.played})
            self.item = None
            self.event({'type': 'input_audio_buffer.clear'})
            await self.send({'type': 'flush'})
            await self.send({'type': 'recording'})
        elif kind == 'ptt_up' and self.recording:
            self.recording = False
            if self.bytes < 4800:
                self.event({'type': 'input_audio_buffer.clear'})
                await self.send({'type': 'idle', 'reason': 'too_short'})
                return
            self.allow_output = True
            self.generation_done = False
            self.lookback.clear()
            self.event({'type': 'input_audio_buffer.commit'})
            self.event({'type': 'response.create', 'response': {'metadata': {'turn': str(self.turn)}}})
            await self.send({'type': 'thinking'})
            turn = self.turn
            async def response_timeout():
                await asyncio.sleep(30)
                if turn == self.turn and self.allow_output and not self.ws.closed:
                    await self.send({'type': 'error', 'message': 'Codex voice response timed out'})
                    await self.ws.close()
            self.spawn(response_timeout())

    async def pcm(self, data):
        import base64
        if not self.ready or not self.recording: return
        self.bytes += len(data)
        if len(data)%2 or self.bytes > 30*48000: raise ValueError('Invalid/overlong PCM')
        if self.events.bufferedAmount > 96000: raise ValueError('Voice upload is too slow')
        self.event({'type': 'input_audio_buffer.append', 'audio': base64.b64encode(data).decode()})

    async def stop(self):
        self.ready = self.recording = self.allow_output = self.outputting = False
        # Ignore closure notifications from the old peer while changing channel.
        self.thread = self.item = self.response_id = None
        self.lookback.clear()
        self.generation_done = False
        for task in self.tasks: task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        if self.peer:
            peer, self.peer = self.peer, None
            await peer.close()
        if self.rpc:
            rpc, self.rpc = self.rpc, None
            await rpc.close()
        if self.workspace:
            self.workspace.cleanup()
            self.workspace = None


class VoxiRadio:
    def __init__(self, owner, binary):
        self.owner, self.binary, self.active = owner, binary, False
        self.retry_after = 0

    async def websocket(self, request):
        if request.headers.get('Origin') or not hmac.compare_digest(
                request.headers.get('Authorization', ''), 'Bearer '+self.owner.config['device_token']):
            raise web.HTTPForbidden()
        if self.active: raise web.HTTPServiceUnavailable(text='One native voice session at a time')
        if time.monotonic() < self.retry_after:
            raise web.HTTPServiceUnavailable(text='Native voice retry cooldown', headers={'Retry-After':'30'})
        self.active = True
        ws = web.WebSocketResponse(heartbeat=20,max_msg_size=8192,writer_limit=32768)
        session = VoiceSession(ws, self.binary)
        try:
            await ws.prepare(request)
            await session.start()
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
            await session.stop()
            await ws.close()
            self.active = False
        return ws
