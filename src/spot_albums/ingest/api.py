"""Snapshot del estado actual de tu cuenta vía Web API.

Esto es lo que puedes tener *hoy*, sin esperar el export GDPR. Es mucho más
pobre —top 50 por rango, últimas ~50 reproducciones— pero tiene una virtud que
el export no tiene: dice qué escuchas *ahora*, ya rankeado por Spotify.

Cada corrida crea un snapshot fechado en vez de sobrescribir, así que si lo
corres cada pocas semanas acumulas una serie temporal propia.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from ..spotify.client import Client, SpotifyError
from ..db import new_snapshot

TIME_RANGES = ("short_term", "medium_term", "long_term")
PREFIX = "spotify:track:"


def playlist_item_track(item: dict) -> dict:
    """Extrae el track de un item de playlist.

    En feb-2026 Spotify renombró el wrapper: `/playlists/{id}/tracks` pasó a
    `/playlists/{id}/items` (el viejo devuelve 403) y dentro de cada entrada la
    clave `track` pasó a llamarse `item`. Aceptamos ambas por si acaso: el
    fallo es silencioso —devuelve cero canciones sin error— y no queremos
    volver a comerlo si Spotify da marcha atrás.
    """
    return item.get("item") or item.get("track") or {}


def _cache_album(conn: sqlite3.Connection, album: dict) -> None:
    if not album or not album.get("id"):
        return
    images = album.get("images") or []
    release = album.get("release_date") or ""
    artists = album.get("artists") or [{}]
    conn.execute(
        """INSERT INTO albums (album_id, name, album_type, total_tracks,
                               release_date, release_year, artist_id,
                               artist_name, image_url, fetched_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(album_id) DO UPDATE SET
               total_tracks = COALESCE(excluded.total_tracks, albums.total_tracks),
               image_url    = COALESCE(excluded.image_url, albums.image_url)""",
        (
            album["id"],
            album.get("name"),
            album.get("album_type"),
            album.get("total_tracks"),
            release,
            int(release[:4]) if release[:4].isdigit() else None,
            artists[0].get("id"),
            artists[0].get("name"),
            images[0]["url"] if images else None,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def _cache_track(conn: sqlite3.Connection, track: dict) -> bool:
    """Cachea el track y su álbum. Devuelve False si no se pudo.

    Devolver el resultado importa: los ficheros locales que el usuario subió a
    Spotify no tienen id de catálogo y se descartan aquí. Quien inserte después
    en `playlist_tracks` o `top_items` tiene que saberlo, o dejaría una fila
    apuntando a un track inexistente.
    """
    if not track or not track.get("id") or track.get("is_local"):
        return False
    album = track.get("album") or {}
    artists = track.get("artists") or [{}]
    _cache_album(conn, album)
    conn.execute(
        """INSERT OR REPLACE INTO tracks
           (track_id, name, duration_ms, disc_number, track_number,
            album_id, artist_id, artist_name, fetched_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            track["id"],
            track.get("name"),
            track.get("duration_ms"),
            track.get("disc_number"),
            track.get("track_number"),
            album.get("id"),
            artists[0].get("id"),
            artists[0].get("name"),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return True


def _cache_artist(conn: sqlite3.Connection, artist: dict) -> None:
    import json as _json

    if not artist or not artist.get("id"):
        return
    conn.execute(
        """INSERT OR REPLACE INTO artists
           (artist_id, name, genres, popularity, fetched_at)
           VALUES (?,?,?,?,?)""",
        (
            artist["id"],
            artist.get("name"),
            _json.dumps(artist.get("genres") or []),
            artist.get("popularity"),
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def pull(conn: sqlite3.Connection, client: Client) -> dict[str, int]:
    snap = new_snapshot(conn, note="pull")
    stats = {"top_artists": 0, "top_tracks": 0, "recientes": 0,
             "albumes_guardados": 0, "playlists": 0, "tracks_playlist": 0}

    # --- top artists / tracks en los tres rangos temporales
    for time_range in TIME_RANGES:
        for rank, artist in enumerate(client.top("artists", time_range), start=1):
            _cache_artist(conn, artist)
            conn.execute(
                """INSERT OR REPLACE INTO top_items
                   (snapshot_id, kind, time_range, rank, item_id, name,
                    artist_id, artist_name, album_id)
                   VALUES (?,'artist',?,?,?,?,?,?,NULL)""",
                (snap, time_range, rank, artist["id"], artist.get("name"),
                 artist["id"], artist.get("name")),
            )
            stats["top_artists"] += 1

        for rank, track in enumerate(client.top("tracks", time_range), start=1):
            if not _cache_track(conn, track):
                continue  # fichero local: no existe en el catálogo
            artists = track.get("artists") or [{}]
            conn.execute(
                """INSERT OR REPLACE INTO top_items
                   (snapshot_id, kind, time_range, rank, item_id, name,
                    artist_id, artist_name, album_id)
                   VALUES (?,'track',?,?,?,?,?,?,?)""",
                (snap, time_range, rank, track["id"], track.get("name"),
                 artists[0].get("id"), artists[0].get("name"),
                 (track.get("album") or {}).get("id")),
            )
            stats["top_tracks"] += 1

    # --- reproducciones recientes -> tabla `plays` (sin ms_played: la API no lo da)
    for item in client.recently_played():
        track = item.get("track") or {}
        if not track.get("id"):
            continue
        _cache_track(conn, track)
        played_at = item.get("played_at")
        album = track.get("album") or {}
        artists = track.get("artists") or [{}]
        conn.execute(
            """INSERT OR IGNORE INTO plays
               (source, ts, ms_played, track_uri, track_id, track_name,
                artist_name, album_name, reason_start, reason_end,
                shuffle, offline, platform, dedupe_key)
               VALUES ('recent', ?, NULL, ?, ?, ?, ?, ?, NULL, NULL,
                       NULL, NULL, NULL, ?)""",
            (played_at, PREFIX + track["id"], track["id"], track.get("name"),
             artists[0].get("name"), album.get("name"),
             f"recent|{played_at}|{track['id']}"),
        )
        stats["recientes"] += 1

    # --- álbumes guardados (el GET sigue vivo; solo los writes se movieron
    #     a /me/library en feb-2026)
    for item in client.saved_albums():
        album = item.get("album") or {}
        if not album.get("id"):
            continue
        _cache_album(conn, album)
        conn.execute(
            "INSERT OR REPLACE INTO saved_albums (album_id, added_at, snapshot_id) "
            "VALUES (?,?,?)",
            (album["id"], item.get("added_at"), snap),
        )
        stats["albumes_guardados"] += 1

    # --- playlists propias (las ajenas no dicen nada de tu gusto)
    me_id = client.me().get("id")
    for pl in client.my_playlists():
        owned = (pl.get("owner") or {}).get("id") == me_id
        if not owned:
            # Las ajenas además devuelven 403 en /items desde feb-2026.
            continue
        stats["playlists"] += 1
        try:
            items = list(client.playlist_items(pl["id"]))
        except SpotifyError as exc:
            print(f"  playlist {pl.get('name')!r} no accesible: {str(exc)[:80]}")
            continue
        for item in items:
            track = playlist_item_track(item)
            if not track.get("id") or track.get("type") != "track":
                continue
            if not _cache_track(conn, track):
                continue  # MP3 que subiste tú: no está en el catálogo
            conn.execute(
                """INSERT OR REPLACE INTO playlist_tracks
                   (playlist_id, playlist_name, owned_by_me, track_id, added_at)
                   VALUES (?,?,1,?,?)""",
                (pl["id"], pl.get("name"), track["id"], item.get("added_at")),
            )
            stats["tracks_playlist"] += 1

    conn.commit()
    return stats
