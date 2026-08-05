"""Cliente HTTP: auto-regulación y manejo del rate limit.

El límite de Spotify es una ventana deslizante, no una cuota que se reinicie a
diario. Tras una ráfaga grande la app queda penalizada, y entonces hasta dos
peticiones seguidas devuelven 429 con esperas de decenas de minutos. Estos
tests fijan las dos defensas: ir despacio a propósito, y abortar en vez de
dormir cuando la espera es larga.
"""

from __future__ import annotations

import time

import httpx
import pytest

from spot_albums.spotify.client import Client, RateLimited, SpotifyError

TOKEN = {"access_token": "x", "refresh_token": "y", "client_id": "z",
         "expires_at": "2099-01-01T00:00:00+00:00"}


def _client(handler, **kw) -> Client:
    c = Client(token=dict(TOKEN), **kw)
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    return c


def test_espera_entre_peticiones_cuando_se_le_pide_ritmo():
    c = _client(lambda r: httpx.Response(200, json={"ok": True}), min_interval_s=0.1)
    t0 = time.monotonic()
    for _ in range(4):
        c.get("/tracks/x")
    # Tres pausas entre cuatro peticiones.
    assert time.monotonic() - t0 >= 0.28


def test_sin_ritmo_no_introduce_pausas():
    c = _client(lambda r: httpx.Response(200, json={"ok": True}))
    t0 = time.monotonic()
    for _ in range(5):
        c.get("/tracks/x")
    assert time.monotonic() - t0 < 0.1


def test_una_espera_larga_aborta_en_vez_de_dormir():
    """El fallo original: time.sleep(82661) colgó el proceso 23 horas."""
    c = _client(lambda r: httpx.Response(429, headers={"Retry-After": "82661"}))
    t0 = time.monotonic()
    with pytest.raises(RateLimited) as exc:
        c.get("/tracks/x")
    assert exc.value.retry_after == 82_662
    assert time.monotonic() - t0 < 1.0, "no debe haber dormido"


def test_una_espera_corta_si_se_reintenta():
    intentos = []

    def handler(request):
        intentos.append(1)
        if len(intentos) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True})

    assert _client(handler).get("/tracks/x") == {"ok": True}
    assert len(intentos) == 2


def test_un_404_devuelve_none_y_no_revienta():
    """Un track retirado del catálogo no puede tumbar una corrida de miles."""
    assert _client(lambda r: httpx.Response(404)).get("/tracks/x") is None


def test_el_403_explica_los_endpoints_muertos():
    with pytest.raises(SpotifyError, match="deprecados"):
        _client(lambda r: httpx.Response(403)).get("/audio-features/x")
