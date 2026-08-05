"""Rutas, credenciales y constantes de sintonía del pipeline."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING
from pathlib import Path

if TYPE_CHECKING:
    from .devices import Device

APP_NAME = "spot-albums"

# OAuth: desde nov-2025 Spotify solo acepta HTTPS *excepto* el literal de loopback.
# "localhost" ya no vale — tiene que ser la IP.
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = [
    "user-top-read",
    "user-read-recently-played",
    "user-library-read",
    "playlist-read-private",
]

# FLAC 16/44 estéreo ~ 700 MB/hora; un álbum medio de 45 min ≈ 525 MB.
# Medido contra colecciones reales, la media sale más baja por álbumes cortos.
FLAC_MB_PER_MINUTE = 8.5
DEFAULT_ALBUM_MINUTES = 42.0

# Un play por debajo de este umbral cuenta como skip.
# NO usamos el campo `skipped` del export: Spotify no registró skips entre
# 2015-04-13 y 2022-10-16, así que el campo miente en años de historial.
SKIP_THRESHOLD_MS = 30_000

# Decaimiento de recencia: half-life en días.
RECENCY_HALF_LIFE_DAYS = 180.0


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME


def token_path() -> Path:
    return config_dir() / "token.json"


def config_path() -> Path:
    return config_dir() / "config.json"


@dataclass
class Config:
    client_id: str
    db_path: Path
    out_dir: Path
    device: "Device"

    @classmethod
    def load(cls, project_root: Path | None = None) -> Config:
        from .devices import get as get_device

        root = project_root or Path.cwd()
        stored: dict = {}
        if config_path().exists():
            stored = json.loads(config_path().read_text())

        client_id = os.environ.get("SPOTIFY_CLIENT_ID") or stored.get("client_id", "")
        device_name = os.environ.get("SPOT_ALBUMS_DEVICE") or stored.get("device")

        return cls(
            client_id=client_id,
            db_path=Path(os.environ.get("SPOT_ALBUMS_DB", root / "data" / "spot-albums.db")),
            out_dir=Path(os.environ.get("SPOT_ALBUMS_OUT", root / "out")),
            device=get_device(device_name),
        )

    def require_client_id(self) -> str:
        if not self.client_id:
            raise SystemExit(
                "Falta el Client ID de Spotify.\n"
                "  1. Crea una app en https://developer.spotify.com/dashboard\n"
                f"  2. Añade este Redirect URI exacto: {REDIRECT_URI}\n"
                "  3. Guárdalo con:  spot-albums auth --client-id <TU_CLIENT_ID>\n"
                "     (o exporta SPOTIFY_CLIENT_ID)"
            )
        return self.client_id


def save_client_id(client_id: str) -> None:
    config_dir().mkdir(parents=True, exist_ok=True)
    config_path().write_text(json.dumps({"client_id": client_id}, indent=2))
    config_path().chmod(0o600)
