"""Ranking de álbumes a partir de la evidencia de escucha.

El problema no es "qué artistas me gustan" —eso ya lo sabes— sino **qué discos
merecen uno de los ~650 huecos que caben en 256 GB de FLAC**. Son preguntas
distintas: puedes tener 400 horas de un artista y no querer ninguno de sus
álbumes completos porque siempre escuchas los mismos dos singles.

Cinco señales, cada una normalizada a 0..1:

  volumen       cuánto tiempo le has dedicado (log-escalado: la diferencia
                entre 1 y 10 horas importa mucho más que entre 200 y 210)
  breadth       temas distintos escuchados / total del disco  ← LA SEÑAL CLAVE
  recencia      decaimiento exponencial, half-life 6 meses; responde a
                "lo que escucho hoy en día", no a lo que escuchaba en 2016
  intención     no-skip + escucha secuencial sin shuffle: comportamiento de
                quien pone un disco, no de quien deja sonar una radio
  confirmación  aparece en tus tops actuales o lo tienes guardado

Nota sobre `audio_features`: Spotify mató ese endpoint en nov-2024 sin
reemplazo, así que aquí no hay nada de "energy/danceability". Todo el ranking
sale de comportamiento observado, que para este fin es mejor dato de todos modos.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..config import (
    DEFAULT_ALBUM_MINUTES,
    FLAC_MB_PER_MINUTE,
    RECENCY_HALF_LIFE_DAYS,
    SKIP_THRESHOLD_MS,
)
from ..titles import album_key, track_key

# Pesos de la combinación final.
#
# Breadth manda porque es la señal que separa "quiero el disco" de "conozco el
# single". Volumen va segundo: con 12 años de historial, las horas acumuladas
# son la medida más honesta de compromiso.
#
# La recencia se dejó en 0.17 deliberadamente. Más alta (0.25) hacía que un
# disco recién descubierto de 4 h le ganara a uno de 82 h, porque con el
# breadth saturado al 100% en ambos ya no queda nada que los separe. Un DAP se
# llena para meses, así que un descubrimiento reciente debe poder subir, pero
# no barrer a lo que llevas años escuchando.
WEIGHTS = {
    "volume": 0.33,
    "breadth": 0.30,
    "recency": 0.17,
    "intent": 0.10,
    "confirmation": 0.10,
}

# `reason_start == 'trackdone'` significa que el tema anterior terminó y este
# siguió solo. Contraintuitivamente eso es señal *buena* aquí: es exactamente
# lo que pasa cuando alguien deja correr un disco de principio a fin.
SEQUENTIAL_START = "trackdone"


@dataclass
class AlbumScore:
    album_id: str
    name: str
    artist_name: str
    artist_id: str | None
    release_year: int | None
    total_tracks: int
    album_type: str
    hours: float
    plays: int
    distinct_tracks: int
    breadth: float
    recency: float
    intent: float
    confirmation: float
    volume: float
    score: float
    saved: bool
    est_mb: int
    last_played: str | None
    signals: dict = field(default_factory=dict)


def _decay(ts: str, now: datetime) -> float:
    """Peso exponencial por antigüedad. 1.0 hoy, 0.5 a los 6 meses."""
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    days = max((now - when).total_seconds() / 86400.0, 0.0)
    return 0.5 ** (days / RECENCY_HALF_LIFE_DAYS)


def _confirmation(conn: sqlite3.Connection) -> dict[str, float]:
    """0..1 por álbum según lo confirme tu estado actual en Spotify.

    Cruza el histórico (que puede tener años) con lo que Spotify dice que
    escuchas ahora. Un disco con muchas horas *y* presencia en tus tops de hoy
    es una apuesta mucho más segura que uno que solo tiene horas.
    """
    scores: dict[str, float] = {}

    # Cuántos álbumes has guardado de cada artista. Guardar 13 discos de
    # alguien es una declaración de intenciones muy distinta a guardar uno
    # suelto, y es de las pocas señales fuertes disponibles antes del export.
    # Escala logarítmica: de 1 a 4 álbumes la diferencia importa mucho; de 10
    # a 13, casi nada.
    depth = {
        row[0]: row[1]
        for row in conn.execute(
            """SELECT a.artist_id, COUNT(*) FROM saved_albums s
                 JOIN albums a ON a.album_id = s.album_id
                WHERE a.artist_id IS NOT NULL
                GROUP BY a.artist_id"""
        )
    }
    max_depth = max(depth.values(), default=1)
    log_depth = math.log1p(max_depth) or 1.0

    for album_id, artist_id in conn.execute(
        """SELECT s.album_id, a.artist_id FROM saved_albums s
             LEFT JOIN albums a ON a.album_id = s.album_id"""
    ):
        bonus = 0.0
        if artist_id and artist_id in depth:
            bonus = 0.35 * (math.log1p(depth[artist_id]) / log_depth)
        scores[album_id] = scores.get(album_id, 0.0) + 0.45 + bonus

    # Tracks en el top actual -> peso por rango y posición.
    range_weight = {"short_term": 1.0, "medium_term": 0.8, "long_term": 0.6}
    latest = conn.execute("SELECT MAX(snapshot_id) FROM snapshots").fetchone()[0]
    if latest:
        rows = conn.execute(
            """SELECT ti.album_id, ti.time_range, ti.rank
                 FROM top_items ti
                WHERE ti.kind = 'track' AND ti.snapshot_id = ?
                  AND ti.album_id IS NOT NULL""",
            (latest,),
        )
        for album_id, time_range, rank in rows:
            w = range_weight.get(time_range, 0.6) * (51 - min(rank, 50)) / 50
            scores[album_id] = scores.get(album_id, 0.0) + 0.3 * w

        # Artista en el top -> refuerzo suave a todos sus álbumes.
        top_artists = {
            r[0]: range_weight.get(r[1], 0.6) * (51 - min(r[2], 50)) / 50
            for r in conn.execute(
                """SELECT item_id, time_range, rank FROM top_items
                    WHERE kind = 'artist' AND snapshot_id = ?""",
                (latest,),
            )
        }
        if top_artists:
            placeholders = ",".join("?" * len(top_artists))
            rows = conn.execute(
                f"SELECT album_id, artist_id FROM albums WHERE artist_id IN ({placeholders})",
                tuple(top_artists),
            )
            for album_id, artist_id in rows:
                scores[album_id] = scores.get(album_id, 0.0) + 0.2 * top_artists[artist_id]

    return {k: min(v, 1.0) for k, v in scores.items()}


RANGE_WEIGHT = {"short_term": 1.0, "medium_term": 0.8, "long_term": 0.6}
# Cuánta recencia implica cada rango cuando no hay `plays` que la midan.
RANGE_RECENCY = {"short_term": 1.0, "medium_term": 0.7, "long_term": 0.45}


def _top_track_evidence(conn: sqlite3.Connection) -> dict[str, dict]:
    """Evidencia de escucha derivada de /me/top/tracks.

    Los tops SON evidencia de escucha —Spotify los calcula con tus
    reproducciones— así que sus tracks cuentan para breadth. Es la única forma
    de que el ranking valga algo antes de que llegue el export GDPR, donde 50
    reproducciones recientes es todo lo que la API te deja ver.
    """
    latest = conn.execute("SELECT MAX(snapshot_id) FROM snapshots").fetchone()[0]
    if not latest:
        return {}

    out: dict[str, dict] = {}
    rows = conn.execute(
        """SELECT album_id, item_id, time_range, rank FROM top_items
            WHERE kind = 'track' AND snapshot_id = ? AND album_id IS NOT NULL""",
        (latest,),
    )
    for album_id, track_id, time_range, rank in rows:
        bucket = out.setdefault(album_id, {"tracks": set(), "weight": 0.0, "recency": 0.0})
        bucket["tracks"].add(track_id)
        rank_w = (51 - min(rank, 50)) / 50
        bucket["weight"] = max(bucket["weight"], RANGE_WEIGHT.get(time_range, 0.6) * rank_w)
        bucket["recency"] = max(bucket["recency"], RANGE_RECENCY.get(time_range, 0.45))
    return out


def _saved_evidence(conn: sqlite3.Connection, now: datetime) -> dict[str, float]:
    """Álbumes guardados. Devuelve un suelo de recencia, no una medida.

    Ojo con la trampa: `added_at` es **cuándo guardaste el disco, no cuándo lo
    escuchaste**. Decayendo por esa fecha, un álbum guardado en 2018 y
    escuchado ayer sale con recencia cero. Guardar algo es una preferencia
    duradera, no un evento que caduque.

    Así que un guardado reciente sí empuja la recencia hacia arriba, pero uno
    viejo nunca la hunde por debajo de un suelo neutro: significa "no sé
    cuándo lo escuchaste", no "hace años que no lo escuchas".
    """
    FLOOR = 0.35
    out: dict[str, float] = {}
    for album_id, added_at in conn.execute(
        "SELECT album_id, added_at FROM saved_albums"
    ):
        out[album_id] = max(_decay(added_at, now), FLOOR) if added_at else FLOOR
    return out


def score_albums(
    conn: sqlite3.Connection,
    include_singles: bool = False,
    min_hours: float = 0.0,
) -> list[AlbumScore]:
    """Calcula el ranking completo. Ordenado de mejor a peor."""
    now = datetime.now(timezone.utc)

    meta_by_id = {
        r["album_id"]: r
        for r in conn.execute(
            """SELECT album_id, name, artist_name, artist_id, release_year,
                      total_tracks, album_type FROM albums"""
        )
    }

    # Consolidación de ediciones. Un mismo disco vive bajo varios album_id
    # (edición regional, reedición, aniversario) y sin unirlos la wantlist lo
    # repite tres veces mientras reparte su evidencia entre las copias, con lo
    # que puntúa por debajo de lo que le toca.
    #
    # Se agrupa por (artista, título) normalizados y se elige un representante:
    # el que declara más temas —la edición completa, no un EP recortado— y a
    # igualdad, el lanzamiento original.
    canon_of: dict[str, tuple[str, str]] = {}
    reps: dict[tuple[str, str], sqlite3.Row] = {}
    for album_id, meta in meta_by_id.items():
        key = album_key(meta["artist_name"], meta["name"])
        if key == ("", ""):
            key = ("", album_id)  # sin metadatos: que no colapse con otros
        canon_of[album_id] = key
        best = reps.get(key)
        if best is None or (
            (meta["total_tracks"] or 0, -(meta["release_year"] or 9999))
            > (best["total_tracks"] or 0, -(best["release_year"] or 9999))
        ):
            reps[key] = meta

    # Una reproducción se asocia a su álbum por dos vías, en este orden:
    #   1. `tracks.album_id`, si ese track concreto se resolvió por la API.
    #   2. `album_groups`, que enlaza el (artista, álbum) que ya venía en el
    #      export con el álbum del catálogo. Cubre las decenas de miles de
    #      reproducciones cuyo track nunca se resolvió una a una.
    #
    # Para contar temas distintos se usa el NOMBRE, no el id: la misma canción
    # aparece con URIs distintas entre remasters y reediciones, y contarlas
    # por separado infla el breadth justo en los discos más reeditados.
    rows = conn.execute(
        """SELECT COALESCE(t.album_id, g.album_id) AS album_id,
                  COALESCE(p.track_name, p.track_id) AS track_key,
                  p.ts, p.ms_played, p.reason_start, p.shuffle, t.duration_ms
             FROM plays p
        LEFT JOIN tracks t       ON t.track_id = p.track_id
        LEFT JOIN album_groups g ON g.artist_name = p.artist_name
                                AND g.album_name  = p.album_name
            WHERE COALESCE(t.album_id, g.album_id) IS NOT NULL"""
    ).fetchall()

    def bucket_for(key: tuple[str, str]) -> dict:
        b = acc.get(key)
        if b is None:
            b = acc[key] = {
                "ms": 0.0,
                "ms_recent": 0.0,
                "plays": 0,
                "skips": 0,
                "sequential": 0,
                "real_tracks": set(),
                "last": None,
            }
        return b

    acc: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = canon_of.get(r["album_id"])
        if key is None:
            continue
        bucket = bucket_for(key)

        # /me/player/recently-played no da ms_played; asumimos escucha completa.
        ms = r["ms_played"]
        if ms is None:
            ms = r["duration_ms"] or 0
        ms = float(ms)

        bucket["ms"] += ms
        bucket["ms_recent"] += ms * _decay(r["ts"], now)
        bucket["plays"] += 1

        # El campo `skipped` del export es inservible (Spotify no registró
        # skips entre 2015-04 y 2022-10), así que lo derivamos del tiempo.
        if ms < SKIP_THRESHOLD_MS:
            bucket["skips"] += 1
        else:
            # Clave normalizada: `Bigmouth Strikes Again` y
            # `Bigmouth Strikes Again - 2011 Remaster` son el mismo tema, y
            # contarlos aparte dispara el breadth por encima de 1.0.
            bucket["real_tracks"].add(track_key(r["track_key"]))

        if r["reason_start"] == SEQUENTIAL_START and not r["shuffle"]:
            bucket["sequential"] += 1

        if bucket["last"] is None or r["ts"] > bucket["last"]:
            bucket["last"] = r["ts"]

    confirmations = _confirmation(conn)
    saved_recency = _saved_evidence(conn, now)
    saved = set(saved_recency)
    top_evidence = _top_track_evidence(conn)

    # Las señales de la API vienen por album_id; hay que plegarlas a la clave
    # canónica o un guardado de la edición deluxe no confirmaría al disco
    # consolidado.
    def fold(by_id: dict) -> dict:
        out: dict[tuple[str, str], float] = {}
        for album_id, value in by_id.items():
            key = canon_of.get(album_id)
            if key is not None:
                out[key] = max(out.get(key, 0.0), value)
        return out

    confirmations = fold(confirmations)
    saved_recency = fold(saved_recency)
    saved = {canon_of[a] for a in saved if a in canon_of}

    folded_top: dict[tuple[str, str], dict] = {}
    for album_id, ev in top_evidence.items():
        key = canon_of.get(album_id)
        if key is None:
            continue
        cur = folded_top.setdefault(
            key, {"tracks": set(), "weight": 0.0, "recency": 0.0}
        )
        cur["tracks"] |= ev["tracks"]
        cur["weight"] = max(cur["weight"], ev["weight"])
        cur["recency"] = max(cur["recency"], ev["recency"])
    top_evidence = folded_top

    # Un álbum entra al ranking si tiene *cualquier* evidencia. Restringirlo a
    # los que tienen `plays` tiraría a la basura los álbumes guardados y los
    # tops — que es justo todo lo que hay antes de que llegue el export GDPR.
    for key in list(top_evidence) + list(saved):
        if key in reps:
            bucket_for(key)

    # Normalización del volumen contra el máximo del propio dataset: el score
    # es relativo a TU biblioteca, no a una escala absoluta inventada.
    max_hours = max((b["ms"] for b in acc.values()), default=1.0) / 3_600_000 or 1.0
    log_max = math.log1p(max_hours)

    out: list[AlbumScore] = []
    for key, b in acc.items():
        meta = reps.get(key)
        if meta is None:
            continue
        album_id = meta["album_id"]
        album_type = meta["album_type"] or "album"
        if not include_singles and album_type != "album":
            continue

        hours = b["ms"] / 3_600_000
        if hours < min_hours:
            continue

        top = top_evidence.get(key)
        total_tracks = meta["total_tracks"] or 0

        # Breadth: temas escuchados de verdad. Los tops de Spotify cuentan
        # porque él los calcula con tus reproducciones — es evidencia de
        # escucha, no una lista que hiciste a mano.
        listened = set(b["real_tracks"])
        if top:
            listened |= top["tracks"]
        distinct = len(listened)
        breadth = min(distinct / total_tracks, 1.0) if total_tracks else 0.0

        # Volumen: horas reales cuando las hay. Si no (modo solo-API), se
        # aproxima con la posición en tus tops, topada para que nunca supere
        # a un álbum con escucha medida de verdad.
        volume = math.log1p(hours) / log_max if log_max else 0.0
        if top:
            volume = max(volume, top["weight"] * 0.6)

        # Recencia: de las reproducciones si las hay; si no, del rango temporal
        # del top donde aparece y de cuándo guardaste el álbum.
        if b["ms"]:
            recency = b["ms_recent"] / b["ms"]
        else:
            recency = 0.0
        if top:
            recency = max(recency, top["recency"])
        recency = max(recency, saved_recency.get(key, 0.0))

        if b["plays"]:
            skip_rate = b["skips"] / b["plays"]
            sequential_ratio = b["sequential"] / b["plays"]
            intent = 0.5 * (1 - skip_rate) + 0.5 * min(sequential_ratio * 2, 1.0)
        else:
            # Sin reproducciones no hay nada que medir: neutro, ni premio ni
            # castigo. Inventar un valor alto falsearía el ranking.
            skip_rate = 0.0
            sequential_ratio = 0.0
            intent = 0.5

        confirmation = confirmations.get(key, 0.0)

        score = 100 * (
            WEIGHTS["volume"] * volume
            + WEIGHTS["breadth"] * breadth
            + WEIGHTS["recency"] * recency
            + WEIGHTS["intent"] * intent
            + WEIGHTS["confirmation"] * confirmation
        )

        minutes = (total_tracks * 4.0) if total_tracks else DEFAULT_ALBUM_MINUTES
        out.append(
            AlbumScore(
                album_id=album_id,
                name=meta["name"] or "(sin título)",
                artist_name=meta["artist_name"] or "(desconocido)",
                artist_id=meta["artist_id"],
                release_year=meta["release_year"],
                total_tracks=total_tracks,
                album_type=album_type,
                hours=hours,
                plays=b["plays"],
                distinct_tracks=distinct,
                breadth=breadth,
                recency=recency,
                intent=intent,
                confirmation=confirmation,
                volume=volume,
                score=score,
                saved=key in saved,
                est_mb=int(minutes * FLAC_MB_PER_MINUTE),
                last_played=b["last"],
                signals={
                    "skip_rate": skip_rate,
                    "sequential_ratio": sequential_ratio,
                    # De dónde sale la evidencia. Distingue un ranking ganado
                    # de uno heredado de haberle dado a "guardar" hace años.
                    "evidence": (
                        "reproducciones" if b["plays"]
                        else "tops" if top
                        else "solo-guardado"
                    ),
                },
            )
        )

    out.sort(key=lambda a: a.score, reverse=True)
    return out
