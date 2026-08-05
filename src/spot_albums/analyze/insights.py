"""Lecturas derivadas del ranking: lo que el número solo no te dice."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from ..devices import Device, get as get_device
from .scoring import AlbumScore


@dataclass
class ArtistGap:
    """Artista que escuchas mucho pero del que conoces poco."""
    artist_id: str | None
    artist_name: str
    hours: float
    distinct_tracks: int
    albums_touched: int
    best_album: str
    best_album_breadth: float


def album_listeners(albums: list[AlbumScore], min_breadth: float = 0.6,
                    top: int = 40) -> list[AlbumScore]:
    """Discos que escuchas *enteros*. Los que quieres sí o sí en el DAP."""
    picked = [a for a in albums if a.breadth >= min_breadth and a.distinct_tracks >= 4]
    return picked[:top]

def single_listeners(albums: list[AlbumScore], max_breadth: float = 0.25,
                     min_hours: float = 1.0, top: int = 40) -> list[AlbumScore]:
    """Mucho tiempo, poquísimos temas: el disco entero probablemente no te hace falta.

    Aquí es donde se ahorra espacio de verdad. Cada uno de estos que descartas
    son ~350 MB que van a un álbum que sí escuchas completo.
    """
    picked = [
        a for a in albums
        if a.breadth <= max_breadth and a.hours >= min_hours and a.total_tracks >= 5
    ]
    picked.sort(key=lambda a: a.hours, reverse=True)
    return picked[:top]


def discovery_gaps(albums: list[AlbumScore], min_hours: float = 3.0,
                   top: int = 25) -> list[ArtistGap]:
    """Artistas con muchas horas repartidas en muy pocos temas.

    Son el mejor retorno por hora invertida: ya sabes que te gusta el artista,
    solo no has escuchado su obra. El "mejor álbum" que se sugiere es aquel
    donde ya tienes mayor breadth — el punto de entrada natural.
    """
    by_artist: dict[str, list[AlbumScore]] = defaultdict(list)
    for a in albums:
        by_artist[a.artist_name].append(a)

    gaps: list[ArtistGap] = []
    for artist_name, items in by_artist.items():
        hours = sum(a.hours for a in items)
        if hours < min_hours:
            continue
        distinct = sum(a.distinct_tracks for a in items)
        # Pocas canciones distintas para todo ese tiempo -> hueco de descubrimiento.
        if distinct > 12:
            continue
        best = max(items, key=lambda a: a.breadth)
        gaps.append(
            ArtistGap(
                artist_id=items[0].artist_id,
                artist_name=artist_name,
                hours=hours,
                distinct_tracks=distinct,
                albums_touched=len(items),
                best_album=best.name,
                best_album_breadth=best.breadth,
            )
        )

    gaps.sort(key=lambda g: g.hours / max(g.distinct_tracks, 1), reverse=True)
    return gaps[:top]


def yearly_evolution(conn: sqlite3.Connection) -> list[dict]:
    """Horas y artistas distintos por año. Solo tiene sentido con el export GDPR."""
    rows = conn.execute(
        """SELECT substr(ts, 1, 4) AS year,
                  SUM(COALESCE(ms_played, 0)) / 3600000.0 AS hours,
                  COUNT(DISTINCT artist_name) AS artists,
                  COUNT(DISTINCT track_id) AS tracks,
                  COUNT(*) AS plays
             FROM plays
            WHERE source = 'gdpr'
            GROUP BY year
            ORDER BY year"""
    ).fetchall()
    return [dict(r) for r in rows]


def top_artists_by_hours(conn: sqlite3.Connection, top: int = 25) -> list[dict]:
    rows = conn.execute(
        """SELECT artist_name,
                  SUM(COALESCE(ms_played, 0)) / 3600000.0 AS hours,
                  COUNT(DISTINCT track_id) AS tracks,
                  COUNT(*) AS plays
             FROM plays
            WHERE artist_name IS NOT NULL
            GROUP BY artist_name
            ORDER BY hours DESC
            LIMIT ?""",
        (top,),
    ).fetchall()
    return [dict(r) for r in rows]


def assign_tiers(albums: list[AlbumScore],
                 device: Device | None = None) -> dict[str, list[AlbumScore]]:
    """Reparte el ranking en el almacenamiento real del reproductor.

    tier0: memoria interna — lo que llevas aunque olvides la tarjeta.
    tier1: el resto que cabe en la microSD.
    resto: no cabe; para una segunda tarjeta o para descartar.

    Los márgenes de usable salen del perfil: los DAPs se ponen raros con la
    tarjeta llena y el firmware necesita hueco para su base de datos.
    """
    device = device or get_device()
    internal_mb = device.internal_mb
    sd_mb = device.card_mb

    tiers: dict[str, list[AlbumScore]] = {"tier0": [], "tier1": [], "resto": []}
    used_internal = 0.0
    used_sd = 0.0

    for album in albums:
        if used_internal + album.est_mb <= internal_mb:
            tiers["tier0"].append(album)
            used_internal += album.est_mb
        elif used_sd + album.est_mb <= sd_mb:
            tiers["tier1"].append(album)
            used_sd += album.est_mb
        else:
            tiers["resto"].append(album)
    return tiers


def evidence_mode(conn: sqlite3.Connection) -> str:
    """Con qué datos estamos trabajando. Cambia cuánto vale el ranking."""
    gdpr = conn.execute(
        "SELECT COUNT(*) FROM plays WHERE source = 'gdpr'"
    ).fetchone()[0]
    api = conn.execute("SELECT COUNT(*) FROM top_items").fetchone()[0]
    if gdpr and api:
        return "completo"
    if gdpr:
        return "solo-export"
    if api:
        return "solo-api"
    return "vacio"


def summary(conn: sqlite3.Connection, albums: list[AlbumScore],
            device: Device | None = None) -> dict:
    device = device or get_device()
    tiers = assign_tiers(albums, device)
    total_hours = sum(a.hours for a in albums)
    return {
        "dispositivo": device.name,
        "modo": evidence_mode(conn),
        "albumes_rankeados": len(albums),
        "horas_totales": total_hours,
        "tier0": len(tiers["tier0"]),
        "tier1": len(tiers["tier1"]),
        "resto": len(tiers["resto"]),
        "gb_tier0": sum(a.est_mb for a in tiers["tier0"]) / 1024,
        "gb_tier1": sum(a.est_mb for a in tiers["tier1"]) / 1024,
    }
