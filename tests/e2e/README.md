# E2E Tests

Real integration tests against `browser.ittribe.org` (dev relay + live Chrome provider).

## Prerequisites

- Provider online (Konstantin's Chrome with extension v0.6.102+)
- Skill Rent Agent token

## Setup

```bash
export CEKI_API_KEY=$(cat /home/node/.openclaw/secrets/skill_rent_agent_token.txt)
export CEKI_RELAY_URL="wss://browser.ittribe.org/ws/agent"
export CEKI_API_URL="https://clawapi.ittribe.org"
export CEKI_CHAT_URL="https://chat.ittribe.org/api/chat"
```

## Run

```bash
cd python-sdk
python3 -m pytest tests/e2e/ -v -s
```

## Tests

### test_fingerprint_persistence.py

Two sequential rents. Session A exports profile with fingerprint. Session B rents with `fingerprint=profile["fingerprint"]`. Asserts:
- `navigator.userAgent` A == B
- `Intl.DateTimeFormat().resolvedOptions().timeZone` A == B
- `screen.width/height` A == B
- `navigator.hardwareConcurrency` A == B
- WebGL renderer A == B
- `Browser.getFingerprint` CDP response A == B

Cost: ~$0.02 (2 rents x 1 min x $0.10/min).

## Notes

- These tests are **not** in the default `pytest` run (skipped when `CEKI_API_KEY` is not set).
- Run before each extension release to verify fingerprint persistence works end-to-end.
