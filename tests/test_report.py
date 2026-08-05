"""Humo del pipeline de salida.

Existe porque un refactor pasó `device` en la posición de `limit` y rompió
`report` sin que ningún test se enterara: había 39 tests y ninguno llegaba a
escribir un fichero.
"""

from __future__ import annotations

import csv
import re

from spot_albums.analyze.scoring import score_albums
from spot_albums.devices import get as get_device
from spot_albums.report import html, wantlist


def test_genera_los_tres_ficheros(seeded_db, tmp_path):
    albums = score_albums(seeded_db)
    assert albums, "el fixture debería producir álbumes rankeables"

    r = html.build(seeded_db, albums, tmp_path / "reporte.html")
    c = wantlist.write_csv(albums, tmp_path / "wantlist.csv")
    m = wantlist.write_markdown(albums, tmp_path / "wantlist.md")

    for path in (r, c, m):
        assert path.exists() and path.stat().st_size > 0


def test_el_html_no_lleva_recursos_externos(seeded_db, tmp_path):
    """Debe abrirse sin red: nada de CDN, fuentes remotas ni imágenes."""
    albums = score_albums(seeded_db)
    doc = html.build(seeded_db, albums, tmp_path / "r.html").read_text()

    assert "<script" not in doc.lower()
    assert not re.search(r'(src|href)\s*=\s*["\']https?://', doc), \
        "hay un recurso remoto en el HTML"


def test_el_svg_sale_con_coordenadas_validas(seeded_db, tmp_path):
    albums = score_albums(seeded_db)
    doc = html.build(seeded_db, albums, tmp_path / "r.html").read_text()

    assert "NaN" not in doc and "Infinity" not in doc
    circles = re.findall(r'<circle cx="([-\d.]+)" cy="([-\d.]+)"', doc)
    assert circles, "el gráfico de dispersión no pintó ningún punto"
    for x, y in circles:
        assert 0 <= float(x) <= 900 and 0 <= float(y) <= 420


def test_el_csv_trae_las_columnas_y_los_links(seeded_db, tmp_path):
    albums = score_albums(seeded_db)
    path = wantlist.write_csv(albums, tmp_path / "w.csv")
    rows = list(csv.DictReader(path.open(encoding="utf-8")))

    assert len(rows) == len(albums)
    assert set(wantlist.COLUMNS) == set(rows[0])
    assert rows[0]["bandcamp"].startswith("https://bandcamp.com/search")
    assert rows[0]["tier"] in {"tier0", "tier1", "resto"}


def test_el_perfil_de_dispositivo_cambia_los_tiers(seeded_db, tmp_path):
    """Con 0 GB internos nada debería caer en tier0."""
    albums = score_albums(seeded_db)
    sin_interna = get_device("generic-256")

    rows = list(csv.DictReader(
        wantlist.write_csv(albums, tmp_path / "w.csv", device=sin_interna)
        .open(encoding="utf-8")
    ))
    assert all(r["tier"] != "tier0" for r in rows)


def test_write_markdown_respeta_el_limite(seeded_db, tmp_path):
    """El fallo real: `device` acabó en la posición de `limit`."""
    albums = score_albums(seeded_db)
    doc = wantlist.write_markdown(
        albums, tmp_path / "w.md", limit=1, device=get_device()
    ).read_text()
    # La fila separadora empieza con "|---", así que las que empiezan con "| "
    # son la cabecera y las de datos: una sola fila de datos.
    assert len([ln for ln in doc.splitlines() if ln.startswith("| ")]) == 2
