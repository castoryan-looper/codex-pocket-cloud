import asyncio
import json
import time
import importlib.util
import unittest
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

from voxi import VoiceSession, VoxiRadio, VoiceFailure, diagnostic_reason, frameless_session_update, create_input_track
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


class Socket:
    closed = False
    def __init__(self): self.packets = []
    async def send_json(self, packet): self.packets.append(packet)


class Channel:
    readyState = 'open'
    bufferedAmount = 0
    def __init__(self): self.packets = []
    def send(self, raw): self.packets.append(json.loads(raw))


class VoiceTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(importlib.util.find_spec('av'), 'Optional native voice dependencies not installed')
    async def test_trailing_rtp_silence_finishes_speech_after_backend_completion(self):
        from av import AudioFrame
        ws = Socket(); ws.send_bytes = AsyncMock()
        voice = VoiceSession(ws, 'unused')
        voice.thread = 'voice-thread'; voice.research_turn = 'research'
        voice.research_pending = voice.allow_output = True
        frames = []
        for value in (100, 0):
            frame = AudioFrame(format='s16', layout='mono', samples=480)
            frame.sample_rate = 24000
            frame.planes[0].update(bytes([value, 0]) * 480)
            frames.append(frame)
        async def receive():
            if len(frames) == 1:
                await voice.on_event({'type': 'turn.done', 'turn': {'role': 'assistant'}})
                await voice.notification({'method': 'turn/completed', 'params': {
                    'threadId': voice.thread, 'turn': {'id': 'research', 'status': 'completed'}}})
                voice.last_voice = time.monotonic() - 2
            if not frames:
                raise asyncio.CancelledError()
            return frames.pop(0)
        with self.assertRaises(asyncio.CancelledError):
            await voice.receive_audio(SimpleNamespace(recv=receive))
        self.assertTrue(voice.ready)
        self.assertFalse(voice.outputting)
        self.assertFalse(voice.allow_output)
        self.assertEqual(ws.packets[-2]['type'], 'audio_end')
        self.assertEqual(ws.packets[-1]['type'], 'ready')

    async def test_backend_completion_preserves_already_finished_native_speech(self):
        voice = VoiceSession(Socket(), 'unused')
        voice.thread = 'voice-thread'; voice.research_turn = 'research'
        voice.research_pending = True
        voice.outputting = voice.allow_output = True
        await voice.on_event({'type': 'turn.done', 'turn': {'role': 'assistant'}})
        await voice.notification({'method': 'turn/completed', 'params': {
            'threadId': voice.thread, 'turn': {'id': 'research', 'status': 'completed'}}})
        self.assertTrue(voice.generation_done)
        self.assertFalse(voice.research_result_waiting)
        await voice.end_audio()
        self.assertTrue(voice.ready)
        self.assertFalse(voice.allow_output)

    async def test_research_survives_filler_and_automatically_plays_result(self):
        ws = Socket(); voice = VoiceSession(ws, 'unused')
        voice.thread = 'voice-thread'; voice.turn = 1; voice.allow_output = True
        try:
            await voice.on_event({'type': 'delegation.created', 'item': {
                'id': 'lookup', 'type': 'delegation', 'target': 'client'}})
            await voice.notification({'method': 'turn/started', 'params': {
                'threadId': voice.thread, 'turn': {'id': 'research'}}})
            await voice.begin_audio()
            await voice.on_event({'type': 'turn.done', 'turn': {'role': 'assistant'}})
            await voice.end_audio()
            self.assertTrue(voice.allow_output)
            self.assertFalse(voice.ready)
            self.assertEqual(ws.packets[-1]['type'], 'thinking')
            await voice.notification({'method': 'turn/completed', 'params': {
                'threadId': voice.thread, 'turn': {'id': 'research', 'status': 'completed'}}})
            await voice.on_event({'type': 'output_audio_buffer.started'})
            await voice.on_event({'type': 'turn.done', 'turn': {'role': 'assistant'}})
            await voice.end_audio()
            self.assertEqual(sum(p['type'] == 'audio_start' for p in ws.packets), 2)
            self.assertTrue(voice.ready)
            self.assertFalse(voice.allow_output)
            self.assertFalse(voice.research_result_waiting)
            # The follow-up remains on the same native thread.
            await voice.control({'type': 'ptt_down'})
            self.assertEqual(voice.thread, 'voice-thread')
            self.assertTrue(voice.recording)
            self.assertEqual(voice.continuation_until, 0)
        finally:
            voice.thread = None
            await voice.stop()

    async def test_late_result_can_start_after_filler_already_finished(self):
        voice = VoiceSession(Socket(), 'unused')
        try:
            await voice.begin_audio()
            await voice.end_audio()
            self.assertFalse(voice.allow_output)
            await voice.begin_research()
            voice.research_pending = False
            await voice.begin_audio()
            await voice.end_audio()
            # More native audio segments may follow without another PTT.
            await voice.on_event({'type': 'output_audio_buffer.started'})
            self.assertTrue(voice.outputting)
            self.assertTrue(voice.allow_output)
        finally: await voice.stop()

    async def test_delayed_stop_does_not_end_a_new_audio_segment(self):
        voice = VoiceSession(Socket(), 'unused')
        voice.allow_output = True
        await voice.begin_audio()
        async def new_segment(_):
            await voice.end_audio()
            await voice.begin_audio()
        with patch('voxi.asyncio.sleep', side_effect=new_segment):
            await voice.on_event({'type': 'output_audio_buffer.stopped'})
        self.assertTrue(voice.outputting)

    async def test_research_timeout_reports_failure_and_ignores_old_turn(self):
        ws = Socket(); ws.close = AsyncMock()
        voice = VoiceSession(ws, 'unused'); voice.turn = 2
        voice.research_pending = True; voice.continuation_until = 0
        await voice.watch_research(1)
        ws.close.assert_not_awaited()
        await voice.watch_research(2)
        self.assertEqual(ws.packets[-1]['type'], 'error')
        ws.close.assert_awaited_once()

    async def test_research_failure_requests_a_spoken_explanation(self):
        voice = VoiceSession(Socket(), 'unused')
        voice.thread = 'voice-thread'; voice.research_turn = 'research'
        voice.rpc = SimpleNamespace(call=AsyncMock(), close=AsyncMock())
        await voice.notification({'method': 'turn/completed', 'params': {
            'threadId': voice.thread, 'turn': {'id': 'research', 'status': 'failed'}}})
        await asyncio.sleep(0)
        voice.rpc.call.assert_awaited_once_with('thread/realtime/appendSpeech', {
            'threadId': 'voice-thread', 'text': '这次查询没有完成，请稍后重试。'})
        self.assertTrue(voice.research_result_waiting)
        await voice.stop()

    @unittest.skipUnless(importlib.util.find_spec('av'), 'Optional native voice dependencies not installed')
    async def test_result_rtp_resumes_without_audio_buffer_started_event(self):
        from av import AudioFrame
        ws = Socket(); ws.send_bytes = AsyncMock()
        voice = VoiceSession(ws, 'unused')
        frame = AudioFrame(format='s16', layout='mono', samples=480)
        frame.sample_rate = 24000
        frame.planes[0].update(bytes([100, 0]) * 480)
        track = SimpleNamespace(recv=AsyncMock(side_effect=[frame, asyncio.CancelledError()]))
        try:
            await voice.begin_research()
            voice.research_pending = False
            # The earlier segment has already closed its output gate.
            await voice.end_audio()
            self.assertFalse(voice.allow_output)
            with self.assertRaises(asyncio.CancelledError):
                await voice.receive_audio(track)
            ws.send_bytes.assert_awaited()
            self.assertTrue(voice.outputting)
            self.assertEqual(ws.packets[-1]['type'], 'audio_start')
        finally: await voice.stop()

    async def test_media_stats_only_reports_audio_counters(self):
        voice=VoiceSession(Socket(),'unused');voice.turn=7
        voice.media_gap_max_ms=120;voice.media_receive_gap_max_ms=350;voice.media_pcm_bytes=96000
        voice.peer=SimpleNamespace(getStats=AsyncMock(return_value={
            'audio':SimpleNamespace(type='inbound-rtp',kind='audio',packetsReceived=200,packetsLost=4),
            'video':SimpleNamespace(type='inbound-rtp',kind='video',packetsReceived=999,packetsLost=100),
            'private':SimpleNamespace(type='transport',secret='not-for-logs')}))
        with self.assertLogs('pocket.voxi',level='INFO') as logs:
            await voice.report_media_stats()
        self.assertIn('"packetsLost": 4',logs.output[0])
        self.assertIn('"maxFrameTimelineGapMs": 120',logs.output[0])
        self.assertNotIn('not-for-logs',repr(logs.output))

    async def test_long_response_keeps_running_while_audio_progresses(self):
        ws = Socket(); ws.close = AsyncMock()
        voice = VoiceSession(ws, 'unused'); voice.turn = 1; voice.allow_output = True
        voice.release_at = 0; voice.bytes = 240000; voice.last_output_at = 6
        clock = [6.0]; ticks = []
        async def advance(delay):
            clock[0] += delay; ticks.append(clock[0])
            voice.last_output_at = clock[0]
            if len(ticks) == 5: voice.allow_output = False
        with patch('voxi.time.monotonic', side_effect=lambda: clock[0]), patch('voxi.asyncio.sleep', side_effect=advance):
            await voice.watch_response(1)
        self.assertGreater(clock[0], 120)
        self.assertFalse(ws.packets); ws.close.assert_not_awaited()

    async def test_response_stall_closes_without_indefinite_wait(self):
        ws = Socket(); ws.close = AsyncMock()
        voice = VoiceSession(ws, 'unused'); voice.turn = 1; voice.allow_output = True
        voice.release_at = 0; voice.bytes = 240000; voice.last_output_at = 100
        with patch('voxi.time.monotonic', return_value=131):
            await voice.watch_response(1)
        self.assertIn('stalled', ws.packets[-1]['message']); ws.close.assert_awaited_once()

    async def test_previous_turn_watchdog_cannot_close_current_turn(self):
        ws = Socket(); ws.close = AsyncMock()
        voice = VoiceSession(ws, 'unused'); voice.turn = 2; voice.allow_output = True
        voice.release_at = 0; voice.bytes = 240000
        await voice.watch_response(1)
        self.assertFalse(ws.packets); ws.close.assert_not_awaited()

    async def test_timing_distinguishes_queued_audio_from_rtp_emission(self):
        ws=Socket();voice=VoiceSession(ws,'unused');voice.bytes=480000
        voice.release_at=10;voice.emitted_baseline=9600
        voice.input_track=SimpleNamespace(emitted_bytes=105600)
        with patch('voxi.time.monotonic',return_value=14.5):
            await voice.begin_audio()
        metrics=next(p for p in ws.packets if p['type']=='timing')
        self.assertEqual(metrics['recordedMs'],10000)
        self.assertEqual(metrics['releaseToAudioStartMs'],4500)
        self.assertEqual(metrics['rtpEmittedMs'],2000)
        self.assertEqual(metrics['rtpQueuedMs'],10800)
        self.assertEqual(ws.packets[-1],{'type':'audio_start'})

    @unittest.skipUnless(importlib.util.find_spec('av'), 'Optional native voice dependencies not installed')
    async def test_receive_failure_preserves_type_and_peer_without_exception_text(self):
        ws=Socket();ws.close=AsyncMock()
        voice=VoiceSession(ws,'unused');voice.peer=SimpleNamespace(connectionState='connected')
        track=SimpleNamespace(recv=AsyncMock(side_effect=RuntimeError('secret-url-and-credential')))
        with self.assertLogs('pocket.voxi',level='WARNING') as logs:
            await voice.receive_audio(track)
        self.assertIn('RuntimeError',ws.packets[-1]['message'])
        self.assertIn('peer=connected',ws.packets[-1]['message'])
        self.assertNotIn('secret',repr(ws.packets)+repr(logs.output))
        ws.close.assert_awaited_once()

    @unittest.skipUnless(importlib.util.find_spec('aiortc'), 'Optional native voice dependencies not installed')
    async def test_webrtc_selects_cli_v3_for_avas_quicksilver_v2(self):
        from aiortc import RTCPeerConnection
        voice=VoiceSession(Socket(),'unused')
        from voxi_memory import MemoryStore
        memory=MemoryStore();uid=memory.bind('esp32-111111111111');other=memory.bind('esp32-222222222222')
        memory.record(uid,'esp32-111111111111','one','我喜欢天文学');memory.record(other,'esp32-222222222222','two','我喜欢别人的秘密')
        voice.memory_store=memory;voice.memory_user=uid
        calls=[]
        class Rpc:
            def __init__(self,binary,notify):self.notify=notify
            async def start(self):pass
            async def close(self):pass
            async def call(self,method,params):
                calls.append((method,params))
                if method=='thread/start':return {'thread':{'id':'fixture'}}
                if method=='thread/realtime/start':
                    await self.notify({'method':'thread/realtime/sdp','params':{'threadId':'fixture','sdp':'answer-fixture'}})
                    voice.ready=True
                return {}
        try:
            with patch('voxi.VoiceRpc',Rpc),patch.object(RTCPeerConnection,'setRemoteDescription',new=AsyncMock()):
                await voice.start()
            params=next(params for method,params in calls if method=='thread/realtime/start')
            self.assertIn('天文学',params['prompt']);self.assertNotIn('别人的秘密',params['prompt'])
            self.assertEqual(params['version'],'v3')
            self.assertEqual(params['voice'],'cove')
            self.assertEqual(params['transport']['type'],'webrtc')
            self.assertFalse(params['clientManagedHandoffs'])
            thread = next(params for method, params in calls if method == 'thread/start')
            self.assertEqual(thread['config']['web_search'], 'live')
            self.assertFalse(thread['config']['features.shell_tool'])
            self.assertEqual(thread['sandbox'], 'read-only')
            self.assertEqual(voice.phase,'ready')
        finally:await voice.stop()

    async def test_buffer_until_release_and_submit_v3_audio_with_silence(self):
        class CaptureTrack:
            def submit(self,data):self.data=data
            def stop(self):pass
        ws=Socket();voice=VoiceSession(ws,'unused');voice.events=Channel();voice.input_track=CaptureTrack()
        await voice.on_event({'type':'session.updated','session':{}})
        self.assertTrue(voice.ready)
        await voice.control({'type':'ptt_down'})
        await voice.pcm(bytes(960))
        self.assertEqual(voice.events.packets,[])
        await voice.control({'type':'ptt_up'})
        self.assertEqual(ws.packets[-1],{'type':'idle','reason':'too_short'})
        await voice.control({'type':'ptt_down'})
        original=bytes([10,0])*2880
        await voice.pcm(original)
        self.assertEqual(voice.events.packets,[])
        await voice.control({'type':'ptt_up'})
        self.assertEqual(voice.events.packets,[])
        self.assertEqual(voice.input_track.data,original+bytes(38400))
        self.assertFalse(voice.ready)
        self.assertEqual(ws.packets[-1],{'type':'thinking'})
        await voice.on_event({'type':'turn.done','turn':{'role':'assistant'}})
        self.assertTrue(voice.generation_done)
        await voice.stop()

    @unittest.skipUnless(importlib.util.find_spec('aiortc'), 'Optional native voice dependencies not installed')
    async def test_microphone_track_emits_paced_24k_mono_audio_and_silence(self):
        from fractions import Fraction
        track=create_input_track()
        try:
            silent=await track.recv()
            self.assertEqual(silent.sample_rate,24000)
            self.assertEqual(silent.samples,480)
            self.assertEqual(silent.time_base,Fraction(1,24000))
            track.submit(bytes([1,0])*480)
            with self.assertRaises(RuntimeError):track.submit(bytes(960))
            audio=await track.recv()
            self.assertEqual(bytes(audio.planes[0])[:960],bytes([1,0])*480)
            self.assertEqual(audio.pts-silent.pts,480)
            self.assertEqual(track.emitted_bytes,960)
            tail=await track.recv()
            self.assertEqual(bytes(tail.planes[0])[:960],bytes(960))
        finally:track.stop()

    @unittest.skipUnless(importlib.util.find_spec('aiortc'), 'Optional native voice dependencies not installed')
    async def test_queued_microphone_audio_traverses_real_webrtc_rtp(self):
        from aiortc import RTCPeerConnection,RTCConfiguration
        a=RTCPeerConnection(RTCConfiguration(iceServers=[]))
        b=RTCPeerConnection(RTCConfiguration(iceServers=[]))
        track=create_input_track();remote=asyncio.get_running_loop().create_future()
        @b.on('track')
        def got_track(received):
            if not remote.done():remote.set_result(received)
        try:
            a.addTrack(track)
            # Candidate self-tests run with Docker --network none. aioice
            # excludes loopback by default, so explicitly gather only loopback
            # in this local test. Production candidate discovery is untouched.
            with patch('aioice.ice.get_host_addresses',return_value=['127.0.0.1']):
                await a.setLocalDescription(await a.createOffer())
                await b.setRemoteDescription(a.localDescription)
                answer=await b.createAnswer()
                # Install both peers' ICE credentials before starting loopback
                # checks: loopback can deliver STUN before setRemoteDescription
                # completes on the offerer if the answerer starts first.
                await a.setRemoteDescription(answer)
                await b.setLocalDescription(answer)
                transceiver=b.getTransceivers()[0]
                for candidate in transceiver.sender.transport.transport.iceGatherer.getLocalCandidates():
                    candidate.sdpMid=transceiver.mid;candidate.sdpMLineIndex=0
                    await a.addIceCandidate(candidate)
                await a.addIceCandidate(None)
            self.assertIn('127.0.0.1',a.localDescription.sdp)
            self.assertIn('127.0.0.1',b.localDescription.sdp)
            received=await asyncio.wait_for(remote,5)
            track.submit(bytes([0xe8,3])*4800)
            async def heard():
                for _ in range(40):
                    frame=await received.recv()
                    raw=bytes(frame.planes[0])[:frame.samples*len(frame.layout.channels)*2]
                    if any(abs(int.from_bytes(raw[i:i+2],'little',signed=True))>50 for i in range(0,len(raw),2)):return True
                return False
            self.assertTrue(await asyncio.wait_for(heard(),5))
            self.assertGreater(track.emitted_bytes,0)
        finally:
            await a.close();await b.close()

    async def test_reject_non_device_access_before_starting_cli(self):
        radio = VoxiRadio(SimpleNamespace(config={'device_token':'test'}),'unused')
        for headers in ({}, {'Authorization':'Bearer test','Origin':'https://elsewhere.invalid'}):
            with self.assertRaises(web.HTTPForbidden):
                await radio.websocket(SimpleNamespace(headers=headers))
        self.assertFalse(radio.active)

    async def test_two_devices_are_isolated_and_third_is_rejected(self):
        radio=VoxiRadio(SimpleNamespace(config={'device_token':'test'}),'unused')
        radio.max_sessions=2
        async def ready(session,channel=0):
            await session.send({'type':'ready'})
        async def control(session,event):
            session.channel=event['channel']
            await session.send({'type':'channel','channel':session.channel})
        app=web.Application();app.router.add_get('/voxi',radio.websocket)
        with patch.object(VoiceSession,'start',new=ready),patch.object(VoiceSession,'control',new=control),patch.object(VoiceSession,'stop',new=AsyncMock()):
            async with TestClient(TestServer(app)) as client:
                headers={'Authorization':'Bearer test'}
                a=await client.ws_connect('/voxi',headers=headers);await a.receive_json()
                b=await client.ws_connect('/voxi',headers=headers);await b.receive_json()
                self.assertEqual(len(radio.active),2)
                response=await client.get('/voxi',headers=headers)
                self.assertEqual(response.status,503)
                self.assertEqual(response.headers['X-Voxi-Rejection'],'session-limit')
                await a.send_json({'channel':3});self.assertEqual((await a.receive_json())['channel'],3)
                await b.send_json({'channel':1});self.assertEqual((await b.receive_json())['channel'],1)
                self.assertEqual(sorted(s.channel for s in radio.active),[1,3])
                await a.close()
                for _ in range(30):
                    if len(radio.active)==1:break
                    await asyncio.sleep(.01)
                self.assertEqual(len(radio.active),1)
                c=await client.ws_connect('/voxi',headers=headers);await c.receive_json()
                await b.send_json({'channel':2});self.assertEqual((await b.receive_json())['channel'],2)
                await b.close();await c.close()

    async def test_failed_voice_setup_enters_cooldown_without_restarting_cli(self):
        radio=VoxiRadio(SimpleNamespace(config={'device_token':'test'}),'unused')
        app=web.Application();app.router.add_get('/voxi',radio.websocket)
        with patch.object(VoiceSession,'start',side_effect=RuntimeError('fixture')) as start:
            async with TestClient(TestServer(app)) as client:
                socket=await client.ws_connect('/voxi',headers={'Authorization':'Bearer test'})
                self.assertEqual((await socket.receive_json())['type'],'error')
                await socket.close()
                response=await client.get('/voxi',headers={'Authorization':'Bearer test'})
                self.assertEqual(response.status,503)
                self.assertEqual(start.call_count,1)

    @unittest.skipUnless(importlib.util.find_spec('aiortc'), 'Optional native voice dependencies not installed')
    async def test_webrtc_peer_can_negotiate_audio_and_event_channel(self):
        from aiortc import RTCPeerConnection, RTCConfiguration
        peer=RTCPeerConnection(RTCConfiguration(iceServers=[]))
        try:
            peer.addTransceiver('audio',direction='recvonly')
            peer.createDataChannel('oai-events')
            await peer.setLocalDescription(await peer.createOffer())
            self.assertIn('m=audio',peer.localDescription.sdp)
            self.assertIn('m=application',peer.localDescription.sdp)
        finally:
            await peer.close()


class SessionContractTests(unittest.TestCase):
    def test_avas_update_matches_frameless_bidi_schema(self):
        packet=frameless_session_update();session=packet['session']
        self.assertEqual(packet['type'],'session.update')
        self.assertEqual(set(session),{'audio','delegation'})
        self.assertEqual(session['delegation'],{'type':'client'})
        self.assertEqual(session['audio'],{'output':{'voice':'cove'}})


class DiagnosticTests(unittest.TestCase):
    def test_error_categories_preserve_reason_without_credentials(self):
        reason=diagnostic_reason('HTTP 403 forbidden token=secret-token-value https://private.example/path?sdp=private')
        self.assertIn('access-denied',reason)
        self.assertIn('http-403',reason)
        self.assertNotIn('secret',reason)
        self.assertNotIn('private',reason)
    def test_rpc_code_retained_and_arbitrary_text_removed(self):
        error=VoiceFailure('rpc',-32602,'Invalid params: voice not supported for version v1 secret-credential')
        self.assertIn('-32602',str(error))
        self.assertIn('unsupported',str(error))
        self.assertIn('voice',str(error))
        self.assertNotIn('secret',str(error))
