# Open decisions

Recorded instead of guessed. "Blocks" = the first milestone that cannot finish without it
(see the milestones table in [ARCHITECTURE.md](ARCHITECTURE.md#12-milestones)). Until then, the stated
interim default applies. Interim defaults never publish anything.

## For Elena

| # | Decision | Blocks | Interim default |
|---|---|---|---|
| E1 | **Which platform and post type first?** (Instagram feed / Story / Facebook / X / …) — the most important one; it selects the first real publisher. | M6 | `NoOpPublisher`; no platform code |
| E2 | Coverage: which competitions and teams; every result, or only selected games? If selected, how are they chosen (fixed team list, per-night list)? | M4 (coverage config) | previews for all games of both competitions; nothing is published |
| E3 | Brand kit: editable template or layered exports, Greek-capable font (with a licence that allows server-side rendering), logos, colours, 3–5 examples of correct posts. | M3 final look | placeholder template stamped `SAMPLE`, never publishable |
| E4 | Image size/aspect per destination (e.g. 1080×1350 feed vs 1080×1920 story). | M3 | 1080×1350 placeholder |
| E5 | Caption examples: final, final after overtime, halftime; hashtags, mentions, emoji; how team names appear (official sponsor names vs everyday names; which grammatical forms, e.g. `του Ολυμπιακού`). | M3 | clearly marked draft templates |
| E6 | Photos: which approved assets exist, proof of permission to use them, and how a photo is linked to a specific game (or always the branded fallback). No "MVP" label unless she defines the rule and source. | M3 (fallback only until then) | branded fallback, no player photos |
| E7 | Who owns the social account(s) and who can authorize API publishing access (e.g. Meta Business portfolio / professional account). | M6 | — |
| E8 | How she wants alerts (email, Telegram, Viber, SMS…), language, and quiet hours. | M4 | alerts written to the log only |
| E9 | Latest acceptable posting delay after the final whistle (after that, hold instead of posting). | M5 | 30 min |
| E10 | Correction policy if a published score was wrong (delete + repost, edit caption, comment). System only prepares a draft and alerts; she decides. | M6 | draft + alert |
| E11 | EuroLeague halftime: no EuroLeague feed has an explicit halftime state (only an "end of period" event). Drop EuroLeague halftime posts, or accept "end-of-Q2 event + confirmation window" for this source? | halftime milestone | EuroLeague halftime unsupported |

## For us (developer), with current recommendation

| # | Decision | Recommendation | Status |
|---|---|---|---|
| D1 | Score source per competition | EuroLeague: InCrowd feed primary, live.euroleague.net cross-check. GBL: pending. See [SCORE_SOURCES.md](SCORE_SOURCES.md) | EuroLeague proposed; GBL open |
| D2 | Renderer | Pillow (deterministic, light, no browser). Revisit only if Elena's template needs effects Pillow can't reproduce. | proposed |
| D3 | Confirmation window defaults | ≥3 consecutive explicit-final observations over ≥90 s; staleness limit 3 min | proposed; tune in shadow run |
| D4 | Public image URL (only if Instagram is chosen: Meta fetches the image from a public URL) | external object store with short-lived/unguessable URLs; ThinkStation stays unexposed | waits on E1 |
| D5 | Dead-man's-switch for "worker stopped checking games" | external heartbeat ping service; provider TBD | proposed |
| D6 | Raw observation retention | 30 days, then keep hashes only | proposed |
| D7 | Terms of use of the chosen data source(s) for a media brand's commercial posting. EuroLeague terms allow "legitimate news reporting or private, non-commercial purposes", require attribution to euroleague.net, and grant no logo rights. | Elena (possibly a lawyer) decides before live; shadow run is unaffected | open |
