"""Perfiles de reproductor.

El presupuesto de almacenamiento y las manías de cada DAP cambian qué álbumes
entran y cómo hay que escribirlos. Tenerlo en un perfil —en vez de constantes
sueltas— permite usar la herramienta con cualquier reproductor sin tocar código.

Para añadir el tuyo: copia un perfil, ajústalo y ponlo en `~/.config/
spot-albums/config.json` como `{"device": "mi-perfil"}`, o define uno nuevo
aquí y manda un PR.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Device:
    """Un reproductor y lo que hay que saber para llenarlo."""

    name: str
    internal_gb: float
    card_gb: float
    # Cuánto del espacio nominal es usable. Los DAPs se ponen raros con la
    # tarjeta llena y el firmware necesita hueco para su base de datos.
    internal_usable: float = 0.85
    card_usable: float = 0.90
    formats: tuple[str, ...] = ("flac", "mp3", "m4a", "ogg", "wav")
    # Peculiaridades que condicionan cómo se prepara la biblioteca.
    supports_m3u: bool = True
    reliable_sorting: bool = True
    notes: str = ""

    @property
    def internal_mb(self) -> float:
        return self.internal_gb * 1024 * self.internal_usable

    @property
    def card_mb(self) -> float:
        return self.card_gb * 1024 * self.card_usable


DEVICES: dict[str, Device] = {
    # El que motivó el proyecto. Las peculiaridades están documentadas por
    # usuarios en el foro de FiiO, no en el manual.
    "snowsky-echo-mini": Device(
        name="Snowsky Echo Mini",
        internal_gb=8,
        card_gb=256,
        formats=("dsd", "wav", "flac", "ape", "mp3", "m4a", "ogg"),
        supports_m3u=False,
        reliable_sorting=False,
        notes=(
            "No lee M3U: una playlist = una carpeta. El orden alfabético no es "
            "fiable, así que conviene forzarlo con prefijos numéricos. Duplica "
            "artistas cuando `albumartist` es inconsistente entre ficheros."
        ),
    ),
    # Perfil genérico para cualquier reproductor con tarjeta.
    "generic-128": Device(name="DAP genérico (128 GB)", internal_gb=0, card_gb=128),
    "generic-256": Device(name="DAP genérico (256 GB)", internal_gb=0, card_gb=256),
    "generic-512": Device(name="DAP genérico (512 GB)", internal_gb=0, card_gb=512),
    # Para quien solo quiere el análisis, sin límite de espacio.
    "unlimited": Device(name="Sin límite", internal_gb=0, card_gb=100_000),
}

DEFAULT_DEVICE = "snowsky-echo-mini"


def get(name: str | None = None) -> Device:
    key = (name or DEFAULT_DEVICE).strip().lower()
    if key not in DEVICES:
        opciones = ", ".join(sorted(DEVICES))
        raise SystemExit(f"Perfil de dispositivo desconocido: {name!r}.\n"
                         f"Disponibles: {opciones}")
    return DEVICES[key]
