"""Negotiated audio retransmission support for pinned aiortc 1.14.0.

aiortc enables NACK for video but not audio. Only enable our audio adapter when
the remote endpoint accepts generic NACK in SDP. No global monkey patching.
"""
def offer_audio_nack(description):
    from aiortc import RTCSessionDescription
    from aiortc.sdp import SessionDescription
    from aiortc.rtcrtpparameters import RTCRtcpFeedback
    parsed = SessionDescription.parse(description.sdp)
    for media in parsed.media:
        if media.kind == 'audio':
            for codec in media.rtp.codecs:
                if codec.mimeType.lower() == 'audio/opus' and not any(f.type == 'nack' and not f.parameter for f in codec.rtcpFeedback):
                    codec.rtcpFeedback.append(RTCRtcpFeedback(type='nack'))
    return RTCSessionDescription(sdp=str(parsed), type=description.type)

def configure_audio_recovery(peer, capacity=64):
    if capacity not in (16,32,64,128): raise ValueError('Invalid recovery window')
    from importlib.metadata import version
    from aiortc.sdp import SessionDescription
    from aiortc.rtcrtpreceiver import NackGenerator
    from aiortc.jitterbuffer import JitterBuffer
    if version('aiortc') != '1.14.0' or not peer.remoteDescription:
        return False
    media = SessionDescription.parse(peer.remoteDescription.sdp).media
    negotiated = any(m.kind == 'audio' and any(c.mimeType.lower() == 'audio/opus' and any(f.type == 'nack' and not f.parameter for f in c.rtcpFeedback) for c in m.rtp.codecs) for m in media)
    if not negotiated:
        return False
    enabled = False
    for transceiver in peer.getTransceivers():
        if transceiver.kind == 'audio':
            receiver = transceiver.receiver
            if not hasattr(receiver, '_RTCRtpReceiver__nack_generator') or not hasattr(receiver, '_RTCRtpReceiver__jitter_buffer'):
                continue
            receiver._RTCRtpReceiver__nack_generator = NackGenerator()
            # 64 x 20ms gives retransmissions a bounded 1.28s recovery window.
            receiver._RTCRtpReceiver__jitter_buffer = JitterBuffer(capacity=capacity, prefetch=4)
            enabled = True
    return enabled
