"""
Minimal SIP signaling handler for Tata Tele Smartflo integration.

Handles INVITE, ACK, BYE on UDP port 5060.
Bridges to RTP media handler for audio.
"""
import asyncio
import hashlib
import random
import socket
import time
from typing import Callable, Optional

from loguru import logger

_SIP_PORT = 5060
_RTP_PORT_RANGE = (10000, 20000)


def _generate_tag() -> str:
    return hashlib.md5(str(time.time()).encode()).hexdigest()[:8]


def _generate_branch() -> str:
    return "z9hG4bK" + hashlib.md5(str(random.random()).encode()).hexdigest()[:12]


def _generate_call_id() -> str:
    return hashlib.md5(str(time.time()).encode()).hexdigest()[:16]


def _pick_rtp_port() -> int:
    return random.randint(_RTP_PORT_RANGE[0], _RTP_PORT_RANGE[1])


def _parse_sdp(body: str) -> dict:
    """Parse SDP body to extract media info."""
    info = {"ip": None, "rtp_port": None, "codec": None, "codec_id": None, "payload_type": None}
    for line in body.strip().split("\r\n"):
        if line.startswith("c=IN IP4 "):
            info["ip"] = line.split(" ")[2].strip()
        elif line.startswith("m=audio "):
            parts = line.split(" ")
            info["rtp_port"] = int(parts[1])
            info["payload_type"] = int(parts[-1]) if len(parts) > 3 else None
        elif line.startswith("a=rtpmap:"):
            parts = line.split(" ")
            pt = int(parts[1].split(" ")[0])
            codec_info = " ".join(parts[1:])
            if "PCMU" in codec_info or "G711" in codec_info:
                info["codec"] = "PCMU"
                info["codec_id"] = 0
                info["payload_type"] = pt
            elif "PCMA" in codec_info:
                info["codec"] = "PCMA"
                info["codec_id"] = 8
                info["payload_type"] = pt
            elif "G722" in codec_info:
                info["codec"] = "G722"
                info["codec_id"] = 9
                info["payload_type"] = pt
    return info


def _build_sdp(local_ip: str, rtp_port: int) -> str:
    """Build SDP response with our RTP endpoint."""
    return (
        "v=0\r\n"
        f"o=- {int(time.time())} {int(time.time())} IN IP4 {local_ip}\r\n"
        f"s=Technopolis AI\r\n"
        f"c=IN IP4 {local_ip}\r\n"
        f"t=0 0\r\n"
        f"m=audio {rtp_port} RTP/AVP 0 8 9 101\r\n"
        f"a=rtpmap:0 PCMU/8000\r\n"
        f"a=rtpmap:8 PCMA/8000\r\n"
        f"a=rtpmap:9 G722/8000\r\n"
        f"a=rtpmap:101 telephone-event/8000\r\n"
        f"a=fmtp:101 0-16\r\n"
        f"a=ptime:20\r\n"
        f"a=sendrecv\r\n"
    )


def _parse_sip_message(data: bytes) -> dict:
    """Parse a raw SIP message into method, headers, and body."""
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\r\n")
    result = {"method": None, "uri": None, "status": None, "reason": None, "headers": {}, "body": ""}

    first_line = lines[0] if lines else ""
    parts = first_line.split(" ")

    if parts[0].startswith("SIP/"):
        result["status"] = int(parts[1]) if len(parts) > 1 else None
        result["reason"] = " ".join(parts[2:]) if len(parts) > 2 else ""
    else:
        result["method"] = parts[0]
        result["uri"] = parts[1] if len(parts) > 1 else ""

    header_lines = []
    body_start = False
    body_parts = []
    for line in lines[1:]:
        if body_start:
            body_parts.append(line)
        elif line == "":
            body_start = True
        else:
            header_lines.append(line)

    result["body"] = "\r\n".join(body_parts)

    for hl in header_lines:
        if ":" in hl:
            key, val = hl.split(":", 1)
            result["headers"][key.strip().lower()] = val.strip()

    return result


class SIPCall:
    def __init__(self, call_id: str, from_tag: str, to_tag: str, remote_ip: str, remote_port: int):
        self.call_id = call_id
        self.from_tag = from_tag
        self.to_tag = to_tag or _generate_tag()
        self.remote_ip = remote_ip
        self.remote_port = remote_port
        self.local_rtp_port = _pick_rtp_port()
        self.state = "INIT"
        self.created_at = time.time()


class SIPServer:
    def __init__(
        self,
        on_call_start: Optional[Callable] = None,
        on_call_answer: Optional[Callable] = None,
        on_call_end: Optional[Callable] = None,
        local_ip: str = "0.0.0.0",
        sip_port: int = _SIP_PORT,
    ):
        self.on_call_start = on_call_start
        self.on_call_answer = on_call_answer
        self.on_call_end = on_call_end
        self.local_ip = local_ip
        self.sip_port = sip_port
        self.active_calls: dict[str, SIPCall] = {}
        self._transport: Optional[asyncio.DatagramTransport] = None
        self._protocol: Optional[asyncio.DatagramProtocol] = None

    async def start(self):
        loop = asyncio.get_event_loop()
        self._transport, self._protocol = await loop.create_datagram_endpoint(
            lambda: _SIPProtocol(self),
            local_addr=(self.local_ip, self.sip_port),
        )
        logger.info("SIP server listening on {}:{}", self.local_ip, self.sip_port)

    async def stop(self):
        if self._transport:
            self._transport.close()

    def get_public_ip(self) -> str:
        import os
        env_ip = os.getenv("SIP_PUBLIC_IP", "").strip()
        if env_ip:
            return env_ip
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def handle_invite(self, msg: dict, addr: tuple):
        call_id = msg["headers"].get("call-id", "")
        from_header = msg["headers"].get("from", "")
        to_header = msg["headers"].get("to", "")
        via_header = msg["headers"].get("via", "")

        from_tag = ""
        if "tag=" in from_header:
            from_tag = from_header.split("tag=")[1].split(";")[0].split(">")[0].strip()

        remote_ip = addr[0]
        remote_port = addr[1]

        call = SIPCall(call_id, from_tag, "", remote_ip, remote_port)
        call.state = "INVITED"
        self.active_calls[call_id] = call

        local_ip = self.get_public_ip()
        sdp = _build_sdp(local_ip, call.local_rtp_port)

        from_tag_val = from_tag or _generate_tag()
        to_tag_val = call.to_tag

        response = (
            f"SIP/2.0 200 OK\r\n"
            f"Via: {via_header}\r\n"
            f"From: {from_header}\r\n"
            f"To: {to_header};tag={to_tag_val}\r\n"
            f"Call-ID: {call_id}\r\n"
            f"CSeq: {msg['headers'].get('cseq', '1 INVITE')}\r\n"
            f"Contact: <sip:{local_ip}:{self.sip_port}>\r\n"
            f"Content-Type: application/sdp\r\n"
            f"Content-Length: {len(sdp)}\r\n"
            f"User-Agent: Technopolis-AI/1.0\r\n"
            f"Allow: INVITE, ACK, CANCEL, BYE, OPTIONS\r\n"
            f"\r\n"
            f"{sdp}"
        )

        self._transport.sendto(response.encode(), addr)
        logger.info("SIP 200 OK sent to {}:{} call_id={}", remote_ip, remote_port, call_id[:12])

        if self.on_call_start:
            try:
                self.on_call_start(call)
            except Exception as e:
                logger.error("on_call_start callback error: {}", e)

    def handle_ack(self, msg: dict, addr: tuple):
        call_id = msg["headers"].get("call-id", "")
        if call_id in self.active_calls:
            self.active_calls[call_id].state = "ACTIVE"
            logger.info("SIP ACK received — call {} now ACTIVE", call_id[:12])
            if self.on_call_answer:
                try:
                    self.on_call_answer(self.active_calls[call_id])
                except Exception as e:
                    logger.error("on_call_answer callback error: {}", e)

    def handle_bye(self, msg: dict, addr: tuple):
        call_id = msg["headers"].get("call-id", "")
        via_header = msg["headers"].get("via", "")
        from_header = msg["headers"].get("from", "")
        to_header = msg["headers"].get("to", "")

        response = (
            f"SIP/2.0 200 OK\r\n"
            f"Via: {via_header}\r\n"
            f"From: {from_header}\r\n"
            f"To: {to_header}\r\n"
            f"Call-ID: {call_id}\r\n"
            f"CSeq: {msg['headers'].get('cseq', '1 BYE')}\r\n"
            f"Content-Length: 0\r\n"
            f"\r\n"
        )
        self._transport.sendto(response.encode(), addr)

        if call_id in self.active_calls:
            call = self.active_calls.pop(call_id)
            call.state = "ENDED"
            logger.info("SIP BYE processed — call {} ended", call_id[:12])
            if self.on_call_end:
                try:
                    self.on_call_end(call)
                except Exception as e:
                    logger.error("on_call_end callback error: {}", e)


class _SIPProtocol(asyncio.DatagramProtocol):
    def __init__(self, server: SIPServer):
        self.server = server

    def connection_made(self, transport):
        self.server._transport = transport

    def datagram_received(self, data: bytes, addr: tuple):
        try:
            msg = _parse_sip_message(data)
            method = msg["method"]
            logger.debug("SIP {} from {}:{} call_id={}", method, addr[0], addr[1],
                         msg["headers"].get("call-id", "?")[:12])

            if method == "INVITE":
                self.server.handle_invite(msg, addr)
            elif method == "ACK":
                self.server.handle_ack(msg, addr)
            elif method == "BYE":
                self.server.handle_bye(msg, addr)
            elif method == "OPTIONS":
                via = msg["headers"].get("via", "")
                response = (
                    f"SIP/2.0 200 OK\r\n"
                    f"Via: {via}\r\n"
                    f"From: {msg['headers'].get('from', '')}\r\n"
                    f"To: {msg['headers'].get('to', '')}\r\n"
                    f"Call-ID: {msg['headers'].get('call-id', '')}\r\n"
                    f"CSeq: {msg['headers'].get('cseq', '1 OPTIONS')}\r\n"
                    f"Content-Length: 0\r\n"
                    f"\r\n"
                )
                self.server._transport.sendto(response.encode(), addr)
                logger.debug("SIP 200 OK (OPTIONS) sent to {}:{}", addr[0], addr[1])
            else:
                logger.debug("SIP unhandled method {} from {}:{}", method, addr[0], addr[1])

        except Exception as e:
            logger.error("SIP protocol error from {}:{} — {}", addr[0], addr[1], e)
