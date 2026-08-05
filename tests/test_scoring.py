"""El ranking tiene que preferir discos sobre singles. Eso es todo el proyecto."""

from __future__ import annotations

from spot_albums.analyze import insights
from spot_albums.analyze.scoring import score_albums
from spot_albums.ingest import gdpr


def _by_name(albums):
    return {a.name: a for a in albums}


# --------------------------------------------------------------------- ingest
def test_ingest_filtra_podcasts_y_video(seeded_db):
    """Solo entran reproducciones de audio con spotify_track_uri."""
    rows = seeded_db.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
    # 60 del álbum + 60 del hit + 60 del viejo + 5 skips = 185
    assert rows == 185

    sin_uri = seeded_db.execute(
        "SELECT COUNT(*) FROM plays WHERE track_uri IS NULL"
    ).fetchone()[0]
    assert sin_uri == 0


def test_ingest_es_idempotente(seeded_db, export_zip):
    """Reingerir el mismo ZIP no debe duplicar ni una fila."""
    antes = seeded_db.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
    stats = gdpr.ingest(seeded_db, export_zip)
    despues = seeded_db.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
    assert despues == antes
    assert stats["nuevas"] == 0
    assert stats["duplicadas"] == stats["leidas"]


# -------------------------------------------------------------------- breadth
def test_breadth_distingue_album_de_single(seeded_db):
    albums = _by_name(score_albums(seeded_db))

    completo = albums["The Whole Thing"]
    single = albums["Album With One Hit"]

    assert completo.distinct_tracks == 10
    assert completo.breadth == 1.0

    assert single.distinct_tracks == 1
    assert single.breadth < 0.1

    # Horas parecidas (60 reproducciones cada uno) pero el disco gana el ranking:
    # es exactamente la decisión que el proyecto tiene que acertar.
    assert abs(completo.hours - single.hours) < 0.5
    assert completo.score > single.score


def test_el_album_completo_encabeza_el_ranking(seeded_db):
    albums = score_albums(seeded_db)
    assert albums[0].name == "The Whole Thing"


# ------------------------------------------------------------------- recencia
def test_la_recencia_hunde_al_favorito_viejo(seeded_db):
    albums = _by_name(score_albums(seeded_db))
    reciente = albums["The Whole Thing"]
    viejo = albums["Back Then"]

    # Mismo patrón de escucha, misma breadth: solo cambia cuándo.
    assert viejo.breadth == reciente.breadth == 1.0
    assert viejo.recency < 0.05
    assert reciente.recency > 0.8
    assert reciente.score > viejo.score


# ----------------------------------------------------------------------- skip
def test_los_skips_salen_de_ms_played_no_del_campo_skipped(seeded_db):
    """El campo `skipped` de Spotify miente en años de historial.

    En el fixture, los 5 plays de 'Skipped Album' traen skipped=False pero solo
    4 segundos de reproducción. Deben contar como skip igual.
    """
    albums = _by_name(score_albums(seeded_db))
    skippy = albums["Skipped Album"]

    assert skippy.distinct_tracks == 0        # ningún tema escuchado de verdad
    assert skippy.breadth == 0.0
    assert skippy.signals["skip_rate"] == 1.0

    # Y el `skipped` crudo en la base efectivamente no marcaba nada.
    marcados = seeded_db.execute(
        "SELECT COUNT(*) FROM plays WHERE track_id LIKE 'skip%'"
    ).fetchone()[0]
    assert marcados == 5


def test_el_favorito_viejo_no_se_penaliza_por_el_bug_de_skipped(seeded_db):
    """Plays de 2022 con skipped=None son escuchas completas, no skips."""
    albums = _by_name(score_albums(seeded_db))
    assert albums["Back Then"].signals["skip_rate"] == 0.0


# -------------------------------------------------------------------- filtros
def test_min_hours_descarta_lo_marginal(seeded_db):
    todos = score_albums(seeded_db)
    filtrados = score_albums(seeded_db, min_hours=1.0)
    assert len(filtrados) < len(todos)
    assert all(a.hours >= 1.0 for a in filtrados)


# ------------------------------------------------------------------- insights
def test_single_listeners_pilla_el_disco_de_un_solo_hit(seeded_db):
    albums = score_albums(seeded_db)
    nombres = [a.name for a in insights.single_listeners(albums, min_hours=0.5)]
    assert "Album With One Hit" in nombres
    assert "The Whole Thing" not in nombres


def test_album_listeners_pilla_el_disco_completo(seeded_db):
    albums = score_albums(seeded_db)
    nombres = [a.name for a in insights.album_listeners(albums)]
    assert "The Whole Thing" in nombres
    assert "Album With One Hit" not in nombres


def test_los_tiers_respetan_el_presupuesto(seeded_db):
    albums = score_albums(seeded_db)
    tiers = insights.assign_tiers(albums)
    # Con 4 álbumes todo cabe de sobra en los 8 GB internos.
    assert len(tiers["tier0"]) == len(albums)
    assert tiers["resto"] == []

    gb = sum(a.est_mb for a in tiers["tier0"]) / 1024
    assert gb < 8


def test_modo_de_evidencia_sin_snapshot_api(seeded_db):
    assert insights.evidence_mode(seeded_db) == "solo-export"
