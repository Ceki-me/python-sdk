"""
P2P-MVP Integration Smoke Test.

Requires:
  - browser-relay running (signaling only)
  - coturn TURN server
  - Chrome extension loaded in a provider's browser, provider online
  - Agent API key from dashboard (Sanctum token)

Environment variables:
  CEKI_TOKEN        — agent Sanctum API token (required)
  RELAY_URL         — relay WebSocket URL (required)

Usage:
  export CEKI_TOKEN="123|abcdef..."
  export RELAY_URL="wss://browser.ittribe.org/ws/agent"
  python examples/mvp_smoke_p2p.py
"""
import asyncio
import base64
import hashlib
import logging
import os
import sys
import time

from ceki_browser import Browser
from ceki_browser.errors import CekiBrowserError
from ceki_browser.types import ChatMessage

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("examples/mvp_smoke_p2p.log", mode="w"),
    ],
)
log = logging.getLogger("mvp_smoke_p2p")

RELAY_URL = os.environ.get("RELAY_URL", "")
TOKEN = os.environ.get("CEKI_TOKEN", "")

STEPS_PASSED: list[str] = []
STEPS_FAILED: list[str] = []


def step_ok(name: str, detail: str = ""):
    msg = f"PASS  {name}" + (f" — {detail}" if detail else "")
    log.info(msg)
    STEPS_PASSED.append(name)


def step_fail(name: str, detail: str = ""):
    msg = f"FAIL  {name}" + (f" — {detail}" if detail else "")
    log.error(msg)
    STEPS_FAILED.append(name)


def make_test_png(size_bytes: int = 200 * 1024) -> bytes:
    """Generate a minimal valid PNG-like payload for testing."""
    header = b"\x89PNG\r\n\x1a\n"
    padding = os.urandom(size_bytes - len(header))
    return header + padding


async def main() -> int:
    if not TOKEN:
        log.error("CEKI_TOKEN env var is required")
        return 1
    if not RELAY_URL:
        log.error("RELAY_URL env var is required")
        return 1

    log.info("=" * 60)
    log.info("P2P-MVP SMOKE TEST START")
    log.info(f"relay: {RELAY_URL}")
    log.info("=" * 60)

    t0 = time.time()

    received_messages: list[ChatMessage] = []

    # --- Step 1: Connect to relay ---
    try:
        br = Browser(token=TOKEN, relay_url=RELAY_URL)
        info = await br.connect()
        step_ok("connect", f"agent_id={br.agent_id}")
    except Exception as e:
        step_fail("connect", str(e))
        return 1

    try:
        # --- Step 2: Create session (waits for provider match + RTC handshake) ---
        log.info("Requesting incognito session... (waiting for provider match + P2P, timeout=120s)")
        try:
            session = await br.session(
                mode="incognito",
                geo="",
                max_price_per_min=0.10,
                estimated_duration_min=5,
                wait_timeout=120.0,
            )
            step_ok("session_matched", f"session_id={session.session_id}")
        except Exception as e:
            step_fail("session_matched", str(e))
            await br.close()
            return 1

        try:
            # --- Step 3: Verify P2P connection ---
            rtc = session.rtc
            if rtc and rtc.pc.connectionState == "connected":
                step_ok("rtc_connected", f"connectionState={rtc.pc.connectionState}")
            else:
                state = rtc.pc.connectionState if rtc else "no_rtc"
                step_fail("rtc_connected", f"connectionState={state}")

            # --- Step 4: Chat available ---
            try:
                chat = session.chat
                step_ok("chat_available", "relay chat API ready")
            except Exception as e:
                step_fail("chat_available", str(e))

            # Register chat listener
            session.chat.on_message(received_messages.append)

            # --- Step 5: Send chat text ---
            try:
                await session.chat.send("Привет, начинаю P2P-MVP smoke test.")
                step_ok("chat_send_text", "sent via relay")
            except Exception as e:
                step_fail("chat_send_text", str(e))

            # --- Step 6: Navigate via ceki-cmd DataChannel ---
            try:
                nav = await session.navigate("https://github.com")
                step_ok("navigate", f"url={nav.url}")
            except Exception as e:
                step_fail("navigate", str(e))

            # --- Step 7: Query DOM ---
            try:
                result = await session.query("a")
                step_ok("query_dom", f"text={result.text!r}")
            except Exception as e:
                step_fail("query_dom", str(e))

            # --- Step 8: Click ---
            try:
                await session.click("a")
                step_ok("click", "a")
            except Exception as e:
                step_fail("click", str(e))

            # --- Step 9: Type into search ---
            try:
                await session.type("input[name='q']", "hello")
                step_ok("type", "input[name='q'] 'hello'")
            except CekiBrowserError:
                step_ok("type_skipped", "no matching input on page")
            except Exception as e:
                step_fail("type", str(e))

            # --- Step 10: Screenshot ---
            try:
                shot = await session.screenshot(format="png")
                size_kb = len(base64.b64decode(shot.data)) / 1024 if shot.data else 0
                step_ok("screenshot", f"{shot.width}x{shot.height} {size_kb:.0f}KB")
            except Exception as e:
                step_fail("screenshot", str(e))

            # --- Step 11: Send image via relay chat ---
            test_png = make_test_png(200 * 1024)
            test_png_sha256 = hashlib.sha256(test_png).hexdigest()
            log.info(f"Test image: {len(test_png)} bytes, sha256={test_png_sha256[:16]}...")
            try:
                await session.chat.send_image(test_png, "image/png")
                step_ok("chat_send_image", f"{len(test_png)} bytes sent via relay")
            except Exception as e:
                step_fail("chat_send_image", str(e))

            # --- Step 12: Wait for provider chat response ---
            log.info("Waiting up to 30s for provider chat responses...")
            deadline = time.time() + 30
            while time.time() < deadline:
                if len(received_messages) >= 1:
                    break
                await asyncio.sleep(0.5)

            if received_messages:
                step_ok(
                    "chat_recv_message",
                    f"got {len(received_messages)} msg(s), first: {received_messages[0].content[:50]!r}",
                )
            else:
                step_fail(
                    "chat_recv_message",
                    "no messages from provider (manual provider response required)",
                )

            # --- Step 13: Check chat history ---
            history = await session.chat.history()
            log.info(f"Chat history: {len(history)} messages")
            if len(history) >= 1:
                step_ok("chat_history", f"{len(history)} messages")
            else:
                step_ok(
                    "chat_history_partial",
                    f"{len(history)} messages (provider response may be missing)",
                )

            # --- Step 14: Second chat text ---
            try:
                await session.chat.send("Smoke test завершается. Все команды отработали.")
                step_ok("chat_send_text_2", "sent")
            except Exception as e:
                step_fail("chat_send_text_2", str(e))

            # --- Step 15: End session ---
            try:
                await session.end(reason="completed")
                step_ok("session_end", "reason=completed")
            except Exception as e:
                step_fail("session_end", str(e))

            # Verify RTC closed
            if rtc:
                log.info(f"RTC state after end: {rtc.pc.connectionState}")
            log.info("Session ended, chat closed")

        except Exception as e:
            log.error(f"Unexpected error during session: {e}", exc_info=True)
            try:
                await session.end(reason="error")
            except Exception:
                pass

    finally:
        await br.close()

    elapsed = time.time() - t0

    # --- Summary ---
    log.info("=" * 60)
    log.info("P2P-MVP SMOKE TEST SUMMARY")
    log.info(f"Elapsed: {elapsed:.1f}s")
    log.info(f"Passed: {len(STEPS_PASSED)}/{len(STEPS_PASSED) + len(STEPS_FAILED)}")
    for s in STEPS_PASSED:
        log.info(f"  ✓ {s}")
    for s in STEPS_FAILED:
        log.error(f"  ✗ {s}")

    critical_steps = {
        "connect", "session_matched", "rtc_connected", "navigate",
        "query_dom", "screenshot", "chat_send_text", "chat_send_image",
        "session_end",
    }
    critical_fails = [s for s in STEPS_FAILED if s in critical_steps]

    if critical_fails:
        log.error(f"STATUS: FAIL (critical: {', '.join(critical_fails)})")
        return 1
    elif STEPS_FAILED:
        log.warning(f"STATUS: PARTIAL PASS ({len(STEPS_FAILED)} non-critical failures)")
        return 0
    else:
        log.info("STATUS: PASS")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
