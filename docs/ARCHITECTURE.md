# eBasket score-post autopilot — Architecture (Phase A)

Status: **draft for review** (Milestone 1). Nothing here posts anywhere.
Companion docs: [SCORE_SOURCES.md](SCORE_SOURCES.md) (source shortlist + evidence),
[OPEN_DECISIONS.md](OPEN_DECISIONS.md) (what Elena / we still have to decide).

## 1. What it does

For each covered EuroLeague / Greek Basket League (GBL) game, one worker process:

1. keeps the fixture list current,
2. polls the score source only around the game window,
3. confirms an explicit `FINAL` (later: `HALFTIME`) state over a confirmation window,
4. renders a deterministic eBasket image + a Greek caption from reviewed templates,
5. validates everything,
6. hands the post to a `Publisher` — a no-op until Elena picks the platform and the live gate is passed,
7. records every step in a SQLite ledger and tells Elena about anything held, failed or ambiguous.

Uncertainty always ends in **held + alert**, never in a guess or a silent skip.

## 2. Components

```
                ┌──────────────┐  fixtures / observations
 ScoreSource ──►│   Worker     │──────────────────────────► Ledger (SQLite)
 (per comp.)    │   loop       │◄──────────────────────────  state, holds, pause flag
                └──────┬───────┘
                       │ observation history
                       ▼
                 EventDetector ──► hold(reason) ──► Notifier ──► Elena
                       │ confirmed HALFTIME/FINAL
                       ▼
      AssetCatalog ─► Renderer ─┐
      TeamCatalog  ─► CaptionBuilder ─► Validator ─► (mode gate, pause) ─► Publisher
                                                                            │
                                            NoOpPublisher (now) / platform adapter (later)
```

| Component | Responsibility | Phase A implementation |
|---|---|---|
| `ScoreSource` | fixtures, live state, period scores, freshness → normalized `Observation` | one adapter per source (chosen in SCORE_SOURCES.md); fake adapter for tests |
| `EventDetector` | pure function: observation history → `Confirmed(kind)` / `Hold(reason)` / `Wait` | no I/O, fully unit-tested |
| `TeamCatalog` | canonical team IDs, per-source ID mappings, Greek name forms | `config/teams.yaml` |
| `AssetCatalog` | canonical team ID → approved logo; explicit per-game photo; fallback | `assets/manifest.yaml`, read-only mount |
| `Renderer` | fixed template + verified data → PNG/JPEG | Pillow, template YAML per format |
| `CaptionBuilder` | fills reviewed Greek templates with verified values | strict placeholder substitution, no free text |
| `Validator` | data, assets, output, posting rules | returns list of violations; any violation ⇒ hold |
| `Publisher` | `publish(job) → PublishResult`, `reconcile(job) → PublishResult` | `NoOpPublisher`; real adapter only after Elena's decision |
| `Ledger` | durable state machine + audit trail | SQLite (WAL), numbered migrations |
| `Notifier` | holds, failures, `publish_unknown`, corrections, outages | `LogNotifier` + one real channel (TBD) |
| `Clock` | injectable time source | makes window/staleness logic testable |

Everything talks to interfaces, so the laptop and the ThinkStation run the **same image** with
different mounted config.

## 3. Normalized data model

Every source response becomes an `Observation` (the spec's record plus fetch metadata):

```json
{
  "competition": "EUROLEAGUE",
  "season": "2026-27",
  "source": "<adapter-name>",
  "source_match_id": "opaque-string",
  "start_utc": "2026-10-02T17:00:00Z",
  "home_team_id": "olympiacos",
  "away_team_id": "panathinaikos",
  "home_score": 82,
  "away_score": 79,
  "period_scores": [[20, 18], [21, 22], [19, 20], [22, 19]],
  "status": "FINAL",
  "raw_status": "result",
  "current_period": null,
  "source_asof_utc": "2026-10-02T18:58:41Z",
  "fetched_utc": "2026-10-02T18:58:44Z",
  "raw_sha256": "…"
}
```

- `status` ∈ `SCHEDULED | LIVE | HALFTIME | FINAL | POSTPONED | SUSPENDED | CANCELLED | ABANDONED | UNKNOWN`;
  `raw_status` keeps the source's own string for diagnostics.
  Adapters map only **explicit** source states. A source value the adapter does not recognize maps
  to `UNKNOWN` (→ hold), never to the nearest guess. `HALFTIME` / `FINAL` are never inferred from a clock.
- `source_match_id` and source team refs are opaque strings. Canonical `*_team_id` comes from
  `TeamCatalog`; an unmapped source team ⇒ hold.
- All timestamps stored in UTC. Display uses `zoneinfo("Europe/Athens")`. (Windows has no system tz
  database — the `tzdata` package is a hard dependency.)
- `source_asof_utc` (the spec's `source_updated_utc`) is the time the source asserts the data was current,
  e.g. response generation time. `fetched_utc − source_asof_utc` exposes stale cached copies; such
  observations don't count toward confirmation. `null` if the source offers nothing usable. A frozen
  upstream can't be seen this way — the max-game-duration hold and an optional cross-check cover it.

## 4. Event detection rules

`FINAL` is confirmed only when **all** hold across the confirmation window
(defaults, all configurable: ≥3 consecutive observations spanning ≥90 s):

- explicit final status in every observation of the window;
- identical teams (and identical to the fixture), identical scores;
- scores not tied; period scores (when present) sum to the totals;
- period count ≥ 4; `> 4` ⇒ overtime variant (`OT`, `2OT`, …);
- every observation fresh (source freshness ≤ staleness limit).

`HALFTIME` (separate enable switch): explicit halftime status, period count = 2, same consistency
rules. If the game resumes before confirmation, the event is held with reason `window_passed`
(reported in the nightly summary, not as an urgent alert).

Chronology: a `FINAL` first reported less than 70 min after scheduled tip-off (or before it), or a
`HALFTIME` less than 20 min after, is held as `implausible_timing` however consistent the source is.

Hold triggers (each with a machine-readable reason): stale source, team change vs fixture,
score decreasing or changing inside the window, `POSTPONED/CANCELLED/ABANDONED/UNKNOWN`,
no final state by `start + max_game_duration` (default 4 h), unmapped team, missing/unapproved
asset, validator failure, cross-check disagreement (if a second source is configured).

**After publication** the match is polled at low frequency for a correction window (default 2 h).
If the final score changes: alert Elena and create a *correction draft* (held) — never an automatic
second post or replacement.

## 5. Polling and restart behaviour

- Fixture refresh on start-up and every 6 h for the configured coverage window.
- Per match: idle until `start − 15 min`; slow polling pre-tip; live polling at the source's allowed
  interval (per-adapter minimum floor, config cannot go below it); stop after `FINAL` + correction window.
- On restart the worker does **not** rely on schedules having fired: it selects every covered match with
  `start_utc` in the last 24 h that has no terminal event and polls it immediately (catch-up).
- Jobs found in `publishing` at worker start-up become `publish_unknown` (we can't know if the request left).
  This runs in worker start-up only, before any publish/reconcile — never merely on opening the ledger,
  so `ebasket status` during a publish can't disturb it.
- Fixture refreshes only apply *safe* updates (filling in unmapped team IDs; a reschedule before anything
  is confirmed or held). Different teams, replaced canonical IDs, or a reschedule after progress are staged
  in `fixture_changes`, the stored fixture is left untouched, and the match's open events and pre-publish
  jobs are held in the same transaction. In-flight/published jobs are left to reconciliation/corrections.
- A heartbeat (`last_tick_utc`) is written every loop; `ebasket health` (Docker `HEALTHCHECK`) fails if it is
  older than 3× the loop interval or the DB isn't writable.

## 6. Jobs, states and duplicate safety

Job key: `competition:season:source:source_match_id:event:platform:format`
(e.g. `EUROLEAGUE:2026-27:euroleague-incrowd:E2026_12:FINAL:instagram:feed-4x5`). Unique in the ledger.
The spec's key plus the source, so two providers' match IDs can never collide (schema v2 migrated old keys).
A job's identity is derived inside the ledger from the event's stored match — never from caller input.
A second guard, the *natural key* (competition, Athens calendar day, canonical teams, event, destination),
catches the same game arriving under a new source ID (e.g. after switching score source); such a job is
created `held` with reason `possible_duplicate`.

```
event:  observed ─► confirmed                     (or held)
job:              confirmed ─► rendered ─► validated ─► ready ─► publishing ─► published
                                                          │            │
                                                          │            └─► publish_unknown ─► (reconcile) ─► published | held
                                                          └─► shadowed          (shadow mode, NoOp publisher)
any step failure ─► held(reason)
```

- Every transition is written (and committed, `synchronous=FULL`) **before** the side effect it
  guards; `publishing` is persisted before the external call.
- Ledger stores: image SHA-256, caption, template id+version, renderer version, asset-manifest hash,
  timestamps, attempts (redacted request/response excerpts, HTTP status), returned platform post ID.
- `publish_unknown` is never blindly retried: `Publisher.reconcile()` asks the platform first; if still
  uncertain → alert Elena, wait for a manual `resolve`.
- `shadowed` is a separate terminal state so shadow history can never be mistaken for a real post.
- A state name is not proof of content: the ledger refuses `rendered` without all render artifacts
  (caption, image path + SHA-256, template id/version, renderer version, asset-manifest hash), refuses to
  change them afterwards, and a manual retry clears them so the job must be rendered again. The Validator
  (M3) is the only code path to `ready`; the publisher re-checks score, assets, hash, deadline and pause
  immediately before the platform call (M4/M6).
- Late-post guard: a `ready` job older than `max_post_delay` (e.g. after a long pause) is held, not posted.

## 7. Modes and the live gate

| Mode | Detects & renders | Calls publisher | Default |
|---|---|---|---|
| `preview` | yes (stops at `ready`) | no | **yes** |
| `shadow` | yes | `NoOpPublisher` only → `shadowed` | no |
| `live` | yes | real adapter, per destination & post type | no |

Live publishing for a destination requires **all** of: `mode: live`; that destination + post type enabled
(final and halftime are separate switches); env `EBASKET_LIVE_CONFIRM` equal to the configured account
handle; `EBASKET_HOST_ID` equal to the configured `live_host` (laptop *or* ThinkStation, never both);
credentials present; no `SAMPLE`/unapproved assets in the manifest; not paused. The test suite runs with
none of these and uses fake servers only.

Placeholder assets are marked `approved: false` in the manifest **and** the renderer stamps a visible
`SAMPLE` watermark whenever one is used; the Validator refuses to move such a job past `validated`
in any mode except `preview`.

## 8. Rendering and captions

- Pillow composes a fixed template: background layer(s), logo slots, optional photo slot, text boxes.
  Template YAML per format: canvas size, safe area, element boxes, colours, font file, size and
  **minimum** size. Text is shrunk to fit its box down to the minimum; if it still doesn't fit ⇒ hold.
- Greek all-caps needs a dedicated `greek_upper()` (strip tonos, keep dialytika: `Ολυμπιακός → ΟΛΥΜΠΙΑΚΟΣ`,
  `Παναθηναϊκός → ΠΑΝΑΘΗΝΑΪΚΟΣ`); Python's `str.upper()` keeps the accents and is wrong for Greek typesetting.
- Captions: `captions/el.yaml` with final / final-OT / halftime templates. Team names come from `TeamCatalog`,
  which stores the **grammatical forms** the captions need (e.g. nominative and genitive:
  `Ολυμπιακός` / `του Ολυμπιακού`) — they are never generated.
- Output: preview PNG + final file in the destination's required format (checked: dimensions, format,
  non-empty, max file size, text fit). No generative image model anywhere in the path.
- Player photo only when an approved asset is **explicitly** linked to that match/post; otherwise the
  branded fallback. No "MVP" label without a defined, verified source and Elena's wording.

## 9. Ledger (SQLite)

Tables: `matches`, `observations` (incl. raw payload hash; raw bodies retained N days), `events`,
`jobs`, `attempts`, `transitions` (audit: from, to, reason, actor = worker | cli:<user>, at),
`notifications` (with dedupe key), `control` (paused flag, heartbeat, schema version).
WAL mode, foreign keys on, numbered SQL migrations. `ebasket backup` uses SQLite's online backup API
(not a file copy) into the backup volume; restore procedure documented in the runbook.

## 10. Operations (CLI)

| Command | Purpose |
|---|---|
| `ebasket games [--date]` | tonight's games, event and job states |
| `ebasket render <match> [--event] [--format]` | render / re-render a preview |
| `ebasket why <match\|job>` | hold reason, transitions, last observations |
| `ebasket pause` / `resume` | stop all publishing immediately (checked right before every publish call) |
| `ebasket resolve <job> --retry\|--dismiss\|--mark-published <post-id>` | manual resolution; shows current state first |
| `ebasket worker` | run the loop |
| `ebasket health` | container health check |
| `ebasket backup` | online ledger backup |
| `ebasket source-check <competition>` | probe the configured source, print a normalized sample |

## 11. Deployment and security

- Python 3.12, deps kept small: `httpx` (bounded timeouts/retries), `Pillow`, `PyYAML`, `pydantic`
  (config validation), `tzdata`. Tests: `pytest` + recorded fixtures + fake HTTP servers.
- Docker Compose, one service, non-root user, `restart: unless-stopped`, `HEALTHCHECK ebasket health`.
  Mounts: `assets/` and `config/` **read-only**; named volumes for `/data` (ledger), `/output`, `/backup`.
  **No Docker socket.** Nothing exposed on a port.
- Secrets only via env / Docker secrets (never in config, images, logs or previews). Structured JSON logs with a
  redaction filter for tokens/URL query secrets.
- Liveness alerting needs something outside the worker (a dead worker can't alert): Docker health +
  an external dead-man's-switch ping (provider TBD, see OPEN_DECISIONS).
- Fully separate from Home Assistant and from Karma: own repo, own compose project, own volumes, own credentials.

## 12. Milestones

| # | Deliverable | Exit check |
|---|---|---|
| M1 | This architecture, source shortlist with evidence, open decisions | reviewed by you |
| M2 | Skeleton, normalized model, config/team catalog, ledger + migrations, state machine, EventDetector | fixture-driven tests: transitions, OT, postponed, stale, conflicting, duplicates, restart, DB recovery |
| M3 | Renderer, CaptionBuilder, Validator with SAMPLE assets; `render` CLI | previews of both competitions: long names, halftime, final, OT; overflow + Greek tests |
| M4 | First real `ScoreSource` adapter (recorded fixtures), worker loop, catch-up, NoOp publisher, pause/why/resolve, notifier, Docker, health, backup | end-to-end run on recorded game timelines |
| M5 | Shadow run on real game nights | ≥1 game night reviewed with Elena |
| M6 | Real publisher for Elena's chosen destination (fake-server tests first), account onboarding | live gate checklist, read-back of real post IDs |
| M7 | ThinkStation transfer | restore, restart & health tested; laptop worker off before ThinkStation live |
