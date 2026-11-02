import asyncio
import json
import importlib.util
import unittest
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

from voxi import VoiceSession, VoxiRadio, VoiceFailure, diagnostic_reason, frameless_session_update
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
    @unittest.skipUnless(importlib.util.find_spec('aiortc'), 'Optional native voice dependencies not installed')
    async def test_webrtc_selects_cli_v3_for_avas_quicksilver_v2(self):
        from aiortc import RTCPeerConnection
        voice=VoiceSession(Socket(),'unused')
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
            self.assertEqual(params['version'],'v3')
            self.assertEqual(params['voice'],'cove')
            self.assertEqual(params['transport']['type'],'webrtc')
            self.assertEqual(voice.phase,'ready')
        finally:await voice.stop()

    async def test_buffer_until_release_and_submit_v3_audio_with_silence(self):
        import base64
        ws=Socket();voice=VoiceSession(ws,'unused');voice.events=Channel()
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
        self.assertTrue(all(p['type']=='input_audio.append' for p in voice.events.packets))
        submitted=b''.join(base64.b64decode(p['audio']) for p in voice.events.packets)
        self.assertEqual(submitted,original+bytes(38400))
        self.assertFalse(voice.ready)
        self.assertEqual(ws.packets[-1],{'type':'thinking'})
        await voice.on_event({'type':'turn.done','turn':{'role':'assistant'}})
        self.assertTrue(voice.generation_done)
        await voice.stop()

    async def test_reject_non_device_access_before_starting_cli(self):
        radio = VoxiRadio(SimpleNamespace(config={'device_token':'test'}),'unused')
        for headers in ({}, {'Authorization':'Bearer test','Origin':'https://elsewhere.invalid'}):
            with self.assertRaises(web.HTTPForbidden):
                await radio.websocket(SimpleNamespace(headers=headers))
        self.assertFalse(radio.active)

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
