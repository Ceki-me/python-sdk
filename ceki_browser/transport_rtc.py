from __future__ import annotations

import asyncio
import json
import logging
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

SignalingCallback = Callable[[str, dict[str, Any]], Any]


class RTCTransport:
    def __init__(self, ice_servers: list[dict[str, Any]]):
        config = RTCConfiguration(
            iceServers=[RTCIceServer(**s) for s in ice_servers]
        )
        self.pc = RTCPeerConnection(config)
        self.cmd_channel: RTCDataChannel | None = None
        self._cmd_pending: dict[int, asyncio.Future[Any]] = {}
        self._cmd_next_id = 1
        self._signaling_callback: SignalingCallback | None = None
        self._connected_event = asyncio.Event()
        self._closed = False

        self._cmd_open_event = asyncio.Event()

        self.cmd_channel = self.pc.createDataChannel("ceki-cmd", ordered=True)

        self._setup_cmd_channel(self.cmd_channel)

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

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for fut in self._cmd_pending.values():
            if not fut.done():
                fut.cancel()
        self._cmd_pending.clear()
        await self.pc.close()

    def _setup_cmd_channel(self, channel: RTCDataChannel) -> None:
        @channel.on("open")
        def on_open() -> None:
            self._cmd_open_event.set()

        if channel.readyState == "open":
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

    async def _gather_ice(self) -> None:
        ice_done = asyncio.Event()

        @self.pc.on("icegatheringstatechange")
        def on_gather() -> None:
            if self.pc.iceGatheringState == "complete":
                ice_done.set()

        if self.pc.iceGatheringState == "complete":
            return
        await asyncio.wait_for(ice_done.wait(), timeout=10.0)
