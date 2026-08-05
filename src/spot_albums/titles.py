"""Normalización de títulos para consolidar ediciones del mismo disco.

Un mismo álbum vive en Spotify bajo varios ids —edición regional, reedición,
aniversario, remaster— y cada uno escribe los títulos a su manera. Sin
consolidar pasan dos cosas malas:

* La wantlist repite el mismo disco tres veces, y la evidencia se reparte entre
  las copias, así que puntúa más bajo de lo que merece.
* El breadth satura: `The Queen Is Dead` tiene 10 temas, pero entre ediciones
  aparecen 20 títulos distintos, así que "temas escuchados / total" se pasa de
  1.0 en discos que quizá no has escuchado enteros.

La normalización es deliberadamente conservadora: quita los sufijos de edición
conocidos y nada más. Prefiere dejar dos discos separados antes que fusionar
dos que de verdad son distintos — un falso positivo aquí borra un álbum del
ranking.
"""

from __future__ import annotations

import re
import unicodedata

# Sufijos de edición al final del título, entre paréntesis/corchetes o tras un
# guion. Solo se recortan si contienen una de estas palabras clave: así
# `Kiss Me Kiss Me Kiss Me (Remastered 2006)` se limpia, pero un título con
# paréntesis de verdad como `(What's the Story) Morning Glory?` no se toca.
_EDITION_WORDS = (
    r"remaster(?:ed)?|remasterizad[oa]|deluxe|expanded|edition|edición|edicion|"
    r"anniversary|aniversario|reissue|reedici[óo]n|bonus|version|versión|version"
    r"|mono|stereo|explicit|clean|special|super|ultimate|complete|definitive|"
    r"single version|album version|radio edit|re-?master"
)

_PAREN_EDITION = re.compile(
    rf"[\(\[][^\)\]]*(?:{_EDITION_WORDS})[^\)\]]*[\)\]]\s*$",
    re.IGNORECASE,
)
_DASH_EDITION = re.compile(
    rf"\s+[-–—]\s+[^-–—]*(?:{_EDITION_WORDS})[^-–—]*$",
    re.IGNORECASE,
)
_YEAR_SUFFIX = re.compile(r"\s*[-–—]?\s*[\(\[]?(?:19|20)\d{2}[\)\]]?\s*$")
_WS = re.compile(r"\s+")


def strip_edition(title: str) -> str:
    """Quita sufijos de edición del final, repetidamente.

    Hace falta iterar: `Album (Deluxe Edition) [Remastered]` lleva dos.
    """
    out = title.strip()
    for _ in range(4):
        before = out
        out = _PAREN_EDITION.sub("", out).strip()
        out = _DASH_EDITION.sub("", out).strip()
        if out == before:
            break
    return out or title.strip()


def normalize(title: str | None) -> str:
    """Clave canónica para comparar títulos.

    Sin acentos, sin puntuación decorativa, sin sufijos de edición, en
    minúsculas. No se usa para mostrar — solo para agrupar.
    """
    if not title:
        return ""
    out = strip_edition(title)
    # Descompone y descarta los diacríticos: "Corazón" -> "corazon".
    out = unicodedata.normalize("NFKD", out)
    out = "".join(c for c in out if not unicodedata.combining(c))
    out = out.lower()
    # Comillas tipográficas y guiones largos a su equivalente ASCII.
    out = out.translate(str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"',
                                       "–": "-", "—": "-"}))
    out = re.sub(r"[^\w\s'&-]", " ", out)
    return _WS.sub(" ", out).strip()


def album_key(artist: str | None, album: str | None) -> tuple[str, str]:
    """Clave de consolidación de un álbum: (artista, título) normalizados."""
    return (normalize(artist), normalize(album))


def track_key(title: str | None) -> str:
    """Clave de un tema, para contar cuántos distintos del disco has escuchado.

    Además del sufijo de edición se recorta un año suelto al final, que es como
    algunos remasters marcan la pista (`Bigmouth Strikes Again - 2011`).
    """
    base = normalize(title)
    return _YEAR_SUFFIX.sub("", base).strip() or base
