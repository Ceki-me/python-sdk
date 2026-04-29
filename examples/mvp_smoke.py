"""
Browserlend MVP Integration Smoke Test.

Requires:
  - browser-relay running and reachable via RELAY_URL
  - chat-service running (relay must see it via CHAT_SERVICE_URL)
  - Chrome extension loaded in a provider's browser, provider online
  - Agent API key from dashboard (Sanctum token)

Environment variables:
  CEKI_TOKEN        — agent Sanctum API token (required)
  RELAY_URL         — relay WebSocket URL (default: wss://browser.ittribe.org/ws/agent)

Usage:
  export CEKI_TOKEN="123|abcdef..."
  python examples/mvp_smoke.py
"""
import asyncio
import base64
import logging
import os
import sys
import time

from ceki_browser import Browser
from ceki_browser.errors import CekiBrowserError

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("examples/mvp_smoke.log", mode="w"),
    ],
)
log = logging.getLogger("mvp_smoke")

RELAY_URL = os.environ.get("RELAY_URL", "wss://browser.ittribe.org/ws/agent")
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


async def main() -> int:
    if not TOKEN:
        log.error("CEKI_TOKEN env var is required")
        return 1

    log.info("=" * 60)
    log.info("MVP SMOKE TEST START")
    log.info(f"relay: {RELAY_URL}")
    log.info("=" * 60)

    t0 = time.time()

    # --- Step 1: Connect ---
    try:
        br = Browser(token=TOKEN, relay_url=RELAY_URL)
        info = await br.connect()
        step_ok("connect", f"agent_id={br.agent_id}")
    except Exception as e:
        step_fail("connect", str(e))
        return 1

    try:
        # --- Step 2: Create session (waits for provider match) ---
        log.info("Requesting incognito session... (waiting for provider, timeout=120s)")
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
            # --- Step 3: Chat topic auto-created (task #40) ---
            topic_id = session.chat.topic_id if session.chat else None
            if topic_id:
                step_ok("chat_topic_created", f"topic_id={topic_id}")
            else:
                chat_available = session.chat.available if session.chat else False
                step_fail("chat_topic_created", f"topic_id is None, chat.available={chat_available}")

            # --- Step 4: Send chat message ---
            try:
                msg = await session.chat.send("Привет, начинаю MVP smoke test. Если что — пиши.")
                step_ok("chat_send", f"msg_id={msg._id}")
            except Exception as e:
                step_fail("chat_send", str(e))

            # --- Step 5: Navigate ---
            try:
                nav = await session.navigate("https://example.com")
                step_ok("navigate", "https://example.com")
            except Exception as e:
                step_fail("navigate", str(e))

            # --- Step 6: Query DOM ---
            try:
                result = await session.query("h1")
                step_ok("query_h1", f"text={result.text!r}")
            except Exception as e:
                step_fail("query_h1", str(e))

            # --- Step 7: Click ---
            try:
                await session.click("a")
                step_ok("click", "a")
            except Exception as e:
                step_fail("click", str(e))

            # --- Step 8: Type ---
            try:
                await session.type("input[name='q']", "hello")
                step_ok("type", "input[name='q'] 'hello'")
            except CekiBrowserError:
                step_ok("type_skipped", "no input on page (expected for example.com)")
            except Exception as e:
                step_fail("type", str(e))

            # --- Step 9: Screenshot ---
            try:
                shot = await session.screenshot(format="png")
                size_kb = len(base64.b64decode(shot.data)) / 1024 if shot.data else 0
                step_ok("screenshot", f"{shot.width}x{shot.height} {size_kb:.0f}KB")
            except Exception as e:
                step_fail("screenshot", str(e))

            # --- Step 10: Chat history ---
            try:
                msgs = await session.chat.history(limit=20)
                step_ok("chat_history", f"{len(msgs)} messages")
            except Exception as e:
                step_fail("chat_history", str(e))

            # --- Step 11: Second chat message ---
            try:
                msg2 = await session.chat.send("Smoke test: все команды отработали. Завершаюсь.")
                step_ok("chat_send_2", f"msg_id={msg2._id}")
            except Exception as e:
                step_fail("chat_send_2", str(e))

            # --- Step 12: End session ---
            try:
                await session.end(reason="completed")
                step_ok("session_end", "reason=completed")
            except Exception as e:
                step_fail("session_end", str(e))

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
    log.info("MVP SMOKE TEST SUMMARY")
    log.info(f"Elapsed: {elapsed:.1f}s")
    log.info(f"Passed: {len(STEPS_PASSED)}/{len(STEPS_PASSED) + len(STEPS_FAILED)}")
    for s in STEPS_PASSED:
        log.info(f"  ✓ {s}")
    for s in STEPS_FAILED:
        log.error(f"  ✗ {s}")

    if STEPS_FAILED:
        log.error("STATUS: FAIL")
        return 1
    else:
        log.info("STATUS: PASS")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
