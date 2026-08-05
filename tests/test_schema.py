"""Integridad referencial: qué se declara, qué no, y por qué.

Las tres decisiones del esquema son deliberadas y fáciles de "arreglar" mal.
Estos tests las fijan.
"""

from __future__ import annotations

import sqlite3

import pytest

from spot_albums import db


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.db")


def test_las_fk_se_hacen_cumplir(conn):
    """SQLite las ignora salvo que se pida el pragma en cada conexión."""
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_no_se_puede_guardar_un_album_inexistente(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO saved_albums (album_id, added_at) VALUES ('fantasma','x')"
        )


def test_no_se_puede_meter_en_playlist_un_track_inexistente(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO playlist_tracks (playlist_id, track_id)
               VALUES ('pl1', 'fantasma')"""
        )


def test_un_track_no_puede_apuntar_a_un_album_inexistente(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO tracks (track_id, album_id) VALUES ('t1','fantasma')"
        )


def test_plays_admite_tracks_aun_no_resueltos(conn):
    """Deliberado: `ingest` corre antes que `enrich`.

    La evidencia entra antes que el catálogo, en otra corrida y contra la red.
    Un FK aquí haría fallar la ingesta del export entero. La orfandad es un
    estado válido y temporal — `pending_track_ids()` la usa como cola.
    """
    conn.execute(
        """INSERT INTO plays (source, ts, track_id, dedupe_key)
           VALUES ('gdpr', '2026-01-01T00:00:00Z', 'todavia_sin_resolver', 'k1')"""
    )
    assert conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0] == 1


def test_los_albumes_admiten_artistas_fuera_de_la_cache(conn):
    """Deliberado: `artists` solo tiene los de /me/top/artists.

    Con 562 álbumes y 109 artistas, exigir el FK dejaría fuera la mayoría de
    la biblioteca. Para mostrar se usa albums.artist_name, no un JOIN.
    """
    conn.execute(
        "INSERT INTO albums (album_id, name, artist_id) VALUES ('a1','X','no_cacheado')"
    )
    assert conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0] == 1


def test_el_orden_del_pipeline_respeta_las_fk(conn):
    """album -> track -> playlist_tracks, que es el orden real de `pull`."""
    conn.execute("INSERT INTO albums (album_id, name) VALUES ('alb1','Disco')")
    conn.execute("INSERT INTO tracks (track_id, album_id) VALUES ('tr1','alb1')")
    conn.execute(
        "INSERT INTO playlist_tracks (playlist_id, track_id) VALUES ('pl1','tr1')"
    )
    conn.execute("INSERT INTO saved_albums (album_id) VALUES ('alb1')")
    conn.commit()
