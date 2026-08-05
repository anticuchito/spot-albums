"""Parser del export "Extended Streaming History" de Spotify (GDPR).

El ZIP trae una carpeta de JSONs de ~12 MB cada uno, más un PDF que documenta
los campos. Cada registro es una reproducción:

    ts, ms_played, platform, conn_country, ip_addr,
    master_metadata_track_name, master_metadata_album_artist_name,
    master_metadata_album_album_name, spotify_track_uri,
    episode_name, episode_show_name, spotify_episode_uri,
    reason_start, reason_end, shuffle, skipped, offline,
    offline_timestamp, incognito_mode

Decisiones de calidad de dato, documentadas porque no son obvias:

* **Ignoramos el campo `skipped`.** Spotify no registró skips entre 2015-04-13
  y 2022-10-16: en años de historial el campo es NULL o False aunque hubiera
  skip. Derivamos el skip de `ms_played` en la capa de scoring.
* **Descartamos podcasts.** Los registros con `spotify_episode_uri` no aportan
  nada a una wantlist de álbumes.
* **Descartamos reproducciones sin `spotify_track_uri`.** Sin id no se puede
  resolver el álbum de forma fiable, y el matching por nombre es un pozo.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path
from typing import Iterator

PREFIX = "spotify:track:"


def _dedupe_key(rec: dict) -> str:
    raw = f"{rec.get('ts')}|{rec.get('spotify_track_uri')}|{rec.get('ms_played')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _iter_json_files(source: Path) -> Iterator[tuple[str, bytes]]:
    """Acepta el ZIP tal cual llega, o una carpeta ya descomprimida."""
    if source.is_dir():
        for path in sorted(source.rglob("*.json")):
            yield path.name, path.read_bytes()
        return

    with zipfile.ZipFile(source) as zf:
        for info in sorted(zf.infolist(), key=lambda i: i.filename):
            if info.filename.endswith(".json") and not info.is_dir():
                yield Path(info.filename).name, zf.read(info)


def _is_audio_history(filename: str) -> bool:
    """Solo los ficheros de historial de audio.

    El export también trae Streaming_History_Video_*.json y, si pediste
    "Account data", ficheros como Playlist1.json o Marquee.json que tienen
    otra forma completamente distinta.
    """
    name = filename.lower()
    return name.startswith("streaming_history") and "video" not in name


def parse(source: Path) -> Iterator[dict]:
    """Genera filas listas para insertar en `plays`."""
    for filename, raw in _iter_json_files(source):
        if not _is_audio_history(filename):
            continue
        try:
            records = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(records, list):
            continue

        for rec in records:
            uri = rec.get("spotify_track_uri")
            if not uri or not uri.startswith(PREFIX):
                continue  # podcast o registro sin id
            yield {
                "source": "gdpr",
                "ts": rec.get("ts"),
                "ms_played": rec.get("ms_played"),
                "track_uri": uri,
                "track_id": uri[len(PREFIX):],
                "track_name": rec.get("master_metadata_track_name"),
                "artist_name": rec.get("master_metadata_album_artist_name"),
                "album_name": rec.get("master_metadata_album_album_name"),
                "reason_start": rec.get("reason_start"),
                "reason_end": rec.get("reason_end"),
                "shuffle": 1 if rec.get("shuffle") else 0,
                "offline": 1 if rec.get("offline") else 0,
                "platform": rec.get("platform"),
                "dedupe_key": _dedupe_key(rec),
            }


def ingest(conn: sqlite3.Connection, source: Path) -> dict[str, object]:
    """Carga el export en `plays`. Reingerir el mismo ZIP no duplica nada."""
    before = conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
    seen = 0
    batch: list[dict] = []

    def flush() -> None:
        conn.executemany(
            """INSERT OR IGNORE INTO plays
               (source, ts, ms_played, track_uri, track_id, track_name,
                artist_name, album_name, reason_start, reason_end,
                shuffle, offline, platform, dedupe_key)
               VALUES (:source, :ts, :ms_played, :track_uri, :track_id, :track_name,
                       :artist_name, :album_name, :reason_start, :reason_end,
                       :shuffle, :offline, :platform, :dedupe_key)""",
            batch,
        )
        batch.clear()

    for row in parse(source):
        batch.append(row)
        seen += 1
        if len(batch) >= 5000:
            flush()
    if batch:
        flush()
    conn.commit()

    after = conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
    span = conn.execute(
        "SELECT MIN(ts), MAX(ts) FROM plays WHERE source = 'gdpr'"
    ).fetchone()
    return {
        "leidas": seen,
        "nuevas": after - before,
        "duplicadas": seen - (after - before),
        "desde": span[0],
        "hasta": span[1],
    }
