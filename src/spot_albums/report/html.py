"""Reporte HTML autocontenido.

Un solo fichero, sin JS, sin CDN, sin dependencias: los gráficos son SVG
generado aquí mismo. Se abre con doble clic y funciona sin red — que es lo que
quieres de algo que vas a consultar mientras compras discos.
"""

from __future__ import annotations

import html
import math
import sqlite3
from datetime import datetime
from pathlib import Path

from ..analyze import insights
from ..analyze.scoring import AlbumScore

CSS = """
:root {
  --bg:#0e0f11; --panel:#17191c; --line:#262a2f; --fg:#e8eaed; --dim:#9aa0a6;
  --accent:#1db954; --warn:#e8a33d; --bad:#e35d5d;
}
@media (prefers-color-scheme: light) {
  :root { --bg:#fbfbfa; --panel:#fff; --line:#e3e3e0; --fg:#1a1a19; --dim:#6b6b68; }
}
* { box-sizing:border-box; }
body { margin:0; padding:2rem 1.25rem 5rem; background:var(--bg); color:var(--fg);
  font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif; }
main { max-width:1080px; margin:0 auto; }
h1 { font-size:1.9rem; margin:0 0 .25rem; letter-spacing:-.02em; }
h2 { font-size:1.15rem; margin:2.75rem 0 .35rem; letter-spacing:-.01em; }
h2::before { content:""; display:block; width:28px; height:3px; background:var(--accent);
  border-radius:2px; margin-bottom:.6rem; }
p.sub { color:var(--dim); margin:.15rem 0 1rem; max-width:68ch; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:.75rem;
  margin:1.5rem 0; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:10px;
  padding:.85rem 1rem; }
.card b { display:block; font-size:1.5rem; font-weight:650; letter-spacing:-.02em; }
.card span { color:var(--dim); font-size:.78rem; text-transform:uppercase;
  letter-spacing:.05em; }
.wrap { overflow-x:auto; background:var(--panel); border:1px solid var(--line);
  border-radius:10px; }
table { border-collapse:collapse; width:100%; font-size:.875rem; }
th,td { text-align:left; padding:.5rem .75rem; border-bottom:1px solid var(--line);
  white-space:nowrap; }
th { color:var(--dim); font-weight:600; font-size:.75rem; text-transform:uppercase;
  letter-spacing:.04em; position:sticky; top:0; background:var(--panel); }
tr:last-child td { border-bottom:none; }
td.num { text-align:right; font-variant-numeric:tabular-nums; }
td.title { white-space:normal; min-width:200px; }
.bar { display:inline-block; height:8px; border-radius:4px; background:var(--accent);
  vertical-align:middle; }
.bar.low { background:var(--bad); } .bar.mid { background:var(--warn); }
.chart { background:var(--panel); border:1px solid var(--line); border-radius:10px;
  padding:1rem; overflow-x:auto; }
svg { display:block; max-width:100%; height:auto; }
.ev { font-size:.7rem; padding:.12rem .45rem; border-radius:20px; white-space:nowrap;
  border:1px solid currentColor; opacity:.85; }
.ev-reproducciones { color:var(--accent); }
.ev-tops { color:var(--warn); }
.ev-solo-guardado { color:var(--dim); }
.note { border-left:3px solid var(--warn); background:var(--panel); padding:.75rem 1rem;
  border-radius:0 8px 8px 0; color:var(--dim); margin:1rem 0; }
a { color:var(--accent); }
footer { margin-top:4rem; color:var(--dim); font-size:.8rem; border-top:1px solid var(--line);
  padding-top:1rem; }
"""


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def _bar(fraction: float, width: int = 60) -> str:
    """Barra inline proporcional, coloreada por tramo."""
    frac = max(0.0, min(fraction, 1.0))
    cls = "low" if frac < 0.3 else ("mid" if frac < 0.6 else "")
    return f'<span class="bar {cls}" style="width:{max(frac*width,2):.0f}px"></span>'


# ------------------------------------------------------------------- gráficos
def _scatter(albums: list[AlbumScore], top_labels: int = 14) -> str:
    """Breadth vs horas — el gráfico que explica toda la tesis del proyecto.

    Arriba a la derecha: muchas horas y muchos temas distintos -> el disco entero.
    Abajo a la derecha... no existe. Lo interesante es abajo a la IZQUIERDA con
    muchas horas: horas altas con breadth bajo = un single en bucle, no un álbum.
    """
    if not albums:
        return "<p class='sub'>Sin datos todavía.</p>"

    W, H = 900, 420
    pad_l, pad_r, pad_t, pad_b = 58, 20, 24, 46
    plot_w, plot_h = W - pad_l - pad_r, H - pad_t - pad_b

    max_hours = max(a.hours for a in albums) or 1.0
    log_max = math.log1p(max_hours)

    def px(breadth: float) -> float:
        return pad_l + breadth * plot_w

    def py(hours: float) -> float:
        return pad_t + plot_h - (math.log1p(hours) / log_max) * plot_h

    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" '
             f'aria-label="Dispersión de álbumes: breadth contra horas escuchadas">']

    # Zona "álbum completo": breadth >= 60%
    parts.append(
        f'<rect x="{px(0.6):.0f}" y="{pad_t}" width="{px(1.0)-px(0.6):.0f}" '
        f'height="{plot_h}" fill="#1db954" opacity="0.07"/>'
    )
    parts.append(
        f'<text x="{px(0.62):.0f}" y="{pad_t+16}" fill="#1db954" font-size="11" '
        f'font-weight="600">zona álbum completo</text>'
    )

    # Rejilla vertical (breadth) y horizontal (horas)
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        x = px(frac)
        parts.append(f'<line x1="{x:.0f}" y1="{pad_t}" x2="{x:.0f}" '
                     f'y2="{pad_t+plot_h}" stroke="#7f8792" stroke-opacity=".18"/>')
        parts.append(f'<text x="{x:.0f}" y="{H-24}" fill="#9aa0a6" font-size="11" '
                     f'text-anchor="middle">{frac*100:.0f}%</text>')

    for hours in (1, 5, 20, 100, 500):
        if hours > max_hours:
            break
        y = py(hours)
        parts.append(f'<line x1="{pad_l}" y1="{y:.0f}" x2="{pad_l+plot_w}" '
                     f'y2="{y:.0f}" stroke="#7f8792" stroke-opacity=".18"/>')
        parts.append(f'<text x="{pad_l-8}" y="{y+4:.0f}" fill="#9aa0a6" font-size="11" '
                     f'text-anchor="end">{hours}h</text>')

    for album in albums:
        x, y = px(album.breadth), py(album.hours)
        color = "#1db954" if album.breadth >= 0.6 else (
            "#e8a33d" if album.breadth >= 0.3 else "#e35d5d")
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{color}" opacity=".65">'
            f'<title>{_esc(album.artist_name)} — {_esc(album.name)}\n'
            f'{album.hours:.1f} h · breadth {album.breadth*100:.0f}% · '
            f'{album.distinct_tracks}/{album.total_tracks} temas</title></circle>'
        )

    # Etiquetas solo de los mejores, si no el gráfico se vuelve ilegible.
    for album in albums[:top_labels]:
        x, y = px(album.breadth), py(album.hours)
        anchor = "end" if x > pad_l + plot_w * 0.75 else "start"
        dx = -8 if anchor == "end" else 8
        label = album.name if len(album.name) <= 26 else album.name[:24] + "…"
        parts.append(
            f'<text x="{x+dx:.0f}" y="{y+3:.0f}" fill="#e8eaed" font-size="10.5" '
            f'text-anchor="{anchor}" opacity=".85">{_esc(label)}</text>'
        )

    parts.append(
        f'<text x="{pad_l+plot_w/2:.0f}" y="{H-6}" fill="#9aa0a6" font-size="11.5" '
        f'text-anchor="middle">breadth — % de temas del disco que has escuchado</text>'
    )
    parts.append(
        f'<text x="14" y="{pad_t+plot_h/2:.0f}" fill="#9aa0a6" font-size="11.5" '
        f'text-anchor="middle" transform="rotate(-90 14 {pad_t+plot_h/2:.0f})">'
        f'horas (escala log)</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def _bars(data: list[tuple[str, float]], unit: str = "h") -> str:
    """Barras horizontales simples para rankings cortos."""
    if not data:
        return "<p class='sub'>Sin datos.</p>"
    W = 900
    row_h = 24
    H = len(data) * row_h + 12
    label_w = 210
    max_val = max(v for _, v in data) or 1.0
    bar_w = W - label_w - 70

    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Ranking">']
    for i, (label, value) in enumerate(data):
        y = i * row_h + 6
        w = max(value / max_val * bar_w, 1)
        shown = label if len(label) <= 30 else label[:28] + "…"
        parts.append(f'<text x="{label_w-8}" y="{y+13}" fill="#e8eaed" font-size="12" '
                     f'text-anchor="end">{_esc(shown)}</text>')
        parts.append(f'<rect x="{label_w}" y="{y+3}" width="{w:.1f}" height="14" '
                     f'rx="3" fill="#1db954" opacity=".8"/>')
        parts.append(f'<text x="{label_w+w+8:.0f}" y="{y+14}" fill="#9aa0a6" '
                     f'font-size="11">{value:,.0f}{unit}</text>')
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------- tablas
NEEDS_EXPORT = (
    "<p class='sub'>Vacío por ahora: esta sección necesita tiempos reales de "
    "escucha, y la API solo expone las últimas ~50 reproducciones. Se llena "
    "sola cuando ingieras el export GDPR.</p>"
)


def _album_table(albums: list[AlbumScore], show_rank: bool = True,
                 empty: str = "<p class='sub'>Nada que mostrar.</p>") -> str:
    if not albums:
        return empty
    head = ("<tr>" + ("<th>#</th>" if show_rank else "")
            + "<th>Artista</th><th>Álbum</th><th>Año</th><th>Breadth</th>"
            "<th>Temas</th><th>Horas</th><th>Evidencia</th><th>Score</th>"
            "<th>MB</th></tr>")
    rows = []
    for i, a in enumerate(albums, start=1):
        ev = a.signals.get("evidence", "")
        rows.append(
            "<tr>"
            + (f"<td class='num'>{i}</td>" if show_rank else "")
            + f"<td class='title'>{_esc(a.artist_name)}</td>"
            f"<td class='title'>{_esc(a.name)}{' ★' if a.saved else ''}</td>"
            f"<td class='num'>{_esc(a.release_year)}</td>"
            f"<td>{_bar(a.breadth)} {a.breadth*100:.0f}%</td>"
            f"<td class='num'>{a.distinct_tracks}/{a.total_tracks}</td>"
            f"<td class='num'>{a.hours:.1f}</td>"
            f"<td><span class='ev ev-{_esc(ev)}'>{_esc(ev)}</span></td>"
            f"<td class='num'>{a.score:.1f}</td>"
            f"<td class='num'>{a.est_mb}</td></tr>"
        )
    return f"<div class='wrap'><table>{head}{''.join(rows)}</table></div>"


MODE_NOTE = {
    "solo-api": (
        "Estás en modo <b>solo-API</b>: el ranking sale de tus top 50 y las "
        "últimas ~50 reproducciones, sin tiempos reales de escucha. Sirve para "
        "empezar, pero <b>breadth y recencia son poco fiables</b> hasta que "
        "llegue el export GDPR. Pídelo en "
        "<a href='https://www.spotify.com/account/privacy/'>spotify.com/account/privacy</a>."
    ),
    "solo-export": (
        "Modo <b>solo-export</b>: tienes el historial completo pero no un "
        "snapshot de la API, así que falta la señal de confirmación (tops "
        "actuales y álbumes guardados). Corre <code>spot-albums pull</code>."
    ),
    "vacio": "No hay datos. Corre <code>spot-albums pull</code> o <code>ingest</code>.",
}


def build(conn: sqlite3.Connection, albums: list[AlbumScore], path: Path,
          device=None) -> Path:
    summary = insights.summary(conn, albums, device)
    tiers = insights.assign_tiers(albums, device)
    completos = insights.album_listeners(albums)
    singles = insights.single_listeners(albums)
    gaps = insights.discovery_gaps(albums)
    years = insights.yearly_evolution(conn)
    top_artists = insights.top_artists_by_hours(conn, top=20)

    note = MODE_NOTE.get(summary["modo"], "")
    note_html = f"<div class='note'>{note}</div>" if note else ""

    # Guardados de los que la API no expone ninguna escucha. Se agrupan por
    # artista: cuántos discos has guardado de alguien es la señal más fuerte
    # que queda cuando no hay reproducciones que medir.
    sin_evidencia = [
        a for a in albums if a.signals.get("evidence") == "solo-guardado"
    ]
    por_artista: dict[str, list] = {}
    for a in sin_evidencia:
        por_artista.setdefault(a.artist_name, []).append(a)
    ranking_artistas = sorted(
        por_artista.items(), key=lambda kv: len(kv[1]), reverse=True
    )[:20]
    depth_rows = "".join(
        f"<tr><td class='title'>{_esc(nombre)}</td>"
        f"<td class='num'>{len(items)}</td>"
        f"<td class='title'>{_esc(max(items, key=lambda x: x.score).name)}</td></tr>"
        for nombre, items in ranking_artistas
    ) or "<tr><td colspan='3'>Todos tus guardados tienen evidencia de escucha.</td></tr>"

    gap_rows = "".join(
        f"<tr><td class='title'>{_esc(g.artist_name)}</td>"
        f"<td class='num'>{g.hours:.1f}</td>"
        f"<td class='num'>{g.distinct_tracks}</td>"
        f"<td class='num'>{g.hours/max(g.distinct_tracks,1):.1f}</td>"
        f"<td class='title'>{_esc(g.best_album)}</td></tr>"
        for g in gaps
    ) or ("<tr><td colspan='5'>Necesita el export GDPR: con las ~50 "
          "reproducciones que da la API no hay horas suficientes para detectar "
          "un hueco de verdad.</td></tr>")

    year_rows = "".join(
        f"<tr><td>{_esc(y['year'])}</td><td class='num'>{y['hours']:.0f}</td>"
        f"<td class='num'>{y['artists']}</td><td class='num'>{y['tracks']}</td>"
        f"<td class='num'>{y['plays']:,}</td></tr>"
        for y in years
    ) or "<tr><td colspan='5'>Requiere el export GDPR.</td></tr>"

    doc = f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>spot-albums — wantlist para el Echo Mini</title>
<style>{CSS}</style></head><body><main>

<h1>Qué meter en el Echo Mini</h1>
<p class="sub">Ranking de álbumes construido desde tu escucha real en Spotify.
Generado el {datetime.now().strftime('%d/%m/%Y %H:%M')} · modo
<b>{_esc(summary['modo'])}</b>.</p>
{note_html}

<div class="cards">
  <div class="card"><b>{summary['albumes_rankeados']:,}</b><span>álbumes</span></div>
  <div class="card"><b>{summary['horas_totales']:,.0f}</b><span>horas</span></div>
  <div class="card"><b>{summary['tier0']}</b><span>tier 0 · 8 GB internos</span></div>
  <div class="card"><b>{summary['tier1']}</b><span>tier 1 · microSD</span></div>
  <div class="card"><b>{summary['gb_tier1']:.0f} GB</b><span>ocupa la SD</span></div>
</div>

<h2>El gráfico que decide</h2>
<p class="sub">Cada punto es un álbum. A la derecha, los discos de los que ya
conoces casi todos los temas: esos los quieres enteros. Arriba a la izquierda
está la trampa — muchísimas horas concentradas en dos o tres canciones. Bajar
ese disco completo es gastar 350 MB en música que en realidad no escuchas.</p>
<div class="chart">{_scatter(albums)}</div>

<h2>Tier 0 — memoria interna</h2>
<p class="sub">Lo que llevas encima aunque se te olvide la microSD.
{summary['gb_tier0']:.1f} GB.</p>
{_album_table(tiers['tier0'])}

<h2>Discos que escuchas enteros</h2>
<p class="sub">Breadth ≥ 60% y al menos 4 temas distintos. Máxima prioridad de
compra: aquí no hay riesgo de arrepentirse.</p>
{_album_table(completos)}

<h2>Singles disfrazados de álbum</h2>
<p class="sub">Muchas horas, muy pocos temas. Descartar cada uno de estos libera
~350 MB para un disco que sí te vas a escuchar de principio a fin.</p>
{_album_table(singles, show_rank=False, empty=NEEDS_EXPORT)}

<h2>Guardados sin evidencia de escucha</h2>
<p class="sub">{len(sin_evidencia)} álbumes que tienes guardados pero de los que
Spotify no expone ninguna reproducción por la API. <b>No significa que no los
escuches</b> — significa que aquí no se puede saber. Quedan abajo en el ranking
por eso, no porque no valgan. Es justo lo que el export GDPR va a resolver.
Los artistas donde más discos has guardado:</p>
<div class="wrap"><table>
<tr><th>Artista</th><th>Álbumes guardados</th><th>Mejor puntuado</th></tr>
{depth_rows}</table></div>

<h2>Huecos de descubrimiento</h2>
<p class="sub">Artistas a los que les has dedicado horas conociendo apenas un
puñado de canciones. Es el mejor retorno por euro invertido: ya sabes que te
gustan, solo te falta su obra.</p>
<div class="wrap"><table>
<tr><th>Artista</th><th>Horas</th><th>Temas distintos</th><th>Horas/tema</th>
<th>Mejor punto de entrada</th></tr>
{gap_rows}</table></div>

<h2>Top artistas por horas</h2>
<div class="chart">{_bars([(a['artist_name'], a['hours']) for a in top_artists])}</div>

<h2>Evolución por año</h2>
<p class="sub">Cómo ha cambiado tu escucha. El ranking pondera la recencia con
una vida media de 6 meses, así que lo de 2016 pesa poco.</p>
<div class="wrap"><table>
<tr><th>Año</th><th>Horas</th><th>Artistas</th><th>Temas</th><th>Reproducciones</th></tr>
{year_rows}</table></div>

<footer>
Generado por <code>spot-albums</code>. Los archivos de audio no salen de aquí:
esto es una lista de qué buscar, no un descargador. ★ = ya guardado en Spotify.
</footer>
</main></body></html>"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc, encoding="utf-8")
    return path
