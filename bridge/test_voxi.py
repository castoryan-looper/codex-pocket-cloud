import asyncio
import json
import importlib.util
import unittest
from types import SimpleNamespace

from voxi import VoiceSession, VoxiRadio
from aiohttp import web


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
    async def test_ptt_short_press_manual_commit_and_interruption(self):
        ws = Socket()
        voice = VoiceSession(ws, 'unused')
        voice.events = Channel()
        await voice.on_event({'type':'session.updated','session':{'audio':{'input':{'turn_detection':{'type':'server_vad'}}}}})
        self.assertFalse(voice.ready)
        await voice.on_event({'type':'session.updated','session':{'tools':[],'audio':{'input':{'turn_detection':None}}}})
        self.assertTrue(voice.ready)
        await voice.control({'type':'ptt_down'})
        await voice.pcm(bytes(960))
        await voice.control({'type':'ptt_up'})
        self.assertEqual(ws.packets[-1], {'type':'idle','reason':'too_short'})
        await voice.control({'type':'ptt_down'})
        await voice.pcm(bytes(5760))
        await voice.control({'type':'ptt_up'})
        self.assertEqual([p['type'] for p in voice.events.packets][-2:], ['input_audio_buffer.commit','response.create'])
        self.assertTrue(voice.allow_output)
        await voice.begin_audio()
        stopping = asyncio.create_task(voice.on_event({'type':'output_audio_buffer.stopped'}))
        await asyncio.sleep(.01)
        await voice.control({'type':'ptt_down'})
        await stopping
        self.assertFalse(voice.outputting)
        self.assertNotIn({'type':'audio_end'},ws.packets)
        self.assertEqual(ws.packets[-2:], [{'type':'flush'},{'type':'recording'}])
        await voice.stop()

    async def test_reject_non_device_access_before_starting_cli(self):
        radio = VoxiRadio(SimpleNamespace(config={'device_token':'test'}),'unused')
        for headers in ({}, {'Authorization':'Bearer test','Origin':'https://elsewhere.invalid'}):
            with self.assertRaises(web.HTTPForbidden):
                await radio.websocket(SimpleNamespace(headers=headers))
        self.assertFalse(radio.active)

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
