"""La wantlist: el entregable con el que vas a buscar la música.

Cada fila lleva por qué está ahí (horas, breadth, tier) y links de búsqueda
directos, para que no tengas que copiar y pegar nombres a mano 600 veces.
"""

from __future__ import annotations

import csv
import urllib.parse
from pathlib import Path

from ..analyze.insights import assign_tiers
from ..analyze.scoring import AlbumScore

COLUMNS = [
    "tier", "rank", "artista", "album", "año", "tracks", "horas",
    "breadth_pct", "recencia_pct", "score", "evidencia", "guardado_spotify",
    "mb_estimados", "ultima_escucha", "bandcamp", "qobuz", "discogs",
]


def _links(album: AlbumScore) -> dict[str, str]:
    q = urllib.parse.quote_plus(f"{album.artist_name} {album.name}")
    return {
        "bandcamp": f"https://bandcamp.com/search?q={q}",
        "qobuz": f"https://www.qobuz.com/us-en/search?q={q}",
        "discogs": f"https://www.discogs.com/search/?q={q}&type=release",
    }


def _rows(albums: list[AlbumScore], device=None) -> list[dict]:
    tiers = assign_tiers(albums, device)
    tier_of = {}
    for name, items in tiers.items():
        for album in items:
            tier_of[album.album_id] = name

    rows = []
    for i, album in enumerate(albums, start=1):
        links = _links(album)
        rows.append({
            "tier": tier_of.get(album.album_id, "resto"),
            "rank": i,
            "artista": album.artist_name,
            "album": album.name,
            "año": album.release_year or "",
            "tracks": album.total_tracks,
            "horas": round(album.hours, 1),
            "breadth_pct": round(album.breadth * 100),
            "recencia_pct": round(album.recency * 100),
            "score": round(album.score, 1),
            "evidencia": album.signals.get("evidence", ""),
            "guardado_spotify": "sí" if album.saved else "",
            "mb_estimados": album.est_mb,
            "ultima_escucha": (album.last_played or "")[:10],
            **links,
        })
    return rows


def write_csv(albums: list[AlbumScore], path: Path, device=None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(_rows(albums, device))
    return path


def write_markdown(albums: list[AlbumScore], path: Path, limit: int = 200,
                   device=None) -> Path:
    rows = _rows(albums, device)[:limit]
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Wantlist — álbumes a conseguir",
        "",
        "Ordenada por score. `breadth` = % de temas del disco que ya escuchaste: "
        "cuanto más alto, más seguro es que quieres el álbum entero.",
        "",
        "| # | Tier | Artista | Álbum | Año | Breadth | Horas | MB | Buscar |",
        "|---|------|---------|-------|-----|---------|-------|-----|--------|",
    ]
    for r in rows:
        buscar = (
            f"[BC]({r['bandcamp']}) · [QB]({r['qobuz']}) · [DC]({r['discogs']})"
        )
        lines.append(
            f"| {r['rank']} | {r['tier']} | {r['artista']} | {r['album']} | "
            f"{r['año']} | {r['breadth_pct']}% | {r['horas']} | {r['mb_estimados']} | {buscar} |"
        )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
