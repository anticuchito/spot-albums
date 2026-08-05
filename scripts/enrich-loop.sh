#!/bin/zsh
# Espera a que Spotify libere el rate limit y entonces resuelve álbumes.
#
# El límite es una ventana deslizante: tras una ráfaga grande la app queda
# penalizada un rato. Este script sondea hasta que la API responda 200 y
# entonces lanza `enrich` con ritmo conservador.
#
# Uso:   nohup ./scripts/enrich-loop.sh > enrich.log 2>&1 &
#        tail -f enrich.log
#
# Se puede parar con:  pkill -f enrich-loop

cd "$(dirname "$0")/.."
PACE="${PACE:-0.5}"

libre() {
  uv run python -c "
import httpx, sys
from spot_albums.spotify import auth
t = auth.load_token()
r = httpx.get('https://api.spotify.com/v1/tracks/4uLU6hMCjMI75M1A2tKUQC',
              headers={'Authorization': 'Bearer ' + t['access_token']}, timeout=20)
if r.status_code != 200:
    print('  limitado; Retry-After:', r.headers.get('Retry-After', '?'), 's', flush=True)
sys.exit(0 if r.status_code == 200 else 1)" 2>/dev/null
}

until libre; do
  echo "  $(date +%H:%M) esperando, reintento en 2 min"
  sleep 120
done

echo "$(date +%H:%M) cuota libre — lanzando enrich (pace ${PACE}s)"

# enrich puede volver a toparse con el límite a mitad; en ese caso sale
# limpiamente y aquí se reintenta, continuando donde quedó.
while true; do
  uv run spot-albums enrich --pace "$PACE"
  if uv run python -c "
from spot_albums import db, enrich
from spot_albums.config import Config
conn = db.connect(Config.load().db_path)
enrich.build_album_groups(conn)
raise SystemExit(0 if enrich.pending_albums(conn) else 1)" 2>/dev/null; then
    echo "$(date +%H:%M) quedan álbumes pendientes; esperando 10 min y sigo"
    sleep 600
  else
    echo "$(date +%H:%M) terminado: no quedan álbumes en el presupuesto"
    break
  fi
done

uv run spot-albums report --no-open
echo "$(date +%H:%M) reporte regenerado"
