# Integration Smoke Tests — SDK 2.2.0+

Lifecycle integration tests against a live provider. Not for CI — requires a real browser rental environment.

## Prerequisites

- Provider online with known `BROWSER_ID` on dev relay
- Agent token with `browser:relay` ability and positive balance
- `ceki-sdk` installed (`pip install -e .` from repo root)
- Extension v0.6.74+ on provider Chrome

## Environment Variables

```bash
export CEKI_TOKEN="385|<sanctum-token>"
export CEKI_API_URL="https://api.ceki.me"
export CEKI_RELAY_URL="wss://browser.ceki.me/ws/agent"
export BROWSER_ID=240                                    # default
```

Optional (scenario H only):
```bash
export CEKI_TOKEN_NO_FUNDS="<token-of-zero-balance-user>"
```

## Scenarios

### Automatic (no manual intervention)

| ID | Name | What it tests |
|----|------|--------------|
| A  | Happy path | connect → rent → Page.navigate → title check → screenshot → close |
| B  | Auto-accept | Same as A (requires provider auto-accept enabled) |
| D  | Offer timeout | Rent with nonexistent browser — expects ProviderOffline/CekiError |
| H  | Insufficient funds | Rent with zero-balance token — expects InsufficientFunds (needs CEKI_TOKEN_NO_FUNDS) |
| I  | 10 sequential commands | 10x Runtime.evaluate in sequence |
| J  | Long navigation | Page.navigate to httpbin.org/delay/5, wait for loadEventFired |

### Manual (require provider-side action)

| ID | Name | Instructions |
|----|------|-------------|
| C  | Decline offer | Decline the offer in provider plugin when prompted |
| E  | Chrome crash | Kill Chrome on provider machine during active session |
| F  | Network drop | Disconnect provider network for ~30s, then reconnect |
| G  | Kill session | Press Kill/Stop in provider plugin during active session |

### Obsolete

| ID | Name | Reason |
|----|------|--------|
| K  | No incognito mode | `mode` parameter removed from public API in SDK 2.0+ |

## Usage

```bash
# Single scenario
python examples/smoke/mvp_smoke_v2.py --scenario A

# Multiple scenarios
python examples/smoke/mvp_smoke_v2.py --scenario A,I,J

# All automatic scenarios (manual are skipped)
python examples/smoke/mvp_smoke_v2.py --scenario all

# Manual scenario (run individually)
python examples/smoke/mvp_smoke_v2.py --scenario C
```

## Known Risks

- GitHub may show Cloudflare challenge or login wall — title check may fail. Not a SDK bug.
- httpbin.org may be slow or down — scenario J timeout is not a SDK issue.
- Provider must have auto-accept enabled for scenarios A/B to work without manual intervention.
