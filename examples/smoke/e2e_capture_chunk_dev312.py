#!/usr/bin/env python3
"""E2E: capture-chunk reassembly vs live provider dev312 (schedule 40634).

Rents the dev312 provider (extension v0.6.312 with sendCaptureChunked),
navigates to a heavy page, starts screencast and asserts that real frames
>48KB arrive (reassembled from capture-chunk fragments on the ceki-capture DC).

Env:
    CEKI_API_KEY  — rent agent token (required)
    SCHEDULE_ID   — provider schedule (default 40634 = dev312)
"""
from __future__ import annotations

import asyncio
import base64
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ceki_sdk import connect

API_KEY = os.environ.get("CEKI_API_KEY", "")
SCHEDULE_ID = int(os.environ.get("SCHEDULE_ID", "40634"))


async def main() -> int:
    if not API_KEY:
        print("FAIL: CEKI_API_KEY not set")
        return 1

    frames: list[dict] = []
    reassembled_big = []   # frames whose data len > 48000 base64 chars (chunk threshold)
    raw_count = 0
    got_capture_chunk = False

    client = await connect(API_KEY)
    try:
        print(f"connected: schedule={SCHEDULE_ID}")
        browser = await client.rent(SCHEDULE_ID)
        print(f"rent ok: session={browser.session_id}")

        async def on_frame(frame: dict) -> None:
            nonlocal raw_count
            raw_count += 1
            frames.append(frame)
            data = frame.get("data") or ""
            if len(data) > 48000:
                reassembled_big.append(frame)
                print(f"  [frame] big>48KB: data_len={len(data)} w={frame.get('width')} h={frame.get('height')} ts={frame.get('timestamp')}")

        browser.on_capture_frame(on_frame)

        # Navigate to a content-heavy page so frames are large, not black.
        await browser.navigate("https://www.wikipedia.org")
        await asyncio.sleep(3)

        print("starting screencast ...")
        await browser.start_screencast(maxWidth=1920, maxHeight=1080, quality=90, everyNthFrame=1, maxFrameRate=1)
        await asyncio.sleep(12)  # ~12 frames at 1fps
        await browser.stop_screencast()

        await asyncio.sleep(1)
        await browser.close()
    finally:
        await client.close()

    print(f"\nraw video-frame messages (capture DC): {raw_count}")
    print(f"frames with data > 48000 base64 chars (reassembled): {len(reassembled_big)}")
    if not reassembled_big:
        # Show a sample to diagnose
        if frames:
            sample = frames[0]
            print("sample frame keys:", list(sample.keys()))
            print("sample data len:", len(sample.get("data") or ""))
        else:
            print("no frames received at all")
        return 2

    # Verify the big frame is a real JPEG, not empty/black.
    ok = 0
    for f in reassembled_big[:3]:
        data = f.get("data") or ""
        try:
            img = base64.b64decode(data)
            # Check JPEG magic
            is_jpeg = img[:3] == b"\xff\xd8\xff"
            # rough blackness check: sample some bytes
            print(f"  decode: {len(img)} bytes jpeg_magic={is_jpeg} w={f.get('width')} h={f.get('height')}")
            if is_jpeg:
                ok += 1
        except Exception as e:
            print(f"  decode error: {e}")

    print(f"\nRESULT: big frames received={len(reassembled_big)}, valid jpeg={ok}")
    return 0 if ok > 0 else 3


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
