# Design notes

Why this project is built the way it is. Several of these decisions were made
*after* getting them wrong first — those are the ones worth reading, so they're
marked and explained rather than quietly corrected.

For the Spotify API and export specifics, see [FINDINGS.md](FINDINGS.md). This
document is about the software.

---

## The problem shapes everything

The goal is not "analyse my listening history." It's:

> Which ~650 albums earn a slot on a 256 GB card?

That framing drives every decision below. It means the unit of work is the
**album**, not the track. It means precision matters more than recall — a
wantlist with 600 good entries beats one with 3,000 unranked ones. And it means
the output is a **document you read while shopping**, not a dashboard.

The naive version of this project is a script that dumps your top 50 artists.
That's not useful, because you already know your top 50 artists. What you don't
know is *which of their records you actually play end to end*.

---

## Runtime: Python

The workload is JSON parsing, grouping, and arithmetic over a few hundred
thousand rows. Python handles that comfortably — ingesting 250 MB of export
JSON takes about 3 seconds.

The bottleneck was never CPU. It was Spotify's network latency, at roughly
270 ms per request. A runtime fifty times faster would have finished in exactly
the same wall-clock time.

The standard library also happens to cover almost everything this needs:
`sqlite3`, `json`, `zipfile`, `http.server` (for the OAuth loopback),
`hashlib`, `secrets`, `base64`, `unicodedata`, `csv`, `math`, `re`.

**When something else would win:** if this ran on the player itself, or
processed many users' histories server-side. Neither applies.

---

## Dependencies: one

2,759 lines of source, **one runtime dependency** (`httpx`). That's a policy,
not an accident.

| Tempting | Why it was declined |
|---|---|
| `pandas` | The data is already in SQLite. `GROUP BY` is free there; this would add tens of megabytes to reimplement it. |
| `spotipy` or similar | **This would have been actively worse.** Third-party wrappers lag API changes. Spotify renamed `track` → `item` in playlist responses in Feb 2026 and wrappers were still catching up. Hand-writing the client means a break is visible and fixable immediately, rather than blocked on someone else's PR. |
| `matplotlib` | Produces raster images. The report needs inline SVG (see below). |
| `rich` | Pretty terminal output for a tool whose real output is a file. |

The real cost of a dependency isn't installation — it's the layer it puts
between you and the problem at exactly the moment something strange happens.
In this project, everything interesting was something strange.

`httpx` earns its place: correct HTTP/2, timeouts, and connection reuse are not
worth reimplementing.

---

## Storage: SQLite

Considered: loose JSON/Parquet files, or Postgres.

SQLite wins because the pipeline has **five stages that run at different times,
in separate invocations, and every one must be resumable.** `enrich` can die
halfway — it did — and the work already paid for must survive. With flat files
you write that yourself. Here it's a `commit`.

It also serves as a permanent cache. Spotify's catalogue doesn't change, so a
resolved track stays resolved forever. `UPSERT` and `UNIQUE` give idempotency
for free: re-ingesting the same export inserts nothing.

And it needs no server. The whole state is one file you can copy, delete, or
inspect with the `sqlite3` CLI.

### Foreign keys are deliberately asymmetric

SQLite ignores foreign keys unless you issue `PRAGMA foreign_keys = ON` per
connection. We do — and turning it on immediately caught a real bug (locally
uploaded MP3s were skipped from the `tracks` cache but still inserted into
`playlist_tracks`).

But not every relationship should be one. Three distinct cases:

**Declared and enforced** — `tracks→albums`, `saved_albums→albums`,
`playlist_tracks→tracks`, `top_items→albums`. The write path always caches the
parent first, so the constraint holds and catches ordering mistakes.

**Deliberately absent: `plays.track_id`.** Evidence is ingested *before* the
catalogue is resolved, in a separate networked pass. A foreign key here would
make `ingest` fail outright on a fresh database. Orphans are a legitimate,
temporary state — `pending_albums()` uses them as the work queue.

**Deliberately absent: `*.artist_id`.** The `artists` table only holds what
`/me/top/artists` returns — about 109 rows against 562 albums. It's a partial
cache, not a catalogue. Enforcing the key would reject most of the library.

All three cases are documented in the schema itself, and pinned by tests in
`test_schema.py`, so nobody "fixes" the second and third by adding constraints.

---

## Pipeline

```
Spotify API ─┐
             ├─→ [ingest] ─→ SQLite ─→ [enrich] ─→ [analyze] ─→ [report]
GDPR export ─┘                                                      │
                                                            wantlist.csv
```

Each stage is a separate subcommand, idempotent, and safe to re-run. That
matters because they have wildly different costs: `ingest` is seconds of local
CPU, `enrich` is tens of minutes of rate-limited network, `report` is
instantaneous. Fusing them would mean paying the network cost every time you
want to re-render a chart.

### The central pivot: resolve albums, not tracks

**This was wrong first, and the correction is the most important thing in the
codebase.**

The original design: for each distinct track in the history, call
`GET /tracks/{id}` to learn its album. Twelve years of history is ~57,000
requests, and Spotify removed batch endpoints in Feb 2026.

That exhausts the daily quota for a development-mode app. The response is
`429` with `Retry-After: 82661` — 23 hours.

But quota was the symptom. The actual mistake was **asking the wrong question.**
The export already contains `master_metadata_album_album_name` on every play.
Grouping by (artist, album), summing time, and counting distinct tracks needs
**no network at all.**

The only thing the API contributes is `total_tracks` — the denominator for
breadth — and that is one fact *per album*, not per track.

So: group locally, then resolve **one representative track per album** (the
most-played one, most likely to still be in the catalogue). Because the export
carries `spotify_track_uri`, this is exact — no fuzzy title search.

**~57,000 requests → ~1,000.** Same information, and more accurate.

Two supporting details:

- Work is ordered by listening time descending, so an interrupted run leaves
  the albums that matter resolved rather than an arbitrary slice.
- `link_from_cache()` resolves groups from tracks `pull` already fetched,
  spending no requests at all.

**The generalisable lesson:** when something scales badly, the first question
isn't "how do I make this faster?" but *"why am I asking for so much?"*
Parallelising would have hit the wall sooner.

### Fail loudly, never silently

Two related fixes came out of the same incident:

`time.sleep(retry_after)` looks obviously correct — the server says how long to
wait, so you wait. With a 23-hour value it parks the process for a day while
looking healthy. Backoff is now capped; beyond the cap it raises `RateLimited`
carrying the retry window.

And `print()` to a pipe is block-buffered. The rate-limit messages sat unseen in
a 4 KB buffer, which made a throttled process look idle. Anything you'll debug
from needs `flush=True`.

The second one caused more damage than the first: reasoning from *absent* log
output produced a confident, wrong conclusion about what was happening.

---

## Scoring

### Behaviour, not audio characteristics

Not a design preference — a constraint. Spotify removed `audio_features`,
`audio_analysis`, `recommendations` and `related-artists` in Nov 2024,
permanently. Any tutorial suggesting you build a taste profile from
`energy`/`valence`/`danceability` describes a dead API.

It turned out better. Whether a record is "energetic" says nothing about whether
you want it on your player. That you played it for 82 hours, front to back,
says a great deal.

### Five signals

| Signal | Weight | Measures |
|---|---|---|
| volume | 33% | accumulated hours, log-scaled |
| **breadth** | 30% | distinct tracks heard ÷ album length |
| recency | 17% | exponential decay, 6-month half-life |
| intent | 10% | not-skipped + sequential play without shuffle |
| confirmation | 10% | present in current tops, or saved |

**Breadth carries the project.** It is the only signal that separates "I play
this record" from "I know the single that appeared in a playlist." Two albums
with identical hours can be opposite things, and only one deserves 350 MB.

**Volume is log-scaled** because the difference between 1 and 10 hours matters
enormously and the difference between 200 and 210 does not.

**Recency is 17%, and that number was tuned against real output.** At 25%, a
newly discovered 4-hour album outranked an 82-hour one — because with breadth
saturated at 100% for both, nothing else could separate them. A player gets
loaded for months, so recent discoveries should be able to rise without
sweeping away years of listening.

**`reason_start: "trackdone"` counts as a positive signal.** Counterintuitive,
but it means the previous track ended and this one continued unattended — which
is exactly what happens when someone puts on a record and lets it run.

### Evidence, not just plays

An early version accumulated only from the `plays` table. With API data alone
that produced **22 ranked albums**, silently ignoring 395 saved albums and 600
top items already sitting in the database.

Albums now enter the ranking from any evidence: plays, top-track membership, or
a save. Each album carries an `evidence` label (`reproducciones` / `tops` /
`solo-guardado`) that surfaces in the report, so a ranking earned from measured
listening is visibly distinct from one inherited from a save years ago.

### Consolidating editions

A single release exists under several Spotify IDs — regional editions,
reissues, anniversary pressings. Without consolidation, two things break:

- **Evidence splits.** *Cigarettes After Sex — Cry* appeared as three rows of
  36 h + 42 h + 5 h instead of one with 82 h. It ranked below where it belonged
  *and* appeared three times in a buy-list.
- **Breadth saturates.** Editions spell titles differently, so *The Queen Is
  Dead* counted 20 distinct tracks against an album length of 10. 107 of 670
  albums pinned at 100% and the metric stopped discriminating.

`titles.py` normalises artist, album and track titles — stripping known edition
markers, folding diacritics — and groups by that key.

**It is deliberately conservative.** A parenthetical is only stripped if it
contains a known edition keyword, so `(What's the Story) Morning Glory?`
survives intact. A false merge deletes a real album from the results; a false
split is merely untidy. The asymmetry is intentional and tested.

---

## The report

A single HTML file, no `<script>`, no CDN, no remote fonts. Charts are SVG
generated in Python with `math.log1p` and f-strings.

The reason isn't purism: **you'll open this months from now, possibly offline,
possibly on a phone in a record shop.** A dead CDN turns your report into a
blank page. A test fails if a remote `src`/`href` ever appears.

The cost is no interactivity — no client-side filtering or sorting. For a
document you consult rather than explore, that's a good trade. The CSV exists
for anyone who wants to slice it in a spreadsheet.

The centrepiece is a breadth-vs-hours scatter plot, because it makes the whole
thesis visible at a glance: the right side is albums you play whole; high on the
left is the trap — enormous time concentrated in two or three songs.

---

## Testing

57 tests, 811 lines. Two choices carry most of the value.

**The synthetic export fixture.** Three deliberately different profiles — an
album played whole, a single on loop, an old favourite — constructed with
**identical accumulated hours**. If the ranking can't separate them, the project
doesn't work. It's the only test that exercises the actual thesis.

It also embeds the `skipped` field bug: plays before Oct 2022 carry
`skipped: null`, and a test asserts they are not miscounted.

**A fake client for the network loop.** `enrich`'s API loop can't be exercised
against Spotify without spending quota, so a double stands in for the three
responses that matter: a track, a 404, and a 429 carrying a multi-hour
`Retry-After`. That last one is the failure that hung a process for 23 hours in
silence, so it has an explicit test asserting the loop aborts with the retry
window rather than sleeping on it.

There's also a smoke test over the whole output pipeline — added after a
refactor passed `device` in `limit`'s position and broke `report` while 39
tests stayed green, because none of them wrote a file.

**Known gap:** the fake covers logic, not reality. A changed response shape or
an unexpected rate-limit form only surfaces against the live API.

---

## Deliberately out of scope

**Downloading audio from Spotify.** It breaks DRM and the terms of service. The
project ends at the wantlist; you bring the files, and the library tooling
organises them.

**Audio-characteristic analysis and recommendations.** Not a choice — those
endpoints are gone (see FINDINGS §1).

---

## Not built yet

`prep` and `sync` — writing the library to the player — are designed but
unimplemented. It made sense to wait until there were real files to write.

The design has to accommodate the Echo Mini's documented quirks: no M3U support
(so a curated collection becomes a folder), unreliable alphabetical sorting (so
ordering is forced with numeric prefixes derived from the ranking), and
duplicated artists when `albumartist` tags are inconsistent across files (so
tags get normalised to a single canonical value and everything else stripped).

Sync writes to the card in batches rather than staging the full library
locally — the machine this was built on had 45 GB free against a 256 GB card.

---

## Things I'd reconsider

- **The five weights are one global set.** They suit one person's listening.
  A profile mechanism (`--profile deep-cuts`) would be more honest than
  presenting tuned constants as universal.
- **`album_groups` keys on `(artist_name, album_name)` text.** It works and
  it's fast, but it's fragile against artist name variants (`Beyoncé` vs
  `Beyonce` — normalisation handles that one, but not `The Beatles` vs
  `Beatles`). MusicBrainz release-group IDs would be the principled fix.
- **No incremental re-ingest.** Requesting a fresh export and re-ingesting
  works and is idempotent, but it re-reads everything. Fine at 250 MB; not at
  2 GB.
