"""Fixtures: un export GDPR sintético con los casos que importan."""

from __future__ import annotations

import json
import sqlite3
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from spot_albums import db

NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)


def play(track_uri: str, track: str, artist: str, album: str,
         days_ago: int, ms: int = 210_000, reason_start: str = "trackdone",
         shuffle: bool = False, skipped: bool | None = None) -> dict:
    return {
        "ts": (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z"),
        "platform": "osx",
        "ms_played": ms,
        "conn_country": "CL",
        "master_metadata_track_name": track,
        "master_metadata_album_artist_name": artist,
        "master_metadata_album_album_name": album,
        "spotify_track_uri": track_uri,
        "reason_start": reason_start,
        "reason_end": "trackdone",
        "shuffle": shuffle,
        "skipped": skipped,
        "offline": False,
        "incognito_mode": False,
    }


def build_records() -> list[dict]:
    """Tres perfiles de escucha deliberadamente distintos.

    ALBUM_FULL   10 temas distintos, escucha secuencial reciente -> breadth 1.0
    ONE_HIT      un solo tema de un disco de 12, muchísimas repeticiones
    OLD_FAVE     disco entero pero escuchado hace 4 años -> lo mata la recencia
    """
    records: list[dict] = []

    # --- escuchador de álbum: los 10 temas, varias vueltas, reciente
    for rep in range(6):
        for n in range(1, 11):
            records.append(play(
                f"spotify:track:full{n:02d}", f"Full Track {n}",
                "Album Artist", "The Whole Thing",
                days_ago=10 + rep * 5,
            ))

    # --- un solo hit en bucle: mismo número de reproducciones y las mismas
    #     horas totales que el álbum completo, pero concentradas en un tema.
    #     Cada play en un día distinto: si dos coincidieran en (ts, uri, ms)
    #     el dedupe los colapsaría, que es justo lo que debe hacer.
    for rep in range(60):
        records.append(play(
            "spotify:track:hit00001", "The Hit", "Hit Artist",
            "Album With One Hit", days_ago=8 + rep, shuffle=True,
            reason_start="clickrow",
        ))

    # --- favorito viejo: disco completo pero de 2022, con `skipped` a None
    #     (el bug documentado: Spotify no registró skips antes de 2022-10-16)
    for rep in range(6):
        for n in range(1, 11):
            records.append(play(
                f"spotify:track:old{n:02d}", f"Old Track {n}",
                "Nostalgia", "Back Then", days_ago=1400 + rep * 5,
                skipped=None,
            ))

    # --- skips reales: se detectan por ms_played, no por el campo `skipped`
    for n in range(1, 6):
        records.append(play(
            f"spotify:track:skip{n:02d}", f"Skipped {n}", "Skippy",
            "Skipped Album", days_ago=5, ms=4_000, skipped=False,
        ))

    # --- ruido que debe filtrarse: un podcast sin spotify_track_uri
    records.append({
        "ts": NOW.isoformat().replace("+00:00", "Z"),
        "ms_played": 1_800_000,
        "master_metadata_track_name": None,
        "master_metadata_album_artist_name": None,
        "master_metadata_album_album_name": None,
        "spotify_track_uri": None,
        "episode_name": "Un podcast cualquiera",
        "spotify_episode_uri": "spotify:episode:abc",
        "reason_start": "clickrow",
        "reason_end": "endplay",
        "shuffle": False,
        "skipped": False,
        "offline": False,
    })
    return records


@pytest.fixture
def export_zip(tmp_path: Path) -> Path:
    """ZIP con la misma forma que el que manda Spotify."""
    path = tmp_path / "my_spotify_data.zip"
    records = build_records()
    half = len(records) // 2
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Spotify Extended Streaming History/Streaming_History_Audio_2022_0.json",
                    json.dumps(records[:half]))
        zf.writestr("Spotify Extended Streaming History/Streaming_History_Audio_2026_1.json",
                    json.dumps(records[half:]))
        # Historial de vídeo: debe ignorarse por completo.
        zf.writestr("Spotify Extended Streaming History/Streaming_History_Video_2026.json",
                    json.dumps([{"ts": "2026-01-01T00:00:00Z", "ms_played": 999}]))
    return path


# Metadatos de catálogo que normalmente vendrían de la API.
CATALOG_ALBUMS = {
    "alb_full": ("The Whole Thing", "Album Artist", 10, 2024),
    "alb_hit": ("Album With One Hit", "Hit Artist", 12, 2023),
    "alb_old": ("Back Then", "Nostalgia", 10, 2015),
    "alb_skip": ("Skipped Album", "Skippy", 8, 2025),
}


@pytest.fixture
def seeded_db(tmp_path: Path, export_zip: Path) -> sqlite3.Connection:
    """Base con el export cargado y el catálogo ya resuelto."""
    from spot_albums.ingest import gdpr

    conn = db.connect(tmp_path / "test.db")
    gdpr.ingest(conn, export_zip)

    for album_id, (name, artist, total, year) in CATALOG_ALBUMS.items():
        conn.execute(
            """INSERT INTO albums (album_id, name, album_type, total_tracks,
                                   release_date, release_year, artist_id,
                                   artist_name, fetched_at)
               VALUES (?,?,'album',?,?,?,?,?,'now')""",
            (album_id, name, total, f"{year}-01-01", year,
             f"art_{album_id}", artist),
        )

    mapping = [
        (f"full{n:02d}", "alb_full", "Album Artist") for n in range(1, 11)
    ] + [
        ("hit00001", "alb_hit", "Hit Artist"),
    ] + [
        (f"old{n:02d}", "alb_old", "Nostalgia") for n in range(1, 11)
    ] + [
        (f"skip{n:02d}", "alb_skip", "Skippy") for n in range(1, 6)
    ]
    for track_id, album_id, artist in mapping:
        conn.execute(
            """INSERT INTO tracks (track_id, name, duration_ms, album_id,
                                   artist_id, artist_name, fetched_at)
               VALUES (?,?,?,?,?,?,'now')""",
            (track_id, track_id, 210_000, album_id, f"art_{album_id}", artist),
        )
    conn.commit()
    return conn
