# spot-albums

*[Read this in English](README.md) · [Hallazgos técnicos](FINDINGS.md) · [Decisiones de diseño](DESIGN.md)*

Convierte tu escucha real de Spotify en una **wantlist de álbumes priorizada**
para llenar un reproductor de audio — en este caso un Snowsky Echo Mini
(8 GB internos + microSD hasta 256 GB).

El problema no es copiar archivos: es decidir **qué discos merecen uno de los
~650 huecos** que caben en 256 GB de FLAC. Puedes tener 400 horas de un artista
y no querer ninguno de sus álbumes completos, porque siempre escuchas los mismos
dos singles. Esto separa una cosa de la otra.

> Esto **no descarga música**. Produce una lista de qué buscar y por qué.
> Los archivos los consigues tú.

---

## Puesta en marcha

### 1. Pide tu export de Spotify (hazlo ya, tarda días)

En <https://www.spotify.com/account/privacy/>: destilda "Account data", tilda
**"Extended streaming history"** y pulsa "Request data". Llega por email entre
unas horas y 30 días.

Es el dato bueno: tu historial completo con milisegundos escuchados por
reproducción. Mientras llega puedes trabajar con la API, pero el ranking no
vale gran cosa hasta que lo tengas.

### 2. Crea una app en Spotify

En <https://developer.spotify.com/dashboard>, crea una app y añade este
Redirect URI **exacto**:

```
http://127.0.0.1:8888/callback
```

Tiene que ser la IP literal. Desde nov-2025 Spotify solo acepta HTTPS excepto
en loopback, y `localhost` ya no cuenta como tal.

### 3. Instala y autentica

```bash
uv sync
uv run spot-albums auth --client-id <TU_CLIENT_ID>
```

---

## Uso

```bash
# Snapshot de tu cuenta ahora mismo (tops, recientes, guardados, playlists)
uv run spot-albums pull

# Cuando llegue el ZIP del export
uv run spot-albums ingest ~/Downloads/my_spotify_data.zip

# Resuelve álbumes (una llamada por álbum, no por track; ver más abajo)
uv run spot-albums enrich

# Ranking por consola
uv run spot-albums analyze --top 50

# Reporte HTML + wantlist.csv + wantlist.md en ./out
uv run spot-albums report

# Qué hay en la base
uv run spot-albums status
```

Opciones útiles en `analyze`/`report`:

| Opción | Efecto |
|---|---|
| `--min-hours 2` | descarta álbumes con menos de 2 horas acumuladas |
| `--include-singles` | incluye singles y recopilatorios (excluidos por defecto) |
| `--top N` | cuántas filas mostrar por consola |

---

## Cómo se calcula el ranking

Cinco señales, cada una normalizada a 0..1 y combinada con estos pesos:

| Señal | Peso | Qué mide |
|---|---|---|
| **volumen** | 33% | horas acumuladas, log-escaladas |
| **breadth** | 30% | temas distintos escuchados ÷ total del disco |
| **recencia** | 17% | decaimiento exponencial, vida media 6 meses |
| **intención** | 10% | no-skip + escucha secuencial sin shuffle |
| **confirmación** | 10% | aparece en tus tops actuales o lo tienes guardado |

**Breadth es la señal que justifica todo el proyecto.** Dos discos con las
mismas horas acumuladas pueden ser cosas opuestas: uno que te escuchas entero
y uno del que solo conoces el single que salía en una playlist. Solo el primero
merece 350 MB en la tarjeta.

La recencia está en 17% a propósito. Más alta hacía que un disco recién
descubierto de 4 horas le ganara a uno de 82: con el breadth saturado al 100%
en ambos, no queda nada que los separe. Un DAP se llena para meses.

### Por qué `enrich` resuelve álbumes y no tracks

La versión ingenua pedía `GET /tracks/{id}` por cada track escuchado para
descubrir su álbum. Con 12 años de historial son ~57.000 peticiones —Spotify
retiró las llamadas en lote en feb-2026— y eso **agota la cuota diaria de una
app en modo desarrollo**: devuelve `Retry-After` de ~23 horas.

Pero era la pregunta equivocada. El export ya trae el nombre del álbum en cada
reproducción, así que agrupar por (artista, álbum), sumar horas y contar temas
distintos no necesita red. Lo único que aporta la API es `total_tracks`, y eso
es **un dato por álbum**. Se resuelve un track representativo de cada uno —
exacto, porque el export trae el `spotify_track_uri`.

De ~57.000 peticiones a ~1.000. Además `link_from_cache()` enlaza gratis los
álbumes que `pull` ya había traído.

### Consolidación de ediciones

Un mismo disco vive en Spotify bajo varios ids (edición regional, reedición,
aniversario). Sin consolidar, `Cigarettes After Sex — Cry` aparecía tres veces
con las horas repartidas (36 + 42 + 5) en vez de una con 82.

`titles.py` normaliza artista y título —quitando sufijos de edición conocidos—
y agrupa por esa clave. También normaliza los nombres de tema: entre ediciones,
`Bigmouth Strikes Again` y `Bigmouth Strikes Again - 2011 Remaster` son el
mismo, y contarlos aparte disparaba el breadth por encima de 1.0 justo en los
discos más reeditados.

La normalización es conservadora a propósito: prefiere dejar dos discos
separados antes que fusionar dos distintos. Un falso positivo borra un álbum
del ranking.

### Decisiones sobre calidad del dato

- **El campo `skipped` del export se ignora.** Spotify no registró skips entre
  2015-04-13 y 2022-10-16, así que en años de historial el campo miente. Los
  skips se derivan de `ms_played < 30 s`.
- **`reason_start == "trackdone"` cuenta como señal positiva.** Significa que
  el tema anterior terminó y este siguió solo — es exactamente el
  comportamiento de quien deja correr un disco entero.
- **Los podcasts se descartan**, igual que las reproducciones sin
  `spotify_track_uri` (sin id, el matching por nombre es un pozo con remasters
  y reediciones).
- **Nada de `audio_features`.** Spotify mató ese endpoint —junto a
  `recommendations`, `related-artists` y `audio-analysis`— en nov-2024, sin
  reemplazo y sin lista de espera. Todo el ranking sale de comportamiento
  observado, que para este fin es mejor dato de todos modos.

---

## El reporte

`out/reporte.html` es un fichero único sin JS ni CDN — se abre con doble clic
y funciona sin red, que es lo que quieres de algo que vas a consultar mientras
compras discos. Contiene:

- **El gráfico que decide** — dispersión breadth vs horas. A la derecha los
  discos que quieres enteros; arriba a la izquierda la trampa: muchas horas
  concentradas en dos canciones.
- **Tier 0 / Tier 1** — el reparto entre los 8 GB internos y la microSD,
  con el presupuesto ya calculado.
- **Singles disfrazados de álbum** — dónde ahorrar espacio.
- **Huecos de descubrimiento** — artistas con muchas horas y pocos temas
  distintos: ya sabes que te gustan, solo te falta su obra.
- **Evolución por año** y top de artistas.

`out/wantlist.csv` lleva además links de búsqueda a Bandcamp, Qobuz y Discogs
por álbum.

---

## Se resuelve por tandas, en días distintos

Una app en modo desarrollo tiene ~600 peticiones **al día** (medido, no
documentado), contadas por petición y no por velocidad. Por eso `enrich` hace
500 álbumes por corrida y para.

No es problema: se resuelven de más a menos escuchado, así que cada tanda añade
los siguientes más importantes, y una wantlist para 256 GB cabe en ~650 discos.
Dos o tres tandas bastan.

```bash
uv run spot-albums quota     # ¿puedo correr otra?
uv run spot-albums enrich    # siguientes 500
```

No sondees en bucle mientras estés bloqueado: cada intento gasta del mismo
presupuesto. Para más margen, usa **Request Extension** en el dashboard.

## Perfiles de dispositivo

El presupuesto de almacenamiento sale de un perfil, así que sirve con cualquier
reproductor:

```bash
uv run spot-albums devices                  # listarlos
SPOT_ALBUMS_DEVICE=generic-512 uv run spot-albums report
```

Incluidos: `snowsky-echo-mini`, `generic-128/256/512`, `unlimited`.

## Licencia

MIT — ver [LICENSE](LICENSE).

## Estado

Fases 1 y 2 (ingesta, scoring, reporte, wantlist) están completas y con tests.

La fase 3 —`prep` y `sync` a la microSD— está diseñada pero no implementada;
tiene sentido construirla cuando ya tengas los primeros álbumes en disco. Va a
tener que lidiar con las manías documentadas del Echo Mini: no soporta M3U, el
orden alfabético no es fiable y duplica artistas cuando los tags son
inconsistentes. El plan es forzar el orden con prefijos numéricos según tu
ranking de escucha, normalizar `albumartist` a un único valor canónico y usar
nombres ASCII-safe.

## Tests

```bash
uv run pytest
```

El fixture es un export GDPR sintético con tres perfiles deliberadamente
distintos —disco escuchado entero, single en bucle, favorito viejo— con las
mismas horas acumuladas. Los tests verifican que el ranking los ordena bien,
que el dedupe es idempotente y que los skips se detectan pese al bug de
`skipped`.
