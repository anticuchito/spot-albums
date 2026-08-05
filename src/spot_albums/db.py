"""Esquema SQLite y helpers de conexión.

Todo el estado vive aquí: reproducciones, catálogo cacheado y snapshots de la API.
Cada etapa del pipeline es idempotente porque escribe con UPSERT o UNIQUE.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode = WAL;

-- ============================================================================
-- Sobre las claves foráneas
--
-- Hay tres situaciones distintas y conviene no uniformarlas:
--
-- 1. Relaciones declaradas y verificadas (tracks->albums, saved_albums->albums,
--    playlist_tracks->tracks, top_items->albums). El código siempre cachea el
--    padre antes que el hijo, así que se declaran y se hacen cumplir.
--
-- 2. `plays.track_id` NO es FK, a propósito. El pipeline ingiere la evidencia
--    antes de resolver el catálogo: `ingest` mete miles de reproducciones y
--    `enrich` las resuelve después, en otra corrida y contra la red. Un FK aquí
--    haría fallar `ingest` entero. La orfandad es un estado legítimo y
--    temporal — de hecho `pending_track_ids()` la usa como cola de trabajo.
--
-- 3. `*.artist_id` NO es FK. La tabla `artists` no es un catálogo: solo guarda
--    los que llegan por /me/top/artists. Con 562 álbumes y 109 artistas, la
--    mayoría de las filas apuntarían al vacío. Es una caché parcial y el
--    nombre de la tabla lo dice ahora explícitamente.
-- ============================================================================

-- ---------------------------------------------------------------- evidencia
-- Reproducciones. Dos fuentes alimentan esta tabla:
--   source='gdpr'   -> export Extended Streaming History (trae ms_played real)
--   source='recent' -> /me/player/recently-played (sin ms_played; solo el evento)
--
-- `track_id` referencia a tracks(track_id) conceptualmente, pero SIN FK: ver
-- el punto 2 de la nota de arriba.
CREATE TABLE IF NOT EXISTS plays (
    id           INTEGER PRIMARY KEY,
    source       TEXT NOT NULL,
    ts           TEXT NOT NULL,          -- ISO-8601 UTC
    ms_played    INTEGER,                -- NULL cuando la fuente no lo sabe
    track_uri    TEXT,                   -- spotify:track:<id>
    track_id     TEXT,                   -- <id> pelado, para joins
    track_name   TEXT,
    artist_name  TEXT,
    album_name   TEXT,
    reason_start TEXT,
    reason_end   TEXT,
    shuffle      INTEGER,
    offline      INTEGER,
    platform     TEXT,
    dedupe_key   TEXT NOT NULL UNIQUE    -- reingerir el mismo ZIP no duplica
);
CREATE INDEX IF NOT EXISTS idx_plays_track ON plays(track_id);
CREATE INDEX IF NOT EXISTS idx_plays_ts    ON plays(ts);

-- ------------------------------------------------------- catálogo (caché API)
-- Se puebla una sola vez por id y no caduca: el catálogo de Spotify no cambia.
CREATE TABLE IF NOT EXISTS tracks (
    track_id    TEXT PRIMARY KEY,
    name        TEXT,
    duration_ms INTEGER,
    disc_number INTEGER,
    track_number INTEGER,
    album_id    TEXT REFERENCES albums(album_id),
    artist_id   TEXT,          -- sin FK: `artists` es caché parcial
    artist_name TEXT,
    fetched_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_tracks_album ON tracks(album_id);

CREATE TABLE IF NOT EXISTS albums (
    album_id     TEXT PRIMARY KEY,
    name         TEXT,
    album_type   TEXT,                   -- album | single | compilation
    total_tracks INTEGER,
    release_date TEXT,
    release_year INTEGER,
    artist_id    TEXT,          -- sin FK: `artists` es caché parcial
    artist_name  TEXT,
    image_url    TEXT,
    fetched_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_albums_artist ON albums(artist_id);

-- OJO: no es un catálogo de artistas. Solo contiene los que llegan por
-- /me/top/artists, que son los únicos de los que la API nos da géneros y
-- popularidad. La mayoría de los artistas de `albums` NO están aquí; usa
-- albums.artist_name para mostrar, no un JOIN contra esta tabla.
CREATE TABLE IF NOT EXISTS artists (
    artist_id  TEXT PRIMARY KEY,
    name       TEXT,
    genres     TEXT,                     -- JSON array
    popularity INTEGER,
    fetched_at TEXT
);

-- Enlace entre los álbumes tal como aparecen en el export y los del catálogo.
--
-- El export ya trae `master_metadata_album_album_name` en cada reproducción, así
-- que agrupar por (artista, álbum) no necesita la API: las horas y los temas
-- distintos salen del propio fichero. Lo único que falta es `total_tracks`, y
-- para eso basta UNA llamada por álbum usando un track suyo como representante
-- —exacta, sin búsqueda difusa, porque el export trae el spotify_track_uri.
--
-- Resolver por álbum en vez de por track baja el coste de ~57.000 peticiones a
-- ~1.000, que es la diferencia entre agotar la cuota diaria y no tocarla.
CREATE TABLE IF NOT EXISTS album_groups (
    artist_name  TEXT NOT NULL,
    album_name   TEXT NOT NULL,
    album_id     TEXT REFERENCES albums(album_id),
    rep_track_id TEXT,               -- el track más escuchado del grupo
    resolved_at  TEXT,
    PRIMARY KEY (artist_name, album_name)
);
CREATE INDEX IF NOT EXISTS idx_album_groups_album ON album_groups(album_id);

-- Ids que la API no supo resolver (track retirado del catálogo, región, etc.).
-- Se registran para no reintentarlos en cada corrida de `enrich`.
CREATE TABLE IF NOT EXISTS unresolved (
    track_id   TEXT PRIMARY KEY,
    reason     TEXT,
    tried_at   TEXT
);

-- ------------------------------------------------------------- snapshots API
-- Cada `pull` es un snapshot fechado, así se acumula evolución si lo corres
-- periódicamente en vez de sobrescribir el estado anterior.
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id INTEGER PRIMARY KEY,
    taken_at    TEXT NOT NULL,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS top_items (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(snapshot_id),
    kind        TEXT NOT NULL,           -- artist | track
    time_range  TEXT NOT NULL,           -- short_term | medium_term | long_term
    rank        INTEGER NOT NULL,
    item_id     TEXT NOT NULL,
    name        TEXT,
    artist_id   TEXT,
    artist_name TEXT,
    album_id    TEXT REFERENCES albums(album_id),
    PRIMARY KEY (snapshot_id, kind, time_range, rank)
);

CREATE TABLE IF NOT EXISTS saved_albums (
    album_id    TEXT PRIMARY KEY REFERENCES albums(album_id),
    added_at    TEXT,
    snapshot_id INTEGER REFERENCES snapshots(snapshot_id)
);

CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_id   TEXT NOT NULL,
    playlist_name TEXT,
    owned_by_me   INTEGER,
    track_id      TEXT NOT NULL REFERENCES tracks(track_id),
    added_at      TEXT,
    PRIMARY KEY (playlist_id, track_id)
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # SQLite ignora las FK salvo que se pidan explícitamente, y el pragma es
    # por conexión, no por base. Sin esto las declaraciones serían decorativas.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def new_snapshot(conn: sqlite3.Connection, note: str = "") -> int:
    from datetime import datetime, timezone

    cur = conn.execute(
        "INSERT INTO snapshots (taken_at, note) VALUES (?, ?)",
        (datetime.now(timezone.utc).isoformat(), note),
    )
    return int(cur.lastrowid)


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Resumen de qué hay en la base — lo usan `status` y los mensajes del CLI."""
    out: dict[str, int] = {}
    for table in ("plays", "tracks", "albums", "artists", "top_items",
                  "saved_albums", "playlist_tracks", "snapshots"):
        out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    out["plays_gdpr"] = conn.execute(
        "SELECT COUNT(*) FROM plays WHERE source = 'gdpr'"
    ).fetchone()[0]
    out["album_groups"] = conn.execute(
        "SELECT COUNT(*) FROM album_groups"
    ).fetchone()[0]
    out["albums_resueltos"] = conn.execute(
        "SELECT COUNT(*) FROM album_groups WHERE album_id IS NOT NULL"
    ).fetchone()[0]
    out["tracks_pending"] = conn.execute(
        """SELECT COUNT(DISTINCT p.track_id) FROM plays p
           LEFT JOIN tracks t ON t.track_id = p.track_id
           LEFT JOIN unresolved u ON u.track_id = p.track_id
           WHERE p.track_id IS NOT NULL AND t.track_id IS NULL AND u.track_id IS NULL"""
    ).fetchone()[0]
    return out
