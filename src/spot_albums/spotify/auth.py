"""OAuth Authorization Code + PKCE contra el loopback.

Spotify eliminó el implicit grant y los redirect URIs HTTP en nov-2025, con una
única excepción: el literal de loopback (`http://127.0.0.1`). `localhost` ya no
sirve. Como cliente público no hay client secret — la seguridad la da PKCE.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import urllib.parse
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

from ..config import REDIRECT_URI, SCOPES, config_dir, token_path

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"

_PAGE = """<!doctype html><meta charset="utf-8">
<title>spot-albums</title>
<body style="font-family:system-ui;background:#121212;color:#eee;
             display:grid;place-items:center;height:100vh;margin:0">
<div style="text-align:center">
  <h1 style="color:#1db954">{title}</h1>
  <p>{msg}</p>
</div></body>"""


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


class _CallbackHandler(BaseHTTPRequestHandler):
    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 (firma de la stdlib)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        _CallbackHandler.result = params
        ok = "code" in params
        body = _PAGE.format(
            title="Listo" if ok else "Error",
            msg="Ya puedes cerrar esta pestaña y volver a la terminal."
            if ok
            else f"Spotify devolvió: {params.get('error', 'desconocido')}",
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *args) -> None:  # silencia el log del servidor
        pass


def _wait_for_callback(state: str, timeout: float = 300.0) -> str:
    """Levanta un servidor efímero en 127.0.0.1:8888 y espera el redirect."""
    _CallbackHandler.result = {}
    server = HTTPServer(("127.0.0.1", 8888), _CallbackHandler)
    server.timeout = timeout
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout)
    server.server_close()

    params = _CallbackHandler.result
    if not params:
        raise SystemExit("Se agotó el tiempo esperando la autorización de Spotify.")
    if "error" in params:
        raise SystemExit(f"Spotify rechazó la autorización: {params['error']}")
    if params.get("state") != state:
        raise SystemExit("El `state` no coincide — se aborta por seguridad.")
    return params["code"]


def _persist(payload: dict) -> dict:
    """Guarda el token con expiración absoluta y permisos 600."""
    expires_in = int(payload.get("expires_in", 3600))
    payload["expires_at"] = (
        datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)
    ).isoformat()
    config_dir().mkdir(parents=True, exist_ok=True)
    path = token_path()
    path.write_text(json.dumps(payload, indent=2))
    path.chmod(0o600)
    return payload


def login(client_id: str) -> dict:
    """Flujo interactivo completo. Devuelve el token guardado."""
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "state": state,
            "scope": " ".join(SCOPES),
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
    )
    url = f"{AUTH_URL}?{query}"
    print("Abriendo el navegador para autorizar en Spotify...")
    print(f"Si no se abre solo, entra a:\n  {url}\n")
    webbrowser.open(url)

    code = _wait_for_callback(state)
    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise SystemExit(f"Falló el intercambio del código: {resp.status_code} {resp.text}")
    token = resp.json()
    token["client_id"] = client_id
    return _persist(token)


def refresh(token: dict) -> dict:
    client_id = token["client_id"]
    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": token["refresh_token"],
            "client_id": client_id,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise SystemExit(
            f"No se pudo refrescar el token ({resp.status_code}). "
            "Vuelve a correr `spot-albums auth`."
        )
    new = resp.json()
    # Spotify no siempre devuelve un refresh_token nuevo; conserva el anterior.
    new.setdefault("refresh_token", token["refresh_token"])
    new["client_id"] = client_id
    return _persist(new)


def load_token() -> dict:
    """Token válido listo para usar, refrescando si hace falta."""
    path = token_path()
    if not path.exists():
        raise SystemExit("No hay sesión. Corre primero:  spot-albums auth")
    token = json.loads(path.read_text())
    if datetime.fromisoformat(token["expires_at"]) <= datetime.now(timezone.utc):
        token = refresh(token)
    return token
