# Field notes: Spotify data, as of August 2026

Everything here was learned by hitting it, not by reading docs. Each item cost a
real debugging session. If you're building anything on Spotify listening data,
read this before you write code — most of it is not in the official
documentation, and some of it contradicts what's still published in tutorials.

Verified against the live API on **2026-08-04**.

---

## 1. Half the interesting endpoints are gone

**Deprecated 2024-11-27, permanently, with no replacement:**

| Endpoint | Status |
|---|---|
| `GET /v1/audio-features/{id}` | 403 for any app created after that date |
| `GET /v1/audio-analysis/{id}` | same |
| `GET /v1/recommendations` | same |
| `GET /v1/artists/{id}/related-artists` | same |
| `GET /v1/browse/featured-playlists` | same |

Apps with a quota extension already granted before that date still work. There
is **no waitlist and no appeal**. If a tutorial tells you to build a taste
profile from `energy` / `danceability` / `valence`, that tutorial is dead.

**Removed 2026-02:**

- `GET /v1/artists/{id}/top-tracks`
- `GET /v1/markets`
- All the `Get Several *` batch endpoints (`/tracks?ids=`, `/albums?ids=`,
  `/artists?ids=`, …) — **this one matters a lot**, see §4.
- `/me/following` — folded into `/me/library`
- Library writes: `PUT`/`DELETE` on `/me/albums`, `/me/tracks`, `/me/shows`,
  `/me/episodes` → now `PUT`/`DELETE /me/library`. **Reads still work** at the
  old paths.

**Still fine:** `/me/top/artists`, `/me/top/tracks` (all three `time_range`
values), `/me/player/recently-played`, `GET /me/albums`, `GET /albums/{id}`,
`GET /tracks/{id}`, `/artists/{id}/albums`.

### The practical consequence

You cannot derive taste from audio characteristics anymore. You have to derive
it from **behaviour** — how long, how often, how much of an album, how
recently. For most purposes that's better data anyway; it just requires the
GDPR export rather than a couple of API calls.

---

## 2. `/playlists/{id}/items` fails silently

In February 2026, `/playlists/{id}/tracks` became `/playlists/{id}/items`. The
old path now returns **403**, which is at least loud.

The quiet part: inside each entry, the key `track` was renamed to `item`.

```jsonc
// before
{ "added_at": "...", "track": { "id": "...", "type": "track" } }
// now
{ "added_at": "...", "item":  { "id": "...", "type": "track" } }
```

Code doing `entry["track"]` now gets `None`, skips every row, and reports
**zero tracks with HTTP 200 and no exception**. We shipped this bug and only
caught it because a playlist count of `0` looked implausible next to
`playlists: 3`.

The playlist object itself changed to match: `tracks: {total: N}` is now
`items: {total: N}`.

Read both keys, in this order:

```python
track = entry.get("item") or entry.get("track") or {}
```

Also: `/items` returns **403 for playlists you don't own**, even public ones
you follow. Filter by owner before requesting.

---

## 3. The `skipped` field lies for seven years

The GDPR export has a `skipped` boolean per play. **Spotify did not record
skips between 2015-04-13 and 2022-10-16.** Every play in that window has
`skipped: null` or `false` regardless of what actually happened.

If your history predates 2022 — most people's does — that field will quietly
skew any engagement metric built on it.

Derive it from playback time instead:

```python
was_skipped = (ms_played or 0) < 30_000
```

Related export caveats, all documented by others and all real:

- ~2.6% of stream durations overlap with the following stream.
- Timestamps are rounded to the second.
- The same recording appears under multiple `spotify_track_uri` values
  (remasters, reissues, regional editions) — see §5.
- `offline_timestamp` semantics are unclear; >23% of streams disagree with the
  `offline` flag.

---

## 4. Resolving track-by-track will get you banned for a day

This is the expensive one.

The obvious design: for each distinct track in your history, call
`GET /tracks/{id}` to find out which album it belongs to. With 12 years of
history that's ~57,000 requests. Batch calls were removed in Feb 2026, so
there's no way to shrink it.

What happens: you exhaust the daily quota for a development-mode app and get

```
HTTP 429
Retry-After: 82661     # 23 hours
```

**Do not sleep on that value.** A naive `time.sleep(retry_after)` parks your
process for a full day. Cap it and fail loudly:

```python
if wait > MAX_BACKOFF_S:          # we use 120
    raise RateLimited(f"quota exhausted; retry in {wait/3600:.1f}h",
                      retry_after=wait)
```

Two related traps we hit:

- `print()` to a pipe is block-buffered. Our rate-limit messages sat in a 4 KB
  buffer and never appeared, so the process looked idle rather than throttled.
  Use `flush=True` on anything you'll debug from.
- A socket in `CLOSE_WAIT` plus near-zero CPU is the signature of exactly this
  situation.

### The limit is a sliding window, not a daily quota

Worth stating separately because it changes recovery strategy. After the
23-hour block expired, a single request succeeded — and the very next burst
tripped a fresh `429` with `Retry-After: 1190` (20 minutes). The app stays
penalised for a while after a large burst; there is no clean daily reset to
wait for.

So don't plan around "the quota resets tomorrow." Pace requests deliberately
instead. A forced minimum interval of 0.3–0.5 s between calls finishes a few
thousand requests faster than running flat out and absorbing repeated
20-minute penalties.

### The fix is to ask a different question

The export **already contains the album name** for every play
(`master_metadata_album_album_name`). Grouping plays by (artist, album), summing
time, and counting distinct tracks needs **no network at all**.

The only thing the API adds is `total_tracks` — the denominator for
"what fraction of this album have I heard" — and that is **one fact per album,
not per track**.

So: group locally, then resolve **one representative track per album** (the
most-played one; it's the most likely to still be in the catalogue). Because
the export carries `spotify_track_uri`, this is exact — no fuzzy search by name.

**~57,000 requests → ~1,000.** Same information.

Order the work by listening time descending, so an interrupted run leaves you
with the albums that matter rather than an arbitrary slice.

---

## 5. One album is many albums

A single release exists in Spotify under several IDs: regional editions,
reissues, anniversary editions, remasters. Two things break.

**Evidence gets split.** In our data, *Cigarettes After Sex — Cry* appeared as
three rows with 36 h + 42 h + 5 h instead of one with 82 h. The album ranked
lower than it deserved *and* showed up three times in a buy-list.

**Breadth saturates.** Editions spell track titles differently — `Bigmouth
Strikes Again`, `Bigmouth Strikes Again - 2011 Remaster`. Counting distinct
titles, *The Queen Is Dead* showed **20 tracks played out of 10**, so
`played / total` clamped at 100% for anything reissued. 107 of 670 albums
pinned at 100%, and the metric stopped discriminating.

Normalise both album and track titles before grouping: strip parenthetical and
dash-suffixed edition markers, fold diacritics, lowercase.

**Be conservative.** `(What's the Story) Morning Glory?` must survive
normalisation intact. Only strip a parenthetical if it contains a known edition
keyword (`remaster`, `deluxe`, `anniversary`, `reissue`, …). A false merge
deletes a real album from the results; a false split is merely untidy.

---

## 6. OAuth: loopback only, PKCE required

Since **2025-11-27**:

- Implicit grant: gone.
- HTTP redirect URIs: gone, **except literal loopback** — `http://127.0.0.1:PORT/…`
  or `http://[::1]:PORT/…`.
- `http://localhost:PORT` **no longer counts as loopback.** It must be the IP.
  This is the single most common setup failure.
- Public clients must use PKCE. No client secret needed — don't put one in a
  CLI tool.

The registered redirect URI must match byte-for-byte, and the dashboard needs
you to press **Add** before **Save** or it isn't stored.

---

## 7. Requesting the export

At <https://www.spotify.com/account/privacy>, three checkboxes:

| Box | Take it? | Why |
|---|---|---|
| Account data | **no** | Only ~1 year of history. This is the trap — people take this one and think they're done. |
| Extended streaming history | **yes** | Full lifetime, `ms_played` per play. |
| Technical log information | no | Device/network logs. Useless here. |

**You must click the green Confirm button in the follow-up email** or the
request is never processed. Official estimate is 30 days; in practice 1–5.

You get a folder of `Streaming_History_Audio_YYYY[_N].json` files (~12 MB
each), plus `Streaming_History_Video_*.json` — skip those — and a PDF
describing the schema.

Per-play fields: `ts`, `ms_played`, `platform`, `conn_country`, `ip_addr`,
`master_metadata_track_name`, `master_metadata_album_artist_name`,
`master_metadata_album_album_name`, `spotify_track_uri`, `episode_name`,
`spotify_episode_uri`, `reason_start`, `reason_end`, `shuffle`, `skipped`,
`offline`, `offline_timestamp`, `incognito_mode`.

For scale: 12 years of fairly heavy listening came to 325,585 plays across
33,416 distinct (artist, album) pairs — about 250 MB of JSON, which parses in
seconds.

---

## 8. `reason_start: "trackdone"` is a positive signal

Counterintuitive, so worth stating plainly. `trackdone` means the previous
track ended and this one continued on its own. That is exactly what happens
when someone puts on an album and lets it run.

Combined with `shuffle: false`, it's the cleanest available signal for
"listening to a record" as opposed to "leaving a radio on." Weight it up, not
down.

---

## 9. Two orthogonal words of caution about SQLite

Not Spotify-specific, but both bit us in this project.

**Foreign keys are off by default**, per connection, always. Declared
constraints are decorative until you issue `PRAGMA foreign_keys = ON`. Turning
it on immediately exposed a real bug: local files (user-uploaded MP3s) have no
catalogue ID, so they were being skipped from the `tracks` cache while still
being inserted into `playlist_tracks`.

**Not every relationship should be a foreign key.** Ours has three cases:

- Declared and enforced: `tracks→albums`, `saved_albums→albums`,
  `playlist_tracks→tracks`.
- Deliberately not declared: `plays.track_id`. Evidence is ingested *before*
  the catalogue is resolved, in a separate networked pass. An FK here makes
  ingestion fail outright. Orphans are a valid, temporary state — we use them
  as the work queue.
- Deliberately not declared: `*.artist_id`. The `artists` table only holds
  what `/me/top/artists` returns (109 rows against 562 albums). It's a partial
  cache, not a catalogue. Name it so nobody joins against it expecting
  completeness.

---

## Sources

Primary (checked against live responses):

- [Changes to the Web API — 2024-11-27](https://developer.spotify.com/blog/2024-11-27-changes-to-the-web-api)
- [Web API changelog — February 2026](https://developer.spotify.com/documentation/web-api/references/changes/february-2026)
- [OAuth migration reminder — 2025-11-27](https://developer.spotify.com/blog/2025-10-14-reminder-oauth-migration-27-nov-2025)
- [Authorization Code with PKCE](https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow)

Secondary:

- [Ortham — analysis of the extended streaming history export](https://blog.ortham.net/posts/2024-12-21-spotify-streaming-history-part-1/)
  (the `skipped` date window and the overlap statistics come from here)
- [How to export extended streaming history](https://mystats.music/guides/spotify-extended-history)

Device-specific (Snowsky Echo Mini):

- [FiiO forum — file handling](https://forum.fiio.com/note/showNoteContent.do?id=202511112154138842031)
  (no M3U support, unreliable alphabetical sorting, duplicated artists)
- [Product page](https://www.fiio.com/echomini)
