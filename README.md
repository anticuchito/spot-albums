# spot-albums

Turn your real Spotify listening history into a **prioritised album wantlist**
for a portable music player.

*[Léeme en español](README.es.md)*

The hard part isn't copying files. It's deciding **which albums earn one of the
~650 slots** that fit in 256 GB of FLAC. You can have 400 hours of an artist and
want none of their albums, because you only ever play the same two singles.
This tells those apart.

> **This does not download music.** It produces a list of what to look for and
> why. Getting the files is on you.

---

## What it actually does

It reads your listening history — both the GDPR export (years of plays with
millisecond-level playback times) and the live API — and scores **albums**, not
tracks, on five signals:

| Signal | Weight | What it measures |
|---|---|---|
| **volume** | 33% | hours accumulated, log-scaled |
| **breadth** | 30% | distinct tracks heard ÷ tracks on the album |
| **recency** | 17% | exponential decay, 6-month half-life |
| **intent** | 10% | not-skipped + sequential play without shuffle |
| **confirmation** | 10% | appears in your current tops, or you saved it |

**Breadth is the signal that justifies the whole project.** Two albums with
identical hours can be opposite things: one you play front to back, and one
where you know the single that showed up in a playlist. Only the first deserves
350 MB on the card.

Output is a self-contained HTML report (no JS, no CDN — opens by double-click,
works offline) plus a CSV wantlist with per-album search links to Bandcamp,
Qobuz and Discogs.

---

## Quick start

### 1. Request your export first — it takes days

At <https://www.spotify.com/account/privacy>: **untick** "Account data", **tick**
"Extended streaming history", request. **You must click Confirm in the follow-up
email** or nothing happens. Official estimate 30 days; in practice 1–5.

Take the right box: "Account data" only has ~1 year. You want the extended one.

### 2. Create a Spotify app

At <https://developer.spotify.com/dashboard>, create an app and add this exact
redirect URI:

```
http://127.0.0.1:8888/callback
```

It must be the literal IP. Since Nov 2025 Spotify only accepts HTTPS except for
loopback, and `localhost` no longer counts. No client secret needed — the flow
is PKCE.

### 3. Run it

```bash
uv sync
uv run spot-albums auth --client-id <YOUR_CLIENT_ID>

uv run spot-albums pull                        # snapshot of your account, works today
uv run spot-albums ingest ~/Downloads/my_spotify_data.zip   # or the unzipped folder
uv run spot-albums enrich                      # resolve albums — one batch (see below)
uv run spot-albums report                      # HTML + wantlist in ./out
```

Other commands: `quota` (can I run another batch?), `status` (what's in the
database), `analyze` (ranking in the terminal), `devices` (storage profiles).

### Resolving runs in batches, across days

A development-mode app gets roughly **600 requests per day** — measured, not
documented — counted per request, not per second. `enrich` therefore does 500
albums per run and stops.

That's fine, because albums are resolved most-listened first: each batch adds
the next most important ones, and a wantlist for a 256 GB card only needs about
650 albums. Two or three batches is plenty.

```bash
uv run spot-albums quota     # can I go again?
uv run spot-albums enrich    # next 500
```

Don't poll in a loop while blocked — every probe spends from the same budget
and can renew the block. For a bigger allowance, use the **Request Extension**
link on your app's page in the Developer Dashboard.

---

## Device profiles

Storage budget and quirks come from a profile, so this works with any player:

```bash
uv run spot-albums devices                  # list them
SPOT_ALBUMS_DEVICE=generic-512 uv run spot-albums report
```

Built in: `snowsky-echo-mini`, `generic-128/256/512`, `unlimited`. Adding yours
is a few lines in `devices.py` — PRs welcome.

---

## Things worth knowing before you build on Spotify data

Full write-up in **[FINDINGS.md](FINDINGS.md)**; the software rationale is in **[DESIGN.md](DESIGN.md)**. The short version, because each
of these cost a real debugging session:

- **`audio_features`, `recommendations` and `related-artists` are dead** since
  Nov 2024, permanently, no replacement. Taste analysis has to come from
  behaviour now.
- **`/playlists/{id}/items` renamed its `track` key to `item`** in Feb 2026.
  Old code returns **zero tracks with HTTP 200 and no error**. We shipped that
  bug.
- **The export's `skipped` field recorded nothing between 2015-04-13 and
  2022-10-16.** Derive skips from `ms_played` instead.
- **Resolving track-by-track exhausts the daily quota** and earns a 23-hour
  `Retry-After`. The export already has album names, so you only need one
  request per *album*: ~57,000 → ~1,000.
- **One album exists under many IDs.** Without consolidation the evidence
  splits across editions and breadth saturates at 100%.

---

## Status

Ingestion, scoring, report and wantlist are complete and tested (57 tests).

Writing the library to the player (`prep` / `sync`) is designed but not built —
it makes sense to write it once you have actual files. It will have to handle
the Echo Mini's documented quirks: no M3U support, unreliable alphabetical
sorting, and duplicated artists when `albumartist` tags are inconsistent.

## Development

```bash
uv sync --group dev
uv run pytest
```

The test fixture is a synthetic GDPR export with three deliberately different
profiles — an album played whole, a single on loop, an old favourite — all with
the same accumulated hours. The tests assert the ranking separates them
correctly, that ingestion is idempotent, and that skips are detected despite the
`skipped` bug.

## License

MIT — see [LICENSE](LICENSE).
