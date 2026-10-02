"""Browser Vault roundtrip — save a session snapshot to the vault and restore it.

Requires: a user Sanctum token (the vault endpoints resolve to a user).
Point CEKI_API_URL at your API environment (defaults to https://api.ceki.me).

Two modes:
  1. ``export-envelope`` — read a locally exported profile.json and push it to
     the vault (no live browser needed).
  2. ``save`` — rent a browser, export its state, POST/PUT to the vault.
  3. ``rent-with-vault`` — rent a browser and restore a vault session by id.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from ceki_sdk import ConnectOptions, connect


def _raw_arg() -> str:
    key = os.environ.get("CEKI_API_KEY")
    if not key:
        print("CEKI_API_KEY not set", file=sys.stderr)
        sys.exit(2)
    return key


async def cmd_export_envelope(file: str, label: str) -> None:
    with open(file) as f:
        profile = json.load(f)
    client = await connect(_raw_arg(), ConnectOptions(reconnect=False))
    try:
        # If the file is already a vault envelope (has a "data" key), pass data
        # as-is; otherwise normalize a flat profile.export() blob.
        if isinstance(profile, dict) and "data" in profile:
            envelope = profile["data"]
        else:
            from ceki_sdk._vault import normalize_profile_for_vault

            envelope = normalize_profile_for_vault(profile)
        vid = await client.vault.create(envelope, label=label)
        print(f"created vault session {vid}")
    finally:
        await client.disconnect()


async def cmd_save(schedule_id: int) -> None:
    client = await connect(_raw_arg(), ConnectOptions(reconnect=False))
    try:
        browser = await client.rent(schedule_id)
        await browser.send({"method": "Page.navigate", "params": {"url": "https://vc.ru"}})
        await asyncio.sleep(3)
        vid = await browser.vault.save(label="vc.ru snapshot")
        print(f"saved vault session {vid}")
        await browser.close()
    finally:
        await client.disconnect()


async def cmd_rent_vault(schedule_id: int, vault_id: int) -> None:
    client = await connect(_raw_arg(), ConnectOptions(reconnect=False))
    try:
        browser = await client.rent(schedule_id, vault=vault_id)
        print(f"rented {browser.session_id} with vault {vault_id}")
        # cookies are applied; navigate to trigger per-origin storage flush
        await browser.send({"method": "Page.navigate", "params": {"url": "https://vc.ru"}})
        await asyncio.sleep(3)
        await browser.close()
    finally:
        await client.disconnect()


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("export-envelope")
    pe.add_argument("file", help="path to an exported profile.json")
    pe.add_argument("--label", default="Snapshot")

    ps = sub.add_parser("save")
    ps.add_argument("--schedule", type=int, required=True)

    pr = sub.add_parser("rent-with-vault")
    pr.add_argument("--schedule", type=int, required=True)
    pr.add_argument("--vault", type=int, required=True)

    args = p.parse_args()
    if args.cmd == "export-envelope":
        await cmd_export_envelope(args.file, args.label)
    elif args.cmd == "save":
        await cmd_save(args.schedule)
    elif args.cmd == "rent-with-vault":
        await cmd_rent_vault(args.schedule, args.vault)


if __name__ == "__main__":
    asyncio.run(main())