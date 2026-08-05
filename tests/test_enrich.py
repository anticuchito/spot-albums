"""Resolución de álbumes, con un cliente falso.

El bucle de red de `enrich` es donde el proyecto se estrelló: resolviendo track
a track agotó la cuota y Spotify devolvió un Retry-After de 23 h, que el código
se puso a dormir en silencio. La reescritura resuelve por álbum, pero no hay
forma de ejercitarla contra la API sin gastar cuota — así que se prueba contra
un doble que imita las tres respuestas que importan: track, 404 y 429.
"""

from __future__ import annotations

import pytest

from spot_albums import enrich
from spot_albums.spotify.client import RateLimited


class FakeClient:
    """Imita `Client.track()`. Cuenta llamadas para verificar el coste."""

    def __init__(self, catalogo: dict[str, dict] | None = None,
                 falla_tras: int | None = None, retry_after: int = 82_661):
        self.catalogo = catalogo or {}
        self.falla_tras = falla_tras
        self.retry_after = retry_after
        self.llamadas: list[str] = []

    def track(self, track_id: str) -> dict | None:
        if self.falla_tras is not None and len(self.llamadas) >= self.falla_tras:
            raise RateLimited(
                f"cuota agotada; reintenta en {self.retry_after/3600:.1f} h",
                retry_after=self.retry_after,
            )
        self.llamadas.append(track_id)
        return self.catalogo.get(track_id)


def _catalogo_del_fixture() -> dict[str, dict]:
    """Respuestas de /tracks/{id} para los tracks del export sintético."""
    albums = {
        "full": ("alb_full", "The Whole Thing", 10, "Album Artist"),
        "hit": ("alb_hit", "Album With One Hit", 12, "Hit Artist"),
        "old": ("alb_old", "Back Then", 10, "Nostalgia"),
    }
    out = {}
    for prefijo, (album_id, nombre, total, artista) in albums.items():
        for n in range(1, 11):
            tid = f"{prefijo}{n:02d}" if prefijo != "hit" else "hit00001"
            out[tid] = {
                "id": tid, "name": tid, "duration_ms": 210_000,
                "artists": [{"id": f"art_{album_id}", "name": artista}],
                "album": {
                    "id": album_id, "name": nombre, "album_type": "album",
                    "total_tracks": total, "release_date": "2024-01-01",
                    "artists": [{"id": f"art_{album_id}", "name": artista}],
                    "images": [],
                },
            }
    return out


@pytest.fixture
def db_sin_catalogo(tmp_path, export_zip):
    """Export ingerido pero sin nada resuelto: el estado real tras `ingest`."""
    from spot_albums import db
    from spot_albums.ingest import gdpr

    conn = db.connect(tmp_path / "t.db")
    gdpr.ingest(conn, export_zip)
    return conn


# ------------------------------------------------------- agrupación sin red
def test_agrupa_por_album_sin_tocar_la_red(db_sin_catalogo):
    n = enrich.build_album_groups(db_sin_catalogo)
    assert n == 4  # los cuatro álbumes del fixture

    filas = db_sin_catalogo.execute(
        "SELECT artist_name, album_name, rep_track_id FROM album_groups"
    ).fetchall()
    assert all(r["rep_track_id"] for r in filas), "todo grupo necesita representante"


def test_el_representante_es_el_track_mas_escuchado(db_sin_catalogo):
    enrich.build_album_groups(db_sin_catalogo)
    rep = db_sin_catalogo.execute(
        "SELECT rep_track_id FROM album_groups WHERE album_name = 'Album With One Hit'"
    ).fetchone()[0]
    # Ese álbum solo tiene un track con escuchas: el hit.
    assert rep == "hit00001"


def test_agrupar_es_idempotente(db_sin_catalogo):
    assert enrich.build_album_groups(db_sin_catalogo) == 4
    assert enrich.build_album_groups(db_sin_catalogo) == 4


# ------------------------------------------------------------- una por álbum
def test_gasta_una_peticion_por_album_no_por_track(db_sin_catalogo):
    """El punto de todo el rediseño.

    El fixture tiene 26 tracks distintos en 4 álbumes. La versión vieja habría
    hecho 26 peticiones; esta hace como mucho una por álbum.
    """
    tracks_distintos = db_sin_catalogo.execute(
        "SELECT COUNT(DISTINCT track_id) FROM plays"
    ).fetchone()[0]
    assert tracks_distintos == 26

    client = FakeClient(_catalogo_del_fixture())
    stats = enrich.run(db_sin_catalogo, client)

    assert len(client.llamadas) <= 4
    assert stats["resueltos"] >= 3


def test_no_repite_lo_ya_resuelto(db_sin_catalogo):
    client = FakeClient(_catalogo_del_fixture())
    enrich.run(db_sin_catalogo, client)
    primera = len(client.llamadas)

    enrich.run(db_sin_catalogo, client)
    assert len(client.llamadas) == primera, "una segunda corrida no debe repetir"


def test_link_from_cache_no_gasta_red(db_sin_catalogo):
    """Los tracks que `pull` ya cacheó resuelven grupos gratis."""
    enrich.build_album_groups(db_sin_catalogo)
    db_sin_catalogo.execute(
        "INSERT INTO albums (album_id, name, total_tracks) VALUES ('alb_x','X',10)"
    )
    rep = db_sin_catalogo.execute(
        "SELECT rep_track_id FROM album_groups WHERE album_name='The Whole Thing'"
    ).fetchone()[0]
    db_sin_catalogo.execute(
        "INSERT INTO tracks (track_id, album_id) VALUES (?, 'alb_x')", (rep,)
    )
    db_sin_catalogo.commit()

    assert enrich.link_from_cache(db_sin_catalogo) == 1


# --------------------------------------------------------------- la cuota
def test_la_cuota_agotada_corta_en_vez_de_dormir(db_sin_catalogo):
    """El bug que colgó el proceso 23 horas en silencio.

    `run` tiene que salir con la información del bloqueo, no dormirlo.
    """
    client = FakeClient(_catalogo_del_fixture(), falla_tras=2)
    stats = enrich.run(db_sin_catalogo, client)

    assert stats["cortado_por_cuota"] is True
    assert stats["retry_after_h"] == pytest.approx(23.0, abs=0.1)
    assert len(client.llamadas) == 2, "no debe seguir pidiendo tras el 429"


def test_lo_resuelto_antes_del_corte_se_guarda(db_sin_catalogo):
    """Interrumpir no puede perder el trabajo ya pagado."""
    client = FakeClient(_catalogo_del_fixture(), falla_tras=2)
    enrich.run(db_sin_catalogo, client)

    resueltos = db_sin_catalogo.execute(
        "SELECT COUNT(*) FROM album_groups WHERE album_id IS NOT NULL"
    ).fetchone()[0]
    assert resueltos == 2

    # Y al volver, continúa por donde iba en vez de empezar de cero.
    client2 = FakeClient(_catalogo_del_fixture())
    enrich.run(db_sin_catalogo, client2)
    assert len(client2.llamadas) <= 2


def test_un_track_retirado_del_catalogo_no_tumba_la_corrida(db_sin_catalogo):
    """Un 404 debe registrarse y seguir, no propagarse."""
    catalogo = _catalogo_del_fixture()
    enrich.build_album_groups(db_sin_catalogo)
    reps = [r[0] for r in db_sin_catalogo.execute(
        "SELECT rep_track_id FROM album_groups")]
    catalogo.pop(reps[0], None)  # ese devuelve None = 404

    client = FakeClient(catalogo)
    stats = enrich.run(db_sin_catalogo, client)

    assert stats["no_encontrados"] >= 1
    assert stats["resueltos"] >= 1
    assert db_sin_catalogo.execute(
        "SELECT COUNT(*) FROM unresolved").fetchone()[0] >= 1


def test_el_presupuesto_acota_el_gasto(db_sin_catalogo):
    client = FakeClient(_catalogo_del_fixture())
    enrich.run(db_sin_catalogo, client, budget=2)
    assert len(client.llamadas) == 2


def test_pending_albums_ordena_por_tiempo_escuchado(db_sin_catalogo):
    """El orden es lo que hace útil una corrida interrumpida.

    Sin él, cortar a la mitad deja resuelto un trozo arbitrario del catálogo
    en vez de los discos que de verdad pesan en el ranking.
    """
    enrich.build_album_groups(db_sin_catalogo)
    pendientes = enrich.pending_albums(db_sin_catalogo)

    horas = []
    for artista, album, _ in pendientes:
        ms = db_sin_catalogo.execute(
            """SELECT SUM(COALESCE(ms_played,0)) FROM plays
                WHERE artist_name = ? AND album_name = ? AND ms_played >= 30000""",
            (artista, album),
        ).fetchone()[0] or 0
        horas.append(ms)
    assert horas == sorted(horas, reverse=True), "deben ir de más a menos escuchado"


def test_pending_albums_descarta_lo_que_solo_saltaste(db_sin_catalogo):
    """Un disco donde ninguna reproducción llegó a 30s no merece una petición.

    No es heurística: el scoring nunca lo contaría como escuchado.
    """
    enrich.build_album_groups(db_sin_catalogo)
    nombres = {album for _, album, _ in enrich.pending_albums(db_sin_catalogo)}
    assert "Skipped Album" not in nombres
