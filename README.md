# eBasket score-post autopilot

Verifies EuroLeague / Greek Basket League results and prepares eBasket-branded score posts.
**Default mode is preview: nothing is ever published** unless the live gate in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#7-modes-and-the-live-gate) is passed.

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — design, state machine, milestones
- [docs/SCORE_SOURCES.md](docs/SCORE_SOURCES.md) — data sources and evidence
- [docs/OPEN_DECISIONS.md](docs/OPEN_DECISIONS.md) — what Elena and we still need to decide

## Develop

```sh
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # Linux/macOS: .venv/bin/python
.venv/Scripts/python -m pytest
```

Tests use recorded responses in `tests/fixtures/` only — they never contact a score source or a social platform.

## Operate (so far)

```sh
ebasket init-db      # create / migrate the ledger in ./data (EBASKET_DATA_DIR)
ebasket pause        # stop all publishing immediately
ebasket resume
ebasket status       # pause flag, heartbeat, job counts
```
