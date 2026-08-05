"""Cliente de la Web API de Spotify.

Endpoints muertos desde nov-2024 (no los uses, devuelven 403 permanente):
    audio-features · audio-analysis · recommendations · related-artists
Endpoints retirados en feb-2026:
    /me/following · los `Get Several *` (varios ids en una llamada)

Por eso `enrich` pide track por track: `GET /tracks?ids=` ya no existe.
"""

from __future__ import annotations

import time
from typing import Any, Iterator

import httpx

from . import auth

API = "https://api.spotify.com/v1"


class SpotifyError(RuntimeError):
    pass


class RateLimited(SpotifyError):
    """Cuota agotada: la espera que pide Spotify es de horas, no de segundos."""

    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class Client:
    # Por encima de esto no se espera: se aborta con RateLimited.
    max_backoff_s = 120

    # Pausa mínima entre peticiones.
    #
    # OJO: esto NO evita el rate limit. Medido, el límite de una app en modo
    # desarrollo es un presupuesto de ~600 peticiones AL DÍA, no una tasa por
    # segundo: 599 peticiones a 0.5s de separación se comieron el mismo
    # bloqueo de 24 h que una ráfaga sin pausas. Lo único que reduce el
    # consumo es pedir menos (ver enrich.py).
    #
    # Se conserva por cortesía con la API y por si el límite cambia.
    min_interval_s = 0.0

    def __init__(self, token: dict | None = None,
                 min_interval_s: float | None = None) -> None:
        self._token = token or auth.load_token()
        self._http = httpx.Client(timeout=30)
        self._last_request = 0.0
        if min_interval_s is not None:
            self.min_interval_s = min_interval_s

    def _throttle(self) -> None:
        if self.min_interval_s <= 0:
            return
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.min_interval_s:
            time.sleep(self.min_interval_s - elapsed)
        self._last_request = time.monotonic()

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token['access_token']}"}

    def get(self, path: str, **params: Any) -> dict | None:
        """GET con reintento en 429 y refresh transparente en 401.

        Devuelve None en 404 — un track puede haber salido del catálogo, y eso
        no debe tumbar una corrida de miles de ids.
        """
        url = path if path.startswith("http") else f"{API}{path}"
        for attempt in range(6):
            self._throttle()
            resp = self._http.get(url, headers=self._headers(), params=params or None)

            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", "2")) + 1
                # NUNCA dormir a ciegas. Cuando se agota la cuota diaria de una
                # app en modo desarrollo, Spotify devuelve Retry-After de hasta
                # ~24 h. Dormirlo deja el proceso colgado un día entero sin que
                # nadie se entere; mejor abortar y decirlo.
                if wait > self.max_backoff_s:
                    raise RateLimited(
                        f"Spotify limitó la app durante {wait/3600:.1f} h "
                        f"(Retry-After: {wait}s). Se agotó la cuota. "
                        f"Lo resuelto hasta ahora está guardado.",
                        retry_after=wait,
                    )
                print(f"  rate limit — esperando {wait}s", flush=True)
                time.sleep(wait)
                continue

            if resp.status_code == 401:
                self._token = auth.refresh(self._token)
                continue

            if resp.status_code == 404:
                return None

            if resp.status_code == 403:
                raise SpotifyError(
                    f"403 en {url}. Si es audio-features/recommendations/"
                    f"related-artists, están deprecados sin reemplazo desde "
                    f"nov-2024. Si no, revisa los scopes del token."
                )

            if resp.status_code >= 500:
                time.sleep(2**attempt)
                continue

            if resp.status_code != 200:
                raise SpotifyError(f"{resp.status_code} en {url}: {resp.text[:300]}")

            return resp.json()

        raise SpotifyError(f"Se agotaron los reintentos en {url}")

    def paginate(self, path: str, limit: int = 50, cap: int | None = None,
                 **params: Any) -> Iterator[dict]:
        """Itera un endpoint paginado siguiendo `next`."""
        page = self.get(path, limit=limit, **params)
        seen = 0
        while page:
            items = page.get("items", [])
            for item in items:
                yield item
                seen += 1
                if cap and seen >= cap:
                    return
            nxt = page.get("next")
            if not nxt:
                return
            page = self.get(nxt)

    # ---------------------------------------------------------------- catálogo
    def track(self, track_id: str) -> dict | None:
        return self.get(f"/tracks/{track_id}")

    def album(self, album_id: str) -> dict | None:
        return self.get(f"/albums/{album_id}")

    def artist(self, artist_id: str) -> dict | None:
        return self.get(f"/artists/{artist_id}")

    def artist_albums(self, artist_id: str, cap: int = 50) -> list[dict]:
        return list(
            self.paginate(
                f"/artists/{artist_id}/albums",
                include_groups="album",
                cap=cap,
            )
        )

    # ------------------------------------------------------------------ perfil
    def me(self) -> dict:
        data = self.get("/me")
        if data is None:
            raise SpotifyError("No se pudo leer /me")
        return data

    def top(self, kind: str, time_range: str, cap: int = 50) -> list[dict]:
        """kind: 'artists' | 'tracks'. time_range: short_term|medium_term|long_term."""
        return list(self.paginate(f"/me/top/{kind}", time_range=time_range, cap=cap))

    def recently_played(self, cap: int = 50) -> list[dict]:
        # Este endpoint pagina con `before`/`after`, no con offset, y Spotify
        # solo guarda las últimas ~50. Una página es todo lo que hay.
        page = self.get("/me/player/recently-played", limit=min(cap, 50))
        return page.get("items", []) if page else []

    def saved_albums(self) -> Iterator[dict]:
        # GET sigue vivo; solo los PUT/DELETE se movieron a /me/library en feb-2026.
        yield from self.paginate("/me/albums", limit=50)

    def my_playlists(self) -> Iterator[dict]:
        yield from self.paginate("/me/playlists", limit=50)

    def playlist_items(self, playlist_id: str, cap: int = 1000) -> Iterator[dict]:
        # feb-2026: /playlists/{id}/tracks pasó a /playlists/{id}/items
        yield from self.paginate(f"/playlists/{playlist_id}/items", limit=50, cap=cap)
