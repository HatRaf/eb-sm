# Score sources — shortlist and evidence

Checked 2026-09-26. **First pass — expect corrections** once live games have been recorded.
Every endpoint below was actually requested; values quoted from real responses. Anything not seen in a
real response is marked *unverified*. Recorded responses: [tests/fixtures/](../tests/fixtures/).

## EuroLeague

The 2026-27 season is already running: round 1 (24–25 Sep, `E2026_1…10`) is final, round 2 starts
29 Sep (first tip 16:00Z). 380 regular-season games are published.

| Candidate | Explicit FINAL | Explicit HALFTIME | Periods / OT | Caching | Role |
|---|---|---|---|---|---|
| **A. InCrowd feed** (what euroleaguebasketball.net uses) | `status:"result"` | **none** | `q1–q4`, `ot1–ot5` (null if not played) | CloudFront, `Miss` each request | **Primary** |
| B. `live.euroleague.net/api` Header / Boxscore / PlaybyPlay | `Live:false` + PBP `EG` ("End Game") | none (PBP `EP` = end of period) | Boxscore `Quarter1-4`, `Extra1..n` | Header/Boxscore `max-age=60`; PBP no-cache | Cross-check |
| C. `api-live.euroleague.net` v1/v2/v3 | only `played:true` | none | `partials`, `extraPeriods{}` | `max-age=7200`, CF `HIT` | Schedule reconciliation only |

### A. InCrowd (primary)

- Schedule: `GET https://feeds.incrowdsports.com/provider/euroleague-feeds/v2/competitions/E/seasons/E2026/games?limit=400`
  (also `?roundNumber=N`). One game: `…/seasons/E2026/games/{code}`. Play-by-play: `…/games/{code}/play-by-play`.
- Match ID: `identifier` = `"E2026_7"` (season code + game `code`); identical across A/B/C. The `id` UUID differs per feed — don't join on it.
- Teams: `home.code` / `away.code` (`OLY`, `PAN`, …), English names only (no Greek anywhere), crest URL in `imageUrls.crest`.
- Status seen in real data: `"confirmed"` (scheduled) and `"result"` (final). The site's JS enum also lists
  `fixture, live, postponed, scheduled, suspended, cancelled, walkover, planned` — *unverified in real responses*.
- Scheduled games report `score: 0` and `q1..q4: 0` (zeros, not nulls) — never trust scores unless status says so.
- OT seen: `E2025_340` `minute:"50:00"`, `ot1:13, ot2:13`; `E2025_168` 3OT `minute:"55:00"`.
- Freshness: only envelope `metadata.createdAt` (response generation time, not data update time). No ETag/Last-Modified.
  The adapter uses it as `source_asof_utc`, which exposes stale cached copies but not a frozen upstream.
- Tip-off: `date` in ISO UTC. `confirmedDate` / `confirmedTime` booleans exist (all true for 2026-27 right now).
- No rate limiting or bot challenge seen across ~50 requests.

### B. live.euroleague.net (cross-check)

`/api/Header?gamecode=7&seasoncode=E2026`, `/api/Boxscore?…`, `/api/PlaybyPlay?…`.
Traps: scores are strings; `ScoreQuarterN` are **cumulative**; all OTs merged in `ScoreExtraTime`;
`Hour` is CET/CEST with a trailing space; **before tip-off all three return HTTP 200 with an empty body**;
`last-modified` just echoes request time.

### C. api-live.euroleague.net (schedule only)

Swagger at `/swagger/v3/swagger.json` now has empty `paths` and an `ApiKey` scheme — endpoints still answer
without a key today but may be locked. Traps: `gameStatus:"Confirmed"` on played **and** unplayed games;
`winner` is the season champion, not the game winner; `standingsScore` is the regulation score. 2 h cache.

### EuroLeague consequences for the design

1. **FINAL**: publish only on A `status=="result"`, periods summing to totals, confirmed over the window;
   optionally require B `Live==false` before publishing.
2. **HALFTIME has no explicit state in any EuroLeague source.** The spec forbids inferring it from end of Q2.
   → EuroLeague halftime is *unsupported* by the adapter unless that rule is relaxed (see OPEN_DECISIONS E11).
3. Live values (`"live"`, `quarter`, `minute` during play) are unverified → record round 2 before relying on them.

### Terms of use (needs a decision before live)

Only clause found (`https://id.euroleague.net/terms`): Euroleague statistics require "a prominent attribution
to www.euroleague.net", "may only be used… for legitimate news reporting or private, non-commercial purposes",
not "in connection with any sponsorship or commercial identification", and not for live/near-live play-by-play.
No licence to logos/trademarks. None of these APIs is publicly documented. → OPEN_DECISIONS D7.

## Greek Basket League (GBL)

*Research still running — to be filled in.* EuroLeague feeds do **not** cover the current GBL
(their `GR` competition stops at 2005-06).

## Commercial feeds covering both

Docs and pricing pages only — **no API keys were used**, so every live-behaviour claim needs a free-key
check. (api-sports.io blocks automated fetches; its evidence comes from Wayback copies, Dec 2025–Apr 2026.)

| Provider | EuroLeague + GBL | Halftime / Final / after OT | Periods | Price at our volume | Role |
|---|---|---|---|---|---|
| API-Sports "API-Basketball" | both listed ("Euroleague", "A1") | `HT` / `FT` / `AOT` | Q1–Q4 + **one combined** `over_time` | PRO $15/mo: 7,500/day | Best budget candidate |
| Highlightly | both named | "Half time" / "Finished" / "Finished after over time" | q1–q4 + one combined `overTime` | $6.99/mo: 7,500/day | Budget second opinion |
| Sportradar Global Basketball | both, full live | no halftime (`pause`) / `ended` → `closed` / `aet` | each OT separate | enterprise; trial 1,000 calls/30 days | Too expensive; **holds exclusive official EuroLeague data distribution through 2031** |
| Goalserve | both named | full status list unverified | q1–q4 + one `ot` | $150/mo | Costly |
| Genius Sports (FIBA LiveStats) | GBL runs on it | `FINISHED` → `COMPLETE` (official) | regular vs OT | by permission | Only if the league grants access |
| Sofascore / Flashscore | yes (UI) | — | — | — | Manual cross-check only; terms forbid automated/commercial use |

Consequences:

- A single budget feed (API-Sports) would give an **explicit halftime** for both competitions — the only
  path so far that satisfies the spec's halftime rule for EuroLeague.
- Merged overtime means the number of OTs is unknown: the caption could say "after overtime" but not "2OT",
  and the OT consistency check can't run. The adapter would declare `per_overtime_scores=False`.
- API-Sports' terms: "We do not provide a 'license' for the use and publication of the data… permission to
  publish… must be requested… from the competent authorities." Buying a feed does not settle D7.
- Sizing: 2 competitions × ~120 polls/h × ~4 h ≈ 1,000 calls/day — fits the $15 tier, not the free 100/day.
