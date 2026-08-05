"""Resuelve álbumes contra el catálogo de Spotify.

## Por qué esto va por álbum y no por track

La versión ingenua resolvía cada track escuchado (`GET /tracks/{id}`) para
descubrir a qué álbum pertenecía. Con 12 años de historial son ~57.000
peticiones, y Spotify retiró las llamadas en lote en feb-2026. En la práctica
eso agota la cuota diaria de una app en modo desarrollo y devuelve un
`Retry-After` de ~23 horas.

Pero la pregunta que resolvía era la equivocada. El export **ya trae el nombre
del álbum** en cada reproducción (`master_metadata_album_album_name`), así que
agrupar por (artista, álbum), sumar horas y contar temas distintos no necesita
red alguna. Lo único que la API aporta es `total_tracks`, el denominador del
breadth — y eso es **un dato por álbum, no por track**.

Así que se resuelve un solo track representativo por álbum. Como el export trae
`spotify_track_uri`, es exacto: nada de buscar por nombre y rezar. El coste baja
de ~57.000 peticiones a ~1.000.

## Prioridad

Los álbumes se resuelven de más a menos escuchado. La wantlist cabe en ~650
discos, así que resolver los primeros ~1.500 cubre de sobra cualquier cosa que
vaya a entrar, y el resto solo añadiría ruido al fondo del ranking.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

from .config import SKIP_THRESHOLD_MS
from .ingest.api import _cache_album, _cache_track
from .spotify.client import Client, RateLimited

# Cuántos álbumes resolver por defecto. Los ~650 que caben en 256 GB de FLAC
# quedan cubiertos de sobra; el resto da cola larga para descartar y para el
# análisis histórico. A 3.7 req/s son ~27 min, muy lejos de la cuota diaria.
DEFAULT_ALBUM_BUDGET = 6000


def build_album_groups(conn: sqlite3.Connection) -> int:
    """Agrupa las reproducciones por (artista, álbum). Sin red.

    Elige como representante el track con más tiempo acumulado del grupo: es el
    que tiene más probabilidad de seguir en el catálogo y no ser una rareza
    regional retirada.
    """
    conn.execute(
        """INSERT INTO album_groups (artist_name, album_name, rep_track_id)
           SELECT artist_name, album_name, track_id FROM (
               SELECT artist_name, album_name, track_id,
                      ROW_NUMBER() OVER (
                          PARTITION BY artist_name, album_name
                          ORDER BY SUM(COALESCE(ms_played, 0)) DESC
                      ) AS rn
                 FROM plays
                WHERE artist_name IS NOT NULL
                  AND album_name  IS NOT NULL
                  AND track_id    IS NOT NULL
                GROUP BY artist_name, album_name, track_id
           ) WHERE rn = 1
           ON CONFLICT(artist_name, album_name) DO NOTHING"""
    )
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM album_groups").fetchone()[0]


def link_from_cache(conn: sqlite3.Connection) -> int:
    """Enlaza grupos cuyo track representante ya está en caché. Sin red.

    `pull` cachea los tracks de tus tops y playlists, y una corrida previa de
    `enrich` puede haber dejado más. Todo eso resuelve grupos gratis, así que
    conviene agotarlo antes de gastar una sola petición.
    """
    cur = conn.execute(
        """UPDATE album_groups
              SET album_id = (SELECT t.album_id FROM tracks t
                               WHERE t.track_id = album_groups.rep_track_id),
                  resolved_at = ?
            WHERE album_id IS NULL
              AND rep_track_id IN (SELECT track_id FROM tracks
                                    WHERE album_id IS NOT NULL)""",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.commit()
    return cur.rowcount


def pending_albums(conn: sqlite3.Connection, budget: int = DEFAULT_ALBUM_BUDGET
                   ) -> list[tuple[str, str, str]]:
    """Grupos sin resolver, de más a menos escuchado.

    Solo cuentan las reproducciones que superan el umbral de skip: un disco que
    solo has saltado no merece una petición.
    """
    rows = conn.execute(
        """SELECT g.artist_name, g.album_name, g.rep_track_id,
                  SUM(COALESCE(p.ms_played, 0)) AS ms
             FROM album_groups g
             JOIN plays p ON p.artist_name = g.artist_name
                         AND p.album_name  = g.album_name
            WHERE g.album_id IS NULL
              AND g.rep_track_id IS NOT NULL
              AND COALESCE(p.ms_played, 0) >= ?
            GROUP BY g.artist_name, g.album_name
            ORDER BY ms DESC
            LIMIT ?""",
        (SKIP_THRESHOLD_MS, budget),
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def run(conn: sqlite3.Connection, client: Client,
        budget: int = DEFAULT_ALBUM_BUDGET,
        progress_every: int = 50) -> dict[str, object]:
    """Resuelve álbumes hasta agotar el presupuesto o toparse con la cuota."""
    total_groups = build_album_groups(conn)
    desde_cache = link_from_cache(conn)
    pending = pending_albums(conn, budget)

    stats: dict[str, object] = {
        "grupos_totales": total_groups,
        "desde_cache": desde_cache,
        "pedidos": 0,
        "resueltos": 0,
        "no_encontrados": 0,
        "cortado_por_cuota": False,
    }
    if not pending:
        return stats

    now = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()

    for i, (artist_name, album_name, rep_track_id) in enumerate(pending, start=1):
        try:
            track = client.track(rep_track_id)
        except RateLimited as exc:
            # Cuota agotada. Lo hecho ya está commiteado; se avisa y se sale
            # limpiamente en vez de dormir horas.
            conn.commit()
            stats["cortado_por_cuota"] = True
            stats["retry_after_h"] = round(exc.retry_after / 3600, 1)
            print(f"\n  {exc}", flush=True)
            break

        stats["pedidos"] = int(stats["pedidos"]) + 1

        if track is None:
            conn.execute(
                "INSERT OR REPLACE INTO unresolved (track_id, reason, tried_at) "
                "VALUES (?,?,?)",
                (rep_track_id, "404", now),
            )
            stats["no_encontrados"] = int(stats["no_encontrados"]) + 1
        else:
            album = track.get("album") or {}
            _cache_album(conn, album)
            _cache_track(conn, track)
            if album.get("id"):
                conn.execute(
                    """UPDATE album_groups SET album_id = ?, resolved_at = ?
                        WHERE artist_name = ? AND album_name = ?""",
                    (album["id"], now, artist_name, album_name),
                )
                stats["resueltos"] = int(stats["resueltos"]) + 1

        if i % progress_every == 0:
            conn.commit()
            elapsed = time.monotonic() - started
            rate = i / elapsed if elapsed else 0
            eta = (len(pending) - i) / rate / 60 if rate else 0
            print(f"  {i:,}/{len(pending):,} ({i/len(pending)*100:.1f}%) · "
                  f"{rate:.1f} req/s · faltan ~{eta:.0f} min", flush=True)

    conn.commit()
    return stats


# Compatibilidad: `status` y el CLI siguen preguntando por tracks sueltos.
def pending_track_ids(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """SELECT DISTINCT p.track_id
             FROM plays p
        LEFT JOIN tracks t     ON t.track_id = p.track_id
        LEFT JOIN unresolved u ON u.track_id = p.track_id
            WHERE p.track_id IS NOT NULL
              AND t.track_id IS NULL
              AND u.track_id IS NULL"""
    ).fetchall()
    return [r[0] for r in rows]
