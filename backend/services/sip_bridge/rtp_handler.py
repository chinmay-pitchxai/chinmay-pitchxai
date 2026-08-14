"""
RTP media handler for Tata Tele SIP audio bridge.

Receives G.711/G.722 RTP from Tata Tele, converts to 16kHz PCM for Gemini Live.
Sends Gemini Live audio back as RTP to Tata Tele.
"""
import asyncio
import os
import random
import struct
import time
from typing import Callable, Optional

from loguru import logger

G711_ULAW_TABLE = list(range(256))

def _ulaw_decode(sample: int) -> int:
    """Decode a μ-law sample to 16-bit linear PCM."""
    sample = ~sample & 0xFF
    sign = 1 if sample & 0x80 else -1
    exponent = (sample >> 4) & 0x07
    mantissa = sample & 0x0F
    sample = (mantissa << (exponent + 3)) + (0x80 << exponent) - 0x84
    return sign * sample


def _alaw_decode(sample: int) -> int:
    """Decode an A-law sample to 16-bit linear PCM."""
    sample ^= 0x55
    sign = 1 if sample & 0x80 else -1
    sample &= 0x7F
    exponent = (sample >> 4) & 0x07
    mantissa = sample & 0x0F
    if exponent == 0:
        sample = (mantissa << 1) | 1
    else:
        sample = ((mantissa << 1) | 0x21) << (exponent - 1)
    return sign * sample


def _ulaw_encode(pcm: int) -> int:
    """Encode 16-bit linear PCM to μ-law."""
    sign = 0
    if pcm < 0:
        sign = 0x80
        pcm = -pcm
    pcm = min(pcm, 32767)
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (pcm & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (pcm >> (exponent + 3)) & 0x0F
    byte = sign | (exponent << 4) | mantissa
    return ~byte & 0xFF


class RTPPacket:
    def __init__(self, data: bytes):
        self.data = data
        self.version = (data[0] >> 6) & 0x03
        self.padding = (data[0] >> 5) & 0x01
        self.extension = (data[0] >> 4) & 0x01
        self.cc = data[0] & 0x0F
        self.marker = (data[1] >> 7) & 0x01
        self.payload_type = data[1] & 0x7F
        self.sequence_number = struct.unpack("!H", data[2:4])[0]
        self.timestamp = struct.unpack("!I", data[4:8])[0]
        self.ssrc = struct.unpack("!I", data[8:12])[0]
        self.header_len = 12 + (self.cc * 4)
        if self.extension:
            ext_offset = self.header_len
            ext_len = struct.unpack("!H", data[ext_offset + 2:ext_offset + 4])[0]
            self.header_len += 4 + ext_len * 4
        self.payload = data[self.header_len:]


class RTPSession:
    def __init__(
        self,
        remote_ip: str,
        remote_port: int,
        local_port: int,
        on_audio_from_call: Optional[Callable[[bytes], None]] = None,
        codec: str = "PCMU",
        sample_rate: int = 8000,
    ):
        self.remote_ip = remote_ip
        self.remote_port = remote_port
        self.local_port = local_port
        self.on_audio_from_call = on_audio_from_call
        self.codec = codec
        self.sample_rate = sample_rate
        self.ssrc = random.randint(0, 0xFFFFFFFF)
        self.sequence_number = random.randint(0, 0xFFFF)
        self.timestamp = 0
        self.expected_seq = 0
        self.jitter_buffer: list[tuple[float, bytes]] = []
        self._transport: Optional[asyncio.DatagramTransport] = None
        self.active = False
        self.started_at: Optional[float] = None

    async def start(self):
        loop = asyncio.get_event_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _RTPProtocol(self),
            local_addr=("0.0.0.0", self.local_port),
        )
        self.active = True
        self.started_at = time.time()
        logger.info("RTP session started on port {} → {}:{}", self.local_port, self.remote_ip, self.remote_port)

    async def stop(self):
        self.active = False
        if self._transport:
            self._transport.close()

    def _convert_rtp_to_pcm(self, pkt: RTPPacket) -> Optional[bytes]:
        """Convert RTP payload to 16kHz 16-bit PCM."""
        payload = pkt.payload
        if not payload:
            return None

        if self.codec == "PCMU":
            pcm_8k = b""
            for byte in payload:
                sample = _ulaw_decode(byte)
                pcm_8k += struct.pack("!h", sample)
            return _resample_8k_to_16k(pcm_8k)
        elif self.codec == "PCMA":
            pcm_8k = b""
            for byte in payload:
                sample = _alaw_decode(byte)
                pcm_8k += struct.pack("!h", sample)
            return _resample_8k_to_16k(pcm_8k)
        elif self.codec == "G722":
            return _decode_g722(payload)
        else:
            logger.warning("Unsupported codec: {}", self.codec)
            return None

    def _convert_pcm_to_rtp(self, pcm_16k: bytes) -> bytes:
        """Convert 16kHz 16-bit PCM to RTP payload."""
        pcm_8k = _resample_16k_to_8k(pcm_16k)

        if self.codec == "PCMU":
            payload = b""
            for i in range(0, len(pcm_8k), 2):
                sample = struct.unpack("!h", pcm_8k[i:i + 2])[0]
                payload += bytes([_ulaw_encode(sample)])
            return payload
        elif self.codec == "PCMA":
            payload = b""
            for i in range(0, len(pcm_8k), 2):
                sample = struct.unpack("!h", pcm_8k[i:i + 2])[0]
                payload += bytes([_alaw_encode(sample)])
            return payload
        else:
            return pcm_8k

    def handle_incoming_rtp(self, pkt: RTPPacket):
        """Handle incoming RTP packet from the call leg."""
        if pkt.payload_type in (101, 96):
            return

        pcm_16k = self._convert_rtp_to_pcm(pkt)
        if pcm_16k and self.on_audio_from_call:
            self.on_audio_from_call(pcm_16k)

        if self.expected_seq > 0 and pkt.sequence_number != self.expected_seq:
            lost = pkt.sequence_number - self.expected_seq
            if lost > 0:
                logger.debug("RTP: {} packets lost (seq {} → {})", lost, self.expected_seq, pkt.sequence_number)
        self.expected_seq = pkt.sequence_number + 1

    async def send_audio(self, pcm_16k: bytes):
        """Send 16kHz PCM audio as RTP to the call leg."""
        if not self.active or not self._transport:
            return

        rtp_payload = self._convert_pcm_to_rtp(pcm_16k)
        if not rtp_payload:
            return

        header = struct.pack(
            "!BBHII",
            0x80,
            0,
            self.sequence_number,
            self.timestamp,
            self.ssrc,
        )

        pkt_data = header + rtp_payload
        try:
            self._transport.sendto(pkt_data, (self.remote_ip, self.remote_port))
        except Exception as e:
            logger.debug("RTP send error: {}", e)

        self.sequence_number = (self.sequence_number + 1) & 0xFFFF
        pkt_size = len(rtp_payload)
        if self.codec in ("PCMU", "PCMA"):
            self.timestamp += pkt_size * 2
        else:
            self.timestamp += pkt_size

    def send_dtmf(self, digit: int):
        """Send DTMF event (RFC 2833)."""
        if not self.active or not self._transport:
            return
        event_id = min(digit, 15)
        marker = 0x80
        pt = 101
        seq = struct.pack("!H", self.sequence_number)
        ts = struct.pack("!I", self.timestamp)
        ssrc = struct.pack("!I", self.ssrc)
        body = struct.pack("!BBH", event_id, 0x80, 1600)
        header = struct.pack("!BB", 0x80, marker | pt)
        pkt = header + seq + ts + ssrc + body
        try:
            self._transport.sendto(pkt, (self.remote_ip, self.remote_port))
        except Exception:
            pass
        self.sequence_number = (self.sequence_number + 1) & 0xFFFF


def _resample_8k_to_16k(pcm_8k: bytes) -> bytes:
    """Simple linear interpolation resampling from 8kHz to 16kHz."""
    samples = []
    for i in range(0, len(pcm_8k) - 1, 2):
        s1 = struct.unpack("!h", pcm_8k[i:i + 2])[0]
        s2 = struct.unpack("!h", pcm_8k[i + 2:i + 4])[0] if i + 4 <= len(pcm_8k) else s1
        samples.append(s1)
        samples.append((s1 + s2) >> 1)
    if not samples and len(pcm_8k) >= 2:
        s = struct.unpack("!h", pcm_8k[:2])[0]
        samples = [s, s]
    return b"".join(struct.pack("!h", s) for s in samples)


def _resample_16k_to_8k(pcm_16k: bytes) -> bytes:
    """Simple decimation resampling from 16kHz to 8kHz."""
    samples = []
    for i in range(0, len(pcm_16k) - 1, 4):
        s = struct.unpack("!h", pcm_16k[i:i + 2])[0]
        samples.append(s)
    if not samples and len(pcm_16k) >= 2:
        samples = [struct.unpack("!h", pcm_16k[:2])[0]]
    return b"".join(struct.pack("!h", s) for s in samples)


def _decode_g722(payload: bytes) -> bytes:
    """G.722 decode stub — returns silence (needs libg722 for real decode)."""
    samples = len(payload) * 4
    return b"\x00\x00" * samples


def _alaw_encode(pcm: int) -> int:
    """Encode 16-bit linear PCM to A-law."""
    sign = 0
    if pcm < 0:
        sign = 0x80
        pcm = -pcm
    pcm = min(pcm, 32767)
    exponent = 0
    mask = 0x1000
    while exponent < 7 and not (pcm & mask):
        exponent += 1
        mask <<= 1
    mantissa = (pcm >> (exponent + 3)) & 0x0F
    if exponent > 0:
        byte = sign | (exponent << 4) | mantissa
    else:
        byte = sign | mantissa | 0x01
    return byte ^ 0x55


class _RTPProtocol(asyncio.DatagramProtocol):
    def __init__(self, session: RTPSession):
        self.session = session

    def connection_made(self, transport):
        self.session._transport = transport

    def datagram_received(self, data: bytes, addr: tuple):
        if len(data) < 12:
            return
        try:
            pkt = RTPPacket(data)
            if pkt.ssrc != self.session.ssrc or pkt.version != 2:
                return
            self.session.handle_incoming_rtp(pkt)
        except Exception as e:
            logger.debug("RTP parse error: {}", e)
