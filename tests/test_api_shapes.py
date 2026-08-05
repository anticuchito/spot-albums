"""Regresiones contra los cambios de forma de la Web API (feb-2026).

Estos fallos son especialmente traicioneros porque **no lanzan error**: la
respuesta llega con 200, la clave esperada no está, y el pipeline guarda cero
filas sin quejarse. Se detectó justo así — `pull` reportó 3 playlists y 0
canciones.
"""

from __future__ import annotations

from spot_albums.ingest.api import playlist_item_track

# Recorte literal de lo que devolvió GET /playlists/{id}/items el 2026-08-04.
ITEM_FEB2026 = {
    "added_at": "2026-03-05T12:31:24Z",
    "added_by": {"id": "12154345521", "type": "user"},
    "is_local": False,
    "item": {
        "id": "4uLU6hMCjMI75M1A2tKUQC",
        "name": "Never Gonna Give You Up",
        "type": "track",
        "track": True,
        "duration_ms": 213_573,
        "album": {"id": "63yRRBtLX8eqbOLD6f0y9U", "album_type": "album",
                  "total_tracks": 12},
        "artists": [{"id": "art1", "name": "Rick Astley"}],
    },
}

# Forma antigua, por si Spotify diera marcha atrás.
ITEM_LEGACY = {
    "added_at": "2024-01-01T00:00:00Z",
    "track": {"id": "legacy1", "name": "Old Shape", "type": "track"},
}


def test_lee_la_clave_item_de_feb2026():
    track = playlist_item_track(ITEM_FEB2026)
    assert track["id"] == "4uLU6hMCjMI75M1A2tKUQC"
    assert track["type"] == "track"


def test_sigue_leyendo_la_clave_track_antigua():
    assert playlist_item_track(ITEM_LEGACY)["id"] == "legacy1"


def test_item_vacio_no_revienta():
    assert playlist_item_track({}) == {}
    assert playlist_item_track({"item": None, "track": None}) == {}


def test_los_episodios_se_descartan_por_type():
    """El filtro de `pull` es `type != 'track'`; un podcast no debe colarse."""
    episode = {"added_at": "x", "item": {"id": "ep1", "type": "episode"}}
    track = playlist_item_track(episode)
    assert track.get("type") != "track"
