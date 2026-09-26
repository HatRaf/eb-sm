"""Local operator commands. More (games, render, why, resolve, worker) arrive with M3/M4."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections import Counter
from pathlib import Path

from .ledger import Ledger


def _ledger(args: argparse.Namespace) -> Ledger:
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    return Ledger(data_dir / "ledger.sqlite")


def _actor() -> str:
    return f"cli:{getpass.getuser()}"


def cmd_init_db(args: argparse.Namespace) -> int:
    ledger = _ledger(args)
    print(f"ledger ready: {ledger.path} (schema v{ledger.schema_version()})")
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    _ledger(args).set_paused(True, _actor())
    print("PAUSED: no publish call will be made until `ebasket resume`.")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    _ledger(args).set_paused(False, _actor())
    print("resumed.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    ledger = _ledger(args)
    beat = ledger.last_heartbeat()
    print(f"paused:    {ledger.is_paused()}")
    print(f"heartbeat: {beat.isoformat() if beat else 'never'}")
    counts = Counter(job.state for job in ledger.jobs())
    print("jobs:      " + (", ".join(f"{state}={n}" for state, n in sorted(counts.items())) or "none"))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ebasket")
    parser.add_argument("--data-dir", default=os.environ.get("EBASKET_DATA_DIR", "data"))
    sub = parser.add_subparsers(dest="command", required=True)
    for name, fn, help_text in (
        ("init-db", cmd_init_db, "create or migrate the ledger"),
        ("pause", cmd_pause, "stop all publishing immediately"),
        ("resume", cmd_resume, "allow publishing again"),
        ("status", cmd_status, "pause flag, heartbeat, job counts"),
    ):
        sub.add_parser(name, help=help_text).set_defaults(fn=fn)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
