"""Interfaz de línea de comandos.

Flujo típico:

    spot-albums auth --client-id <ID>     # una sola vez
    spot-albums pull                      # snapshot de la API (hoy mismo)
    spot-albums ingest ~/Downloads/my_spotify_data.zip   # cuando llegue el export
    spot-albums enrich                    # resuelve álbumes y total_tracks
    spot-albums report                    # HTML + wantlist CSV/MD
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

from . import db
from .config import REDIRECT_URI, Config, save_client_id


def _conn(cfg: Config):
    return db.connect(cfg.db_path)


def enrich_budget() -> int:
    from .enrich import DEFAULT_ALBUM_BUDGET

    return DEFAULT_ALBUM_BUDGET


def cmd_auth(args, cfg: Config) -> int:
    from .spotify import auth

    if args.client_id:
        save_client_id(args.client_id)
        cfg.client_id = args.client_id
    client_id = cfg.require_client_id()
    token = auth.login(client_id)
    from .spotify.client import Client

    with Client(token) as client:
        me = client.me()
    print(f"\nSesión iniciada como {me.get('display_name') or me.get('id')}.")
    print("Siguiente paso:  spot-albums pull")
    return 0


def cmd_pull(args, cfg: Config) -> int:
    from .ingest import api
    from .spotify.client import Client

    conn = _conn(cfg)
    with Client() as client:
        print("Descargando snapshot de tu cuenta...")
        stats = api.pull(conn, client)
    for key, value in stats.items():
        print(f"  {key:20s} {value}")
    print("\nSiguiente paso:  spot-albums enrich")
    return 0


def cmd_ingest(args, cfg: Config) -> int:
    from .ingest import gdpr

    source = Path(args.path).expanduser()
    if not source.exists():
        print(f"No existe: {source}", file=sys.stderr)
        return 1

    conn = _conn(cfg)
    print(f"Leyendo {source.name}...")
    stats = gdpr.ingest(conn, source)
    print(f"  reproducciones leídas   {stats['leidas']:,}")
    print(f"  nuevas                  {stats['nuevas']:,}")
    print(f"  ya estaban              {stats['duplicadas']:,}")
    if stats["desde"]:
        print(f"  rango                   {stats['desde'][:10]} → {stats['hasta'][:10]}")

    pending = db.counts(conn)["tracks_pending"]
    print(f"\nQuedan {pending:,} tracks por resolver.")
    print("Siguiente paso:  spot-albums enrich")
    return 0


def cmd_enrich(args, cfg: Config) -> int:
    from . import enrich
    from .spotify.client import Client

    conn = _conn(cfg)
    total_groups = enrich.build_album_groups(conn)
    desde_cache = enrich.link_from_cache(conn)
    if desde_cache:
        print(f"{desde_cache:,} álbumes enlazados desde la caché, sin gastar red.")
    pending = enrich.pending_albums(conn, budget=args.budget)

    if not pending:
        print(f"Nada que resolver: los {total_groups:,} álbumes del presupuesto "
              f"ya están al día.")
        return 0

    print(f"{total_groups:,} álbumes distintos en tu historial.")
    # ~0.27 s de latencia por petición, más la pausa de auto-regulación.
    seg = len(pending) * max(args.pace, 0.27)
    print(f"Resolviendo los {len(pending):,} más escuchados que faltan "
          f"(≈{seg/60:.0f} min a {args.pace}s por petición).")
    print("Se puede interrumpir con Ctrl-C: lo hecho queda guardado.\n")

    with Client(min_interval_s=args.pace) as client:
        try:
            stats = enrich.run(conn, client, budget=args.budget)
        except KeyboardInterrupt:
            conn.commit()
            print("\nInterrumpido. Vuelve a correr `enrich` para continuar.")
            return 130

    for key, value in stats.items():
        shown = f"{value:,}" if isinstance(value, int) else value
        print(f"  {key:20s} {shown}")

    if stats.get("cortado_por_cuota"):
        h = stats.get("retry_after_h", 0)
        cuando = f"{h*60:.0f} min" if h < 1 else f"{h:.1f} h"
        resueltos = conn.execute(
            "SELECT COUNT(*) FROM album_groups WHERE album_id IS NOT NULL"
        ).fetchone()[0]
        print(f"\nSe agotó el presupuesto de peticiones. Una app en modo "
              f"desarrollo tiene ~600 al día, contadas por petición y no por "
              f"velocidad — subir --pace no ayuda.")
        print(f"Llevas {resueltos:,} álbumes resueltos, y se guardan. Vuelve a "
              f"correr `enrich` en ~{cuando} para continuar donde quedó.")
        print("No lo dejes reintentando en bucle: cada sondeo gasta del mismo "
              "presupuesto y puede renovar el bloqueo.")
        print("El reporte ya funciona con lo resuelto hasta ahora.")
        return 0

    print("\nSiguiente paso:  spot-albums report")
    return 0


def cmd_devices(args, cfg: Config) -> int:
    from .devices import DEVICES

    print(f"Perfil activo: {cfg.device.name}\n")
    for key, dev in sorted(DEVICES.items()):
        marca = "→" if dev.name == cfg.device.name else " "
        print(f" {marca} {key:22s} {dev.internal_gb:>5.0f} GB internos + "
              f"{dev.card_gb:>6.0f} GB tarjeta")
        if dev.notes:
            print(f"      {dev.notes}")
    print("\nPara cambiarlo:  SPOT_ALBUMS_DEVICE=<perfil> spot-albums report")
    print(f"o añade  {{\"device\": \"<perfil>\"}}  a ~/.config/spot-albums/config.json")
    return 0


def cmd_status(args, cfg: Config) -> int:
    from .analyze.insights import evidence_mode

    conn = _conn(cfg)
    counts = db.counts(conn)
    print(f"Base de datos: {cfg.db_path}")
    print(f"Modo de evidencia: {evidence_mode(conn)}\n")
    for key, value in counts.items():
        print(f"  {key:20s} {value:,}")
    return 0


def _load_albums(cfg: Config, args):
    from .analyze.scoring import score_albums

    conn = _conn(cfg)
    albums = score_albums(
        conn,
        include_singles=args.include_singles,
        min_hours=args.min_hours,
    )
    return conn, albums


def cmd_analyze(args, cfg: Config) -> int:
    conn, albums = _load_albums(cfg, args)
    if not albums:
        print("No hay álbumes rankeables todavía. ¿Corriste `pull`/`ingest` y `enrich`?")
        return 1

    from .analyze import insights

    summary = insights.summary(conn, albums)
    print(f"Modo: {summary['modo']} · {summary['albumes_rankeados']:,} álbumes · "
          f"{summary['horas_totales']:,.0f} h\n")
    print(f"{'#':>3}  {'BREADTH':>7}  {'HORAS':>6}  {'SCORE':>5}  ARTISTA — ÁLBUM")
    for i, a in enumerate(albums[: args.top], start=1):
        print(f"{i:>3}  {a.breadth*100:>6.0f}%  {a.hours:>6.1f}  {a.score:>5.1f}  "
              f"{a.artist_name} — {a.name}")
    return 0


def cmd_report(args, cfg: Config) -> int:
    from .report import html, wantlist

    conn, albums = _load_albums(cfg, args)
    if not albums:
        print("No hay álbumes rankeables todavía. ¿Corriste `pull`/`ingest` y `enrich`?")
        return 1

    out = cfg.out_dir
    report_path = html.build(conn, albums, out / "reporte.html", cfg.device)
    csv_path = wantlist.write_csv(albums, out / "wantlist.csv", cfg.device)
    md_path = wantlist.write_markdown(albums, out / "wantlist.md", device=cfg.device)

    print(f"  {report_path}")
    print(f"  {csv_path}")
    print(f"  {md_path}")
    if not args.no_open:
        webbrowser.open(report_path.resolve().as_uri())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spot-albums",
        description="Convierte tus stats de Spotify en una wantlist de álbumes "
                    "para tu reproductor.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("auth", help="inicia sesión en Spotify (OAuth PKCE)")
    p.add_argument("--client-id", help=f"Client ID de tu app. Redirect URI: {REDIRECT_URI}")
    p.set_defaults(func=cmd_auth)

    p = sub.add_parser("pull", help="snapshot de tops, recientes, guardados y playlists")
    p.set_defaults(func=cmd_pull)

    p = sub.add_parser("ingest", help="carga el ZIP del export GDPR")
    p.add_argument("path", help="ruta al ZIP (o a la carpeta descomprimida)")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("enrich", help="resuelve álbumes vía API")
    p.add_argument("--pace", type=float, default=0.35,
                   help="segundos mínimos entre peticiones (por defecto 0.35). "
                        "No evita el rate limit: el límite es ~600 peticiones "
                        "AL DÍA, no por segundo")
    p.add_argument("--budget", type=int, default=enrich_budget(),
                   help=f"cuántos álbumes resolver, de más a menos escuchado "
                        f"(por defecto {enrich_budget()}; la wantlist cabe en ~650)")
    p.set_defaults(func=cmd_enrich)

    p = sub.add_parser("devices", help="perfiles de reproductor disponibles")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("status", help="qué hay en la base de datos")
    p.set_defaults(func=cmd_status)

    for name, help_text, func in (
        ("analyze", "ranking por consola", cmd_analyze),
        ("report", "genera el HTML y la wantlist", cmd_report),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--top", type=int, default=40, help="cuántos mostrar")
        p.add_argument("--min-hours", type=float, default=0.0,
                       help="descarta álbumes por debajo de N horas")
        p.add_argument("--include-singles", action="store_true",
                       help="incluye singles y recopilatorios")
        if name == "report":
            p.add_argument("--no-open", action="store_true",
                           help="no abrir el navegador al terminar")
        p.set_defaults(func=func)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.load()
    try:
        return args.func(args, cfg)
    except KeyboardInterrupt:
        print("\nCancelado.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
