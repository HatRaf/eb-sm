"""Read-only feed recorder: captures how a score feed really evolves during a game.

Polls each URL at its own interval until a deadline, with conditional requests
(ETag / Last-Modified). Every poll is logged to <out>/<name>/index.jsonl with
fetch time, HTTP status and cache headers; each changed body is saved gzipped.
Stdlib only. Recordings are git-ignored: feed terms don't allow republishing.

  python tools/record_feed.py --out recordings/<run> --until 2026-09-26T21:00:00Z \
      --feed game=https://.../data.json@30 --feed comp=https://.../49913.json@60
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

USER_AGENT = "eBasket-recorder/0.1 (read-only research; github.com/HatRaf/eb-sm)"
KEEP_HEADERS = ("date", "age", "cache-control", "etag", "last-modified", "content-encoding", "x-cache", "cf-cache-status")


@dataclass
class Feed:
    name: str
    url: str
    every_s: int
    next_at: float = 0.0
    etag: str | None = None
    last_modified: str | None = None
    last_sha: str | None = None
    folder: Path = field(default=Path())


def parse_feed(spec: str) -> Feed:
    name, _, rest = spec.partition("=")
    url, _, every = rest.rpartition("@")
    if not name or not url or not every.isdigit() or int(every) < 15:
        raise argparse.ArgumentTypeError(f"expected name=url@seconds (>=15), got {spec!r}")
    return Feed(name, url, int(every))


def poll(feed: Feed) -> dict:
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    if feed.etag:
        headers["If-None-Match"] = feed.etag
    if feed.last_modified:
        headers["If-Modified-Since"] = feed.last_modified
    fetched = datetime.now(UTC)
    entry: dict = {"fetched_utc": fetched.isoformat(timespec="milliseconds")}
    try:
        with urllib.request.urlopen(urllib.request.Request(feed.url, headers=headers), timeout=15) as resp:
            status, raw, hdrs = resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as exc:
        status, raw, hdrs = exc.code, exc.read() if exc.code != 304 else b"", exc.headers
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        entry.update(status=None, error=f"{type(exc).__name__}: {exc}")
        return entry
    entry["status"] = status
    entry["headers"] = {k: hdrs.get(k) for k in KEEP_HEADERS if hdrs.get(k) is not None}
    if status != 200:
        return entry
    body = gzip.decompress(raw) if hdrs.get("content-encoding") == "gzip" else raw
    sha = hashlib.sha256(body).hexdigest()
    entry.update(bytes=len(body), sha256=sha, changed=sha != feed.last_sha)
    feed.etag, feed.last_modified = hdrs.get("etag"), hdrs.get("last-modified")
    if sha != feed.last_sha:
        name = f"{fetched.strftime('%Y%m%dT%H%M%S.%f')[:-3]}Z.body.gz"
        (feed.folder / name).write_bytes(gzip.compress(body))
        entry["file"] = name
        feed.last_sha = sha
    return entry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--until", required=True, help="UTC deadline, e.g. 2026-09-26T21:00:00Z")
    parser.add_argument("--feed", required=True, action="append", type=parse_feed)
    args = parser.parse_args(argv)
    deadline = datetime.fromisoformat(args.until.replace("Z", "+00:00")).timestamp()

    for feed in args.feed:
        feed.folder = args.out / feed.name
        feed.folder.mkdir(parents=True, exist_ok=True)
        (feed.folder / "feed.json").write_text(json.dumps({"url": feed.url, "every_s": feed.every_s}), "utf-8")

    while time.time() < deadline:
        due = min(args.feed, key=lambda f: f.next_at)
        wait = due.next_at - time.time()
        if wait > 0:
            time.sleep(min(wait, deadline - time.time()))
            continue
        entry = poll(due)
        due.next_at = time.time() + due.every_s
        with open(due.folder / "index.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        print(f"{entry['fetched_utc']} {due.name}: {entry.get('status')} "
              f"{'changed' if entry.get('changed') else ''}{entry.get('error', '')}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
