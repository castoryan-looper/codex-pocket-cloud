import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from aiortc import RTCPeerConnection
from aiortc.rtcrtpreceiver import RTCRtpReceiver
from aiortc.rtcrtpparameters import RTCRtpCodecParameters
from aiortc.rtp import RtpPacket
from voice_recovery import offer_audio_nack, configure_audio_recovery

class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_nack_is_only_enabled_when_negotiated_and_missing_audio_packet_can_be_recovered(self):
        peer=RTCPeerConnection();peer.addTransceiver('audio')
        try:
            original=await peer.createOffer()
            offered=offer_audio_nack(original)
            self.assertIn(' nack\r\n', offered.sdp)
            receiver=RTCRtpReceiver('audio', SimpleNamespace(state='new'))
            adapter=SimpleNamespace(remoteDescription=original,getTransceivers=lambda:[SimpleNamespace(kind='audio',receiver=receiver)])
            self.assertFalse(configure_audio_recovery(adapter))
            self.assertIsNone(receiver._RTCRtpReceiver__nack_generator)
            adapter.remoteDescription=offered
            self.assertTrue(configure_audio_recovery(adapter,64))
            self.assertEqual(receiver._RTCRtpReceiver__jitter_buffer.capacity,64)
            receiver._send_rtcp_nack=AsyncMock()
            receiver._RTCRtpReceiver__codecs[96]=RTCRtpCodecParameters(mimeType='audio/opus',clockRate=48000,channels=2,payloadType=96)
            def packet(seq):return RtpPacket(payload_type=96,sequence_number=seq,timestamp=seq*960,ssrc=1,payload=b'\xf8\xff\xfe')
            await receiver._handle_rtp_packet(packet(1),arrival_time_ms=0)
            await receiver._handle_rtp_packet(packet(3),arrival_time_ms=40)
            receiver._send_rtcp_nack.assert_awaited_once_with(1,[2])
            await receiver._handle_rtp_packet(packet(2),arrival_time_ms=60)
            self.assertEqual(receiver._RTCRtpReceiver__nack_generator.missing,set())
        finally:await peer.close()
