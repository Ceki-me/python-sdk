from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from aiortc import (
    RTCConfiguration,
    RTCDataChannel,
    RTCIceCandidate,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)

from .errors import CekiBrowserError, CommandTimeout

logger = logging.getLogger("ceki_browser")

CHUNK_SIZE = 12 * 1024
MAX_IMAGE_SIZE = 5 * 1024 * 1024
MAX_HISTORY = 200
MAX_IMAGE_MEMORY = 50 * 1024 * 1024
ASSEMBLER_TIMEOUT = 30.0


@dataclass
class ChatImage:
    id: str = ""
    from_: str = ""
    ts: float = 0
    mime: str = ""
    data: bytes = b""
    preview_b64: str | None = None


@dataclass
class ChatTextMessage:
    id: str = ""
    from_: str = ""
    ts: float = 0
    text: str = ""


@dataclass
class _ImageAssembler:
    id: str
    from_: str
    ts: float
    mime: str
    size_bytes: int
    total_chunks: int
    preview_b64: str
    chunks: list[str | None] = field(default_factory=list)
    received: int = 0
    timer: asyncio.TimerHandle | None = None


SignalingCallback = Callable[[str, dict[str, Any]], Any]


class RTCTransport:
    def __init__(self, ice_servers: list[dict[str, Any]]):
        config = RTCConfiguration(
            iceServers=[RTCIceServer(**s) for s in ice_servers]
        )
        self.pc = RTCPeerConnection(config)
        self.cmd_channel: RTCDataChannel | None = None
        self.chat_channel: RTCDataChannel | None = None
        self._cmd_pending: dict[int, asyncio.Future[Any]] = {}
        self._cmd_next_id = 1
        self._chat_text_handlers: list[Callable[[ChatTextMessage], Any]] = []
        self._chat_image_handlers: list[Callable[[ChatImage], Any]] = []
        self._signaling_callback: SignalingCallback | None = None
        self._connected_event = asyncio.Event()
        self._closed = False

        self._chat_history: list[ChatTextMessage | ChatImage] = []
        self._assemblers: dict[str, _ImageAssembler] = {}
        self._total_image_bytes = 0

        self._cmd_open_event = asyncio.Event()
        self._chat_open_event = asyncio.Event()

        self.cmd_channel = self.pc.createDataChannel("ceki-cmd", ordered=True)
        self.chat_channel = self.pc.createDataChannel("ceki-chat", ordered=True)

        self._setup_cmd_channel(self.cmd_channel)
        self._setup_chat_channel(self.chat_channel)

        @self.pc.on("icecandidate")
        def on_ice(candidate: RTCIceCandidate | None) -> None:
            if candidate and self._signaling_callback:
                self._signaling_callback("webrtc.ice", {
                    "candidate": candidate.to_sdp(),
                    "sdpMid": candidate.sdpMid,
                    "sdpMLineIndex": candidate.sdpMLineIndex,
                })

        @self.pc.on("connectionstatechange")
        def on_state() -> None:
            state = self.pc.connectionState
            logger.info("RTC connection state: %s", state)
            if state == "connected":
                self._connected_event.set()
                if self._signaling_callback:
                    self._signaling_callback("webrtc.connected", {})
            elif state in ("failed", "closed"):
                self._connected_event.set()

    def on_signaling(self, callback: SignalingCallback) -> None:
        self._signaling_callback = callback

    def on_chat_message(self, callback: Callable[[ChatTextMessage], Any]) -> None:
        self._chat_text_handlers.append(callback)

    def on_chat_image(self, callback: Callable[[ChatImage], Any]) -> None:
        self._chat_image_handlers.append(callback)

    @property
    def chat_history(self) -> list[ChatTextMessage | ChatImage]:
        return list(self._chat_history)

    async def create_offer(self) -> dict[str, Any]:
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)

        await self._gather_ice()

        desc = self.pc.localDescription
        return {"type": desc.type, "sdp": desc.sdp}

    async def apply_answer(self, sdp: dict[str, Any]) -> None:
        answer = RTCSessionDescription(sdp=sdp["sdp"], type=sdp["type"])
        await self.pc.setRemoteDescription(answer)

    async def add_ice(self, candidate_data: dict[str, Any]) -> None:
        candidate_str = candidate_data.get("candidate", "")
        if not candidate_str:
            return
        sdp_mid = candidate_data.get("sdpMid", "0")
        sdp_mline = candidate_data.get("sdpMLineIndex", 0)
        if candidate_str.lstrip().startswith("{"):
            try:
                obj = json.loads(candidate_str)
            except json.JSONDecodeError:
                logger.debug("addIceCandidate: malformed JSON, skipping")
                return
            candidate_str = obj.get("candidate", "") or ""
            sdp_mid = obj.get("sdpMid", sdp_mid)
            sdp_mline = obj.get("sdpMLineIndex", sdp_mline)
            if not candidate_str:
                return
        from aiortc.sdp import candidate_from_sdp
        sdp = candidate_str
        if sdp.startswith("candidate:"):
            sdp = sdp[len("candidate:"):]
        try:
            candidate = candidate_from_sdp(sdp)
        except Exception as exc:
            logger.debug("addIceCandidate: failed to parse SDP %r: %s", sdp, exc)
            return
        candidate.sdpMid = sdp_mid
        candidate.sdpMLineIndex = sdp_mline
        await self.pc.addIceCandidate(candidate)

    async def wait_connected(self, timeout: float = 30.0) -> None:
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise CekiBrowserError("WebRTC connection timed out")
        if self.pc.connectionState != "connected":
            raise CekiBrowserError(f"WebRTC connection failed: {self.pc.connectionState}")
        if self.cmd_channel and self.cmd_channel.readyState != "open":
            try:
                await asyncio.wait_for(self._cmd_open_event.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                raise CekiBrowserError("Command DataChannel did not open after RTC connect")

    async def send_command(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> Any:
        if not self.cmd_channel or self.cmd_channel.readyState != "open":
            raise CekiBrowserError("Command DataChannel not open")

        msg_id = self._cmd_next_id
        self._cmd_next_id += 1

        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": msg_id}
        if params:
            payload["params"] = params

        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._cmd_pending[msg_id] = fut

        self.cmd_channel.send(json.dumps(payload))

        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._cmd_pending.pop(msg_id, None)
            raise CommandTimeout(f"Command {method} timed out after {timeout}s", code=-1020)

    async def send_chat_text(self, text: str) -> None:
        if not self.chat_channel or self.chat_channel.readyState != "open":
            raise CekiBrowserError("Chat DataChannel not open")

        msg = {
            "type": "msg",
            "id": str(uuid.uuid4()),
            "from": "agent",
            "ts": int(time.time() * 1000),
            "text": text,
        }
        self.chat_channel.send(json.dumps(msg))
        self._add_to_history(ChatTextMessage(
            id=msg["id"], from_=msg["from"], ts=msg["ts"], text=text,
        ))

    async def send_chat_image(
        self,
        data: bytes | str | Path,
        mime: str | None = None,
    ) -> None:
        if not self.chat_channel or self.chat_channel.readyState != "open":
            raise CekiBrowserError("Chat DataChannel not open")

        if isinstance(data, (str, Path)):
            path = Path(data)
            raw = path.read_bytes()
            if mime is None:
                ext = path.suffix.lower()
                _ext_map = {
                    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif",
                }
                mime = _ext_map.get(ext, "image/png")
        else:
            raw = data
            if mime is None:
                mime = "image/png"

        if len(raw) > MAX_IMAGE_SIZE:
            raw = self._try_downscale(raw, mime)
            mime = "image/jpeg"
            if len(raw) > MAX_IMAGE_SIZE:
                raise ValueError(f"Image too large ({len(raw)} bytes > {MAX_IMAGE_SIZE}), even after downscale attempt")

        b64 = base64.b64encode(raw).decode("ascii")
        total_chunks = (len(b64) + CHUNK_SIZE - 1) // CHUNK_SIZE
        img_id = str(uuid.uuid4())
        ts = int(time.time() * 1000)

        self.chat_channel.send(json.dumps({
            "type": "img-start",
            "id": img_id,
            "from": "agent",
            "ts": ts,
            "mime": mime,
            "size_bytes": len(raw),
            "total_chunks": total_chunks,
        }))

        for i in range(total_chunks):
            chunk = b64[i * CHUNK_SIZE:(i + 1) * CHUNK_SIZE]
            self.chat_channel.send(json.dumps({
                "type": "img-chunk",
                "id": img_id,
                "seq": i,
                "data": chunk,
            }))

        self.chat_channel.send(json.dumps({"type": "img-end", "id": img_id}))

        self._enforce_memory_cap(len(raw))
        self._total_image_bytes += len(raw)
        self._add_to_history(ChatImage(
            id=img_id, from_="agent", ts=ts, mime=mime, data=raw,
        ))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for asm in self._assemblers.values():
            if asm.timer:
                asm.timer.cancel()
        self._assemblers.clear()
        for fut in self._cmd_pending.values():
            if not fut.done():
                fut.cancel()
        self._cmd_pending.clear()
        self._chat_history.clear()
        self._total_image_bytes = 0
        await self.pc.close()

    def _setup_cmd_channel(self, channel: RTCDataChannel) -> None:
        @channel.on("open")
        def on_open() -> None:
            self._cmd_open_event.set()

        @channel.on("message")
        def on_message(data: str | bytes) -> None:
            try:
                msg = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                return

            msg_id = msg.get("id")
            if msg_id is not None and msg_id in self._cmd_pending:
                fut = self._cmd_pending.pop(msg_id)
                if "error" in msg:
                    err = msg["error"]
                    fut.set_exception(CekiBrowserError(
                        err.get("message", "Unknown error"),
                        code=err.get("code", 0),
                    ))
                else:
                    fut.set_result(msg.get("result"))

    def _setup_chat_channel(self, channel: RTCDataChannel) -> None:
        @channel.on("open")
        def on_open() -> None:
            self._chat_open_event.set()

        @channel.on("message")
        def on_message(data: str | bytes) -> None:
            try:
                msg = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                return
            msg_type = msg.get("type")
            if msg_type == "msg":
                self._handle_chat_text(msg)
            elif msg_type == "img-start":
                self._handle_img_start(msg)
            elif msg_type == "img-chunk":
                self._handle_img_chunk(msg)
            elif msg_type == "img-end":
                self._handle_img_end(msg)

    def _handle_chat_text(self, msg: dict[str, Any]) -> None:
        cm = ChatTextMessage(
            id=msg.get("id", ""),
            from_=msg.get("from", ""),
            ts=msg.get("ts", 0),
            text=msg.get("text", ""),
        )
        self._add_to_history(cm)
        for h in self._chat_text_handlers:
            try:
                h(cm)
            except Exception:
                logger.exception("Error in chat text handler")

    def _handle_img_start(self, msg: dict[str, Any]) -> None:
        img_id = msg.get("id", "")
        if img_id in self._assemblers:
            return
        total = msg.get("total_chunks", 0)
        loop = asyncio.get_event_loop()
        timer = loop.call_later(ASSEMBLER_TIMEOUT, self._assembler_timeout, img_id)
        self._assemblers[img_id] = _ImageAssembler(
            id=img_id,
            from_=msg.get("from", ""),
            ts=msg.get("ts", 0),
            mime=msg.get("mime", "image/png"),
            size_bytes=msg.get("size_bytes", 0),
            total_chunks=total,
            preview_b64=msg.get("preview_b64", ""),
            chunks=[None] * total,
            timer=timer,
        )

    def _handle_img_chunk(self, msg: dict[str, Any]) -> None:
        asm = self._assemblers.get(msg.get("id", ""))
        if not asm:
            return
        seq = msg.get("seq", 0)
        if 0 <= seq < len(asm.chunks) and asm.chunks[seq] is None:
            asm.chunks[seq] = msg.get("data", "")
            asm.received += 1

    def _handle_img_end(self, msg: dict[str, Any]) -> None:
        img_id = msg.get("id", "")
        asm = self._assemblers.pop(img_id, None)
        if not asm:
            return
        if asm.timer:
            asm.timer.cancel()

        b64 = "".join(c or "" for c in asm.chunks)
        try:
            raw = base64.b64decode(b64)
        except Exception:
            logger.warning("Failed to decode image %s", img_id)
            return

        self._enforce_memory_cap(len(raw))
        self._total_image_bytes += len(raw)

        img = ChatImage(
            id=asm.id,
            from_=asm.from_,
            ts=asm.ts,
            mime=asm.mime,
            data=raw,
            preview_b64=asm.preview_b64 or None,
        )
        self._add_to_history(img)
        for h in self._chat_image_handlers:
            try:
                h(img)
            except Exception:
                logger.exception("Error in chat image handler")

    def _assembler_timeout(self, img_id: str) -> None:
        asm = self._assemblers.pop(img_id, None)
        if asm:
            logger.warning("Image assembler timeout: %s (%d/%d chunks)", img_id, asm.received, asm.total_chunks)

    def _add_to_history(self, item: ChatTextMessage | ChatImage) -> None:
        self._chat_history.append(item)
        if len(self._chat_history) > MAX_HISTORY:
            removed = self._chat_history.pop(0)
            if isinstance(removed, ChatImage):
                self._total_image_bytes -= len(removed.data)

    def _enforce_memory_cap(self, incoming: int) -> None:
        while self._total_image_bytes + incoming > MAX_IMAGE_MEMORY and self._chat_history:
            idx = next((i for i, m in enumerate(self._chat_history) if isinstance(m, ChatImage)), -1)
            if idx == -1:
                break
            removed = self._chat_history.pop(idx)
            if isinstance(removed, ChatImage):
                self._total_image_bytes -= len(removed.data)
            logger.warning("Dropped old image to stay under %dMB cap", MAX_IMAGE_MEMORY // (1024 * 1024))

    async def _gather_ice(self) -> None:
        ice_done = asyncio.Event()

        @self.pc.on("icegatheringstatechange")
        def on_gather() -> None:
            if self.pc.iceGatheringState == "complete":
                ice_done.set()

        if self.pc.iceGatheringState == "complete":
            return
        await asyncio.wait_for(ice_done.wait(), timeout=10.0)

    @staticmethod
    def _try_downscale(raw: bytes, mime: str) -> bytes:
        try:
            import io

            from PIL import Image
            img = Image.open(io.BytesIO(raw))
            max_edge = 1280
            w, h = img.size
            scale = max_edge / max(w, h)
            if scale < 1:
                img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            return buf.getvalue()
        except ImportError:
            return raw
