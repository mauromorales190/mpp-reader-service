# Guía de despliegue — MPP Reader MCP + workflow en n8n con Google Drive

Esta guía te lleva desde cero hasta tener un agente de IA en n8n que:
1. Descarga un `.mpp` de Google Drive.
2. Responde preguntas libres sobre tareas, recursos, avance, valor ganado, ruta crítica.
3. Genera un dashboard HTML ejecutivo cuando se lo pidan.
4. Puede construir un cronograma nuevo (XML) a partir de una descripción.

El trabajo pesado lo hacen dos servicios que empaquetamos como Docker: la **REST API** (para automatizaciones deterministas) y el **servidor MCP** (para que agentes de IA descubran las herramientas por descripción y decidan cuál llamar). Para el flujo con n8n AI Agent usarás el MCP; los workflows sin LLM usan la API.

---

## Arquitectura

```
┌───────────────┐      1. chat / webhook / schedule      ┌─────────────────────┐
│   Usuario     │ ─────────────────────────────────────► │        n8n          │
└───────────────┘                                        │  (Chat Trigger +    │
                                                         │   AI Agent node)    │
                                                         └──────┬──────────────┘
                                                                │ 2. Download .mpp
                                                                ▼
                                                         ┌─────────────────────┐
                                                         │   Google Drive      │
                                                         └──────┬──────────────┘
                                                                │ 3. Binary (base64)
                                                                ▼
                                                         ┌─────────────────────┐
                                                         │   AI Agent (LLM)    │
                                                         │   + MCP Client Tool │
                                                         └──────┬──────────────┘
                                                                │ 4. SSE tool calls
                                                                ▼
                     ┌──────────────────────────────────────────────────────────┐
                     │        mpp-reader-service (Docker, tu servidor)          │
                     │                                                          │
                     │  ┌────────────────┐        ┌────────────────────────┐    │
                     │  │  REST API      │        │    MCP server (SSE)    │    │
                     │  │  :8080/*       │◄──────►│    :8765/sse           │    │
                     │  └───────┬────────┘        └───────────┬────────────┘    │
                     │          │                             │                 │
                     │          ▼                             ▼                 │
                     │  ┌──────────────────────────────────────────────────┐    │
                     │  │  skill_scripts/ (shared)                         │    │
                     │  │  extract_project.py  query_project.py            │    │
                     │  │  build_project.py    build_dashboard.py          │    │
                     │  │  (backed by MPXJ via JPype + Java 11)            │    │
                     │  └──────────────────────────────────────────────────┘    │
                     └──────────────────────────────────────────────────────────┘
```

---

## Paso 1 · Requisitos previos

En el host donde correrán los servicios:

| Requisito | Por qué | Cómo verificar |
|---|---|---|
| Docker 24+ | Corre los dos contenedores | `docker --version` |
| Docker Compose v2 | Orquesta los dos | `docker compose version` |
| Puerto 8080 libre | REST API | `ss -tln \| grep 8080` |
| Puerto 8765 libre | MCP SSE | `ss -tln \| grep 8765` |
| ~600 MB RAM | JVM por contenedor (~250 MB c/u) | `free -h` |
| Conectividad saliente | Descarga imágenes base + CDN Chart.js | `curl -sI https://hub.docker.com` |

Si vas a **exponer a internet** para que n8n (en otra máquina, ej. n8n.cloud) llegue al MCP, necesitás además un reverse proxy con TLS. Te muestro dos opciones al final.

En la máquina donde está **n8n**:
- n8n 1.50+ (preferiblemente 1.62+ con soporte nativo de MCP). Si no lo tenés nativo, instalá el paquete de comunidad `n8n-nodes-mcp`.
- Credenciales: Google Drive OAuth + una API key de Claude/OpenAI para el LLM.

---

## Paso 2 · Descargar el paquete y desplegar el backend

```bash
# 1. Descargar y descomprimir
unzip mpp-reader-service.zip
cd mpp-reader-service

# 2. Revisar estructura
tree -L 2
# .
# ├── README.md
# ├── DEPLOY-GUIDE.md           ← este archivo
# ├── api/         ← FastAPI + Dockerfile
# ├── mcp/         ← MCP server + Dockerfile
# ├── skill_scripts/  ← los 4 scripts de MPXJ
# ├── n8n/         ← workflows JSON + agent prompts
# └── docker-compose.yml

# 3. Definir un API key (opcional pero recomendado para la REST)
export MPP_API_KEY="$(openssl rand -hex 24)"
echo "MPP_API_KEY=$MPP_API_KEY" > .env

# 4. Levantar
docker compose up -d --build

# 5. Ver que ambos estén healthy
docker compose ps
# mpp-reader-api   running  0.0.0.0:8080->8080/tcp
# mpp-reader-mcp   running  0.0.0.0:8765->8765/tcp
```

### Verificación rápida

```bash
# REST API — si pusiste API key, agregala como -H "X-API-Key: $MPP_API_KEY"
curl -s http://localhost:8080/health | jq

# Debe devolver:
# {
#   "service": "mpp-reader",
#   "java": "ok",
#   "mpxj": "ok",
#   "extract_present": true,
#   ...
# }

# Swagger interactivo
open http://localhost:8080/docs
```

Si `java` o `mpxj` salen `missing`, revisá los logs (`docker compose logs api`). Lo más común es memoria insuficiente para la JVM; aumentá el RAM del host a ≥1 GB.

### Prueba end-to-end del API

```bash
# Análisis
curl -F "file=@/ruta/a/tu-cronograma.mpp" \
     -H "X-API-Key: $MPP_API_KEY" \
     http://localhost:8080/query/status | jq -r .output_text

# Dashboard
curl -F "file=@/ruta/a/tu-cronograma.mpp" \
     -F "title=Review Abril" \
     -H "X-API-Key: $MPP_API_KEY" \
     -o dashboard.html \
     http://localhost:8080/dashboard
open dashboard.html
```

---

## Paso 3 · Exponer el MCP a n8n (si n8n corre en otra máquina)

Saltate este paso si n8n corre en el mismo host: en ese caso los contenedores se ven por nombre de servicio (`mpp-reader-mcp:8765`) dentro de la red Docker.

### Opción A · Mismo servidor, red Docker compartida

```yaml
# En tu docker-compose.yml de n8n
services:
  n8n:
    ...
    networks: [ shared ]

networks:
  shared:
    external: true  # y creala con: docker network create shared
```

Y en el compose de mpp-reader-service, la misma `shared`. n8n alcanza el MCP como `http://mpp-reader-mcp:8765/sse`.

### Opción B · n8n.cloud o n8n remoto → necesitás TLS

Usa **Caddy** como reverse proxy (zero-config HTTPS con Let's Encrypt):

```caddyfile
# /etc/caddy/Caddyfile
mpp-api.tudominio.com {
  reverse_proxy localhost:8080
}

mpp-mcp.tudominio.com {
  reverse_proxy localhost:8765
}
```

```bash
sudo caddy run --config /etc/caddy/Caddyfile
```

Apuntás tus DNS `A` records a la IP del server y en pocos segundos tenés `https://mpp-mcp.tudominio.com/sse`.

**Seguridad mínima** para producción: agregar autenticación básica al MCP (Caddy maneja basic auth nativo), o levantar el MCP detrás de Cloudflare Tunnel con Access policy.

---

## Paso 4 · Configurar Google Drive en n8n

1. **Google Cloud Console**: crear un proyecto → "Habilitar API de Google Drive" → "Credenciales OAuth 2.0" para "Aplicación web". Autorizar `https://tu-n8n-url/rest/oauth2-credential/callback`.
2. En n8n: **Credentials → New → Google Drive OAuth2 API**. Pegá Client ID / Client Secret. Clic *Connect my account*, aceptar permisos.
3. Anotá el **ID de la credencial** (lo necesitás para pegar en el JSON del workflow).

---

## Paso 5 · Importar el workflow

1. n8n → **Workflows → Import from File** → seleccionar `n8n/workflow-drive-agent.json`.
2. Dentro del workflow, abrir cada nodo y revisar:

| Nodo | Configuración |
|---|---|
| **Drive — download .mpp** | Reemplazar la credencial por la tuya. Dejar `fileId` = `{{$vars.MPP_DRIVE_FILE_ID || $json.driveFileId}}` para que sea parametrizable desde el trigger. |
| **Claude Sonnet 4.6** | Conectar tu credencial de Anthropic (o cambiá a OpenAI/Gemini si preferís). Temperatura 0.2 para respuestas reproducibles. |
| **MPP Reader (MCP)** | `sseEndpoint`: `http://mpp-reader-mcp:8765/sse` (local) o `https://mpp-mcp.tudominio.com/sse` (remoto). Toggle "Include all tools" ON. |
| **AI Agent** | El *System Message* ya viene cargado; revisalo. Si renombrás tools, actualizá el prompt también. |

3. Click "Save" → "Activate".

### Variables de workflow

Abrí **Settings → Variables** en n8n y definí:
- `MPP_DRIVE_FILE_ID`: ID del archivo default en Drive (para pruebas).
- `MPP_MCP_URL`: URL completa del endpoint SSE.
- `MPP_API_KEY`: si usás auth en el REST.

---

## Paso 6 · Probar el workflow

Activá el Chat Trigger del workflow. n8n te da una URL pública (o podés habilitar el "Chat" integrado). Preguntas sugeridas para probar cobertura:

| Pregunta | Tool que debería disparar | Salida esperada |
|---|---|---|
| "¿Cómo va el proyecto?" | `query_project(query='status')` | Tabla con % completo, fechas, costo actual vs plan |
| "Dame la ruta crítica" | `query_project(query='critical')` | Tabla de tareas con slack=0 |
| "Necesito los indicadores de valor ganado" | `query_project(query='evm')` | BAC/BCWS/BCWP/ACWP + CPI/SPI |
| "¿Qué tareas están atrasadas?" | `query_project(query='overdue')` | Lista ordenada por días de atraso |
| "¿Quién está sobreasignado?" | `query_project(query='resources')` | Tabla de recursos con peak units |
| "Mostrame las tareas con 'diseño'" | `query_project(query='find', name='diseño')` | Matches por substring |
| "Hacé un dashboard del proyecto" | `build_dashboard(...)` | HTML self-contained como archivo adjunto |
| "¿Qué empieza la semana próxima?" | `query_project(query='upcoming', days=7)` | Tareas que inician en 7 días |
| "Diferencia con la línea base" | `query_project(query='baseline')` | Variance de fechas + costos |
| "Arma un cronograma de X semanas con Y fases" | `build_project(spec=...)` | .xml descargable |

Si el agente no usa los tools, revisá que:
- El nodo MCP Client aparezca en el "Chat log" del run (tab `AI Tools`) — si no, el endpoint SSE no está accesible.
- El System Message mencione *explícitamente* las tools disponibles (el que viene por default ya lo hace).

---

## Paso 7 · Entregar el dashboard al usuario

El workflow incluido extrae cualquier `file_b64` que el agente produzca (por ejemplo, el dashboard HTML) y lo deja disponible como binario en el output del *Extract file attachment* node. Desde ahí podés:

### Opción A · Responder en chat con un link

Agregar un nodo **HTTP Request** que suba el binario a Google Drive / S3, y un nodo **Set** que cambie la respuesta del chat por: "✅ Dashboard listo: *URL*".

### Opción B · Slack DM con el HTML adjunto

Conectá el nodo **Slack → Send message** después del extract; en *Attachments* usá `{{ $binary.data }}`.

### Opción C · Email con SendGrid / SMTP

Nodo **Send Email** → *Attachments: from previous node*.

### Opción D · Guardar en Drive y abrir

Nodo **Google Drive → Upload file** con el binario; devolvé el enlace en chat.

---

## Paso 8 · Variaciones comunes

### Disparar por agenda (status report automático)

Reemplazá *Chat Trigger* por *Schedule Trigger* (ej. lunes 7am). El `chatInput` lo construís a mano con un nodo Set: `"Genera un dashboard del proyecto"`. Agregá al final un nodo **Slack** o **Email** para distribución.

### Múltiples proyectos en una carpeta de Drive

Reemplazá *Drive Download* por **Drive → List files** filtrando `.mpp` o `.xml`. Después un nodo **Split In Batches** y para cada archivo disparás el agente. Útil para un "Portfolio Dashboard" semanal.

### Análisis comparativo vs línea base previa

Hacés dos extracciones sucesivas: la versión actual y una histórica. Pasás ambos JSON al agente con el prompt: "compará avances entre v1 y v2".

---

## Paso 9 · Troubleshooting

| Síntoma | Causa probable | Solución |
|---|---|---|
| `401 Unauthorized` en REST | Falta header `X-API-Key` | Agregalo o quitá `MPP_API_KEY` del env |
| `mpxj: missing` en `/health` | Java no inició | `docker compose logs api`; normalmente RAM insuficiente |
| `n8n MCP Client Tool: 0 tools found` | Endpoint SSE inalcanzable desde n8n | `docker exec n8n curl <URL>/sse`, revisá firewall |
| El agente no llama ninguna tool | El LLM considera que puede responder solo | Subir temperatura a 0, hacer el prompt más "pushy" |
| Dashboard sin charts | Red bloquea CDN jsdelivr | Hospedar Chart.js localmente (modificar la plantilla) |
| `/build` falla con error de fechas | Formato de fecha inválido en spec | Usar ISO 8601: `2026-05-04` o `2026-05-04T08:00` |
| Timeouts en archivos grandes | Límite de 50 MB o JVM lenta | Subir `MPP_MAX_UPLOAD_MB`; para >5k tareas considerá caché |

Logs útiles:

```bash
docker compose logs -f api            # REST
docker compose logs -f mcp            # MCP
docker compose exec api cat /tmp/*    # tempfiles (debug)
```

---

## Paso 10 · Hardening para producción

- **Auth** en el MCP: levantarlo detrás de Caddy con basic auth o Cloudflare Access.
- **Rate limits**: usar Caddy plugin *ratelimit* o un WAF.
- **Observabilidad**: agregá *OpenTelemetry* en el uvicorn (`--access-log` + lib otel-fastapi).
- **Backup**: no hay estado persistente — el servicio es stateless, así que solo backupeás la configuración Docker.
- **Actualizaciones**: MPXJ publica cada 2-3 semanas; para actualizar, `pip install --upgrade mpxj` y rebuild.
- **Costos LLM**: el System Message es ~600 tokens, cada llamada de tool ~200-1500 tokens de respuesta. En pruebas, ~3-10 llamadas por pregunta. Con Claude Sonnet 4.6 el ROI es excelente para PMOs; con Opus es 5x más caro sin mejora perceptible para este caso.
- **Aspose (opcional)** para exportar .mpp nativo: descomentar el bloque en ambos Dockerfiles, poner el archivo `aspose-license.lic` junto al Dockerfile antes de `docker compose up --build`.

---

## Referencia rápida · tools disponibles en el MCP

| Tool | Cuándo usar |
|---|---|
| `list_queries` | Cuando el agente necesita recordar qué consultas puede ejecutar |
| `extract_project(file_b64, filename)` | Cuando necesitás el bundle JSON completo para cálculos libres |
| `query_project(file_b64, filename, query, name?, days?, limit?, wbs?)` | 90 % de las preguntas puntuales del usuario |
| `build_project(spec, format='xml')` | Cuando el usuario pide generar un cronograma nuevo |
| `build_dashboard(file_b64, filename, title?)` | Cuando el usuario pide "dashboard", "informe", "reporte visual" |

---

## Anexo · Flujo de datos de una pregunta típica

Usuario escribe en el chat: **"¿Qué tareas están en riesgo y cuál es el SPI de la Fase 2?"**

1. **Chat Trigger** recibe el mensaje.
2. **Drive Download** descarga el `.mpp` con el file ID de `$vars.MPP_DRIVE_FILE_ID`.
3. **Code node** convierte a `{chatInput, filename, mppBase64}`.
4. **AI Agent** decide: "necesito dos tools" → llama:
   - `query_project(file_b64=<...>, filename=..., query='overdue')` → devuelve tabla.
   - `query_project(file_b64=..., filename=..., query='evm')` → devuelve BAC/BCWP/SPI.
5. **AI Agent** compone la respuesta combinando ambos resultados, filtra Fase 2 si el bundle lo permite.
6. **Extract file** no encuentra binario (ningún tool produjo un archivo), así que solo pasa `reply`.
7. **Reply in chat** devuelve el texto formateado al usuario.

Si en el mismo chat el usuario después dice "genera un dashboard":

1. Chat Trigger → Drive (se re-descarga) → Code.
2. AI Agent llama `build_dashboard(...)` → recibe `{filename, file_b64}`.
3. **Extract file** detecta `file_b64`, lo convierte en binario.
4. Nodo siguiente (Slack/Email/Drive upload) entrega el HTML.
5. El chat responde con un resumen: "✅ Dashboard listo (14 KB). Incluye KPIs, Gantt, EVM, recursos y top 8 tareas en riesgo."
