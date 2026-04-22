# mpp-reader-service

Servicio contenedorizado que expone la *skill* **mpp-reader** como dos transportes listos para consumir: una **API REST** (FastAPI) y un **servidor MCP** (stdio y SSE). Ambos comparten una única copia de los scripts de parseo (`skill_scripts/`) y usan el mismo extractor respaldado por MPXJ, de manera que no hay lógica duplicada entre transportes.

Con este servicio puedes, desde un workflow de n8n, un cliente LLM (Claude Desktop, Cursor, etc.) o cualquier script que hable HTTP, hacer todo lo que necesita la operación de un proyecto: analizar un `.mpp` existente, generar un cronograma nuevo desde un spec, publicar dashboards EVM/Gantt y publicar EDTs interactivas.

```
mpp-reader-service/
├── skill_scripts/          ← única fuente de verdad (extract, query, build, dashboard, wbs)
├── api/                    ← servidor REST FastAPI
│   ├── server.py
│   ├── requirements.txt
│   └── Dockerfile          → puerto 8080
├── mcp/                    ← servidor MCP (stdio para clientes desktop, SSE para n8n) + rutas HTTP
│   ├── server.py
│   ├── requirements.txt
│   └── Dockerfile          → puerto 8765
├── n8n/
│   ├── workflow-rest-http.json     ← ejemplo: HTTP Request → API REST
│   ├── workflow-mcp-agent.json     ← ejemplo: AI Agent → MCP Client → servidor MCP
│   ├── workflow-drive-agent.json   ← ejemplo: análisis de .mpp desde Drive con agente
│   └── workflow-build-from-sheet.json ← ejemplo: generar cronograma desde Google Sheets
├── docker-compose.yml      ← levanta ambos servidores juntos
└── README.md
```

## ¿Cuál transporte uso?

| Si quieres… | Usa |
|---|---|
| Encadenar pasos deterministas en n8n (formulario → consulta → mensaje Slack) | **API REST** con el nodo *HTTP Request* |
| Que un agente IA decida qué consulta correr, en lenguaje natural | **Servidor MCP** con los nodos *AI Agent → MCP Client* |
| Consumir el mismo backend desde Claude Desktop, Cursor u otros clientes LLM | **Servidor MCP** en modo stdio |
| Ambos | Levanta los dos: comparten los scripts, no hay lógica duplicada |

## Arranque rápido (Docker Compose)

```bash
cd mpp-reader-service
docker compose up --build
```

Eso levanta:

- **API REST** en `http://localhost:8080` — entra a `/docs` para ver el Swagger interactivo.
- **Servidor MCP (SSE)** en `http://localhost:8765/sse` — apunta el nodo MCP Client de n8n a esta URL.

Si tu n8n también está en Docker, conecta los dos servicios a la misma red y usa los nombres internos (`mpp-reader-api:8080`, `mpp-reader-mcp:8765`) en lugar de `localhost`.

## Arranque rápido (Python local, sin Docker)

```bash
# MPXJ requiere Java 11+
java -version

# API REST
pip install -r api/requirements.txt
uvicorn api.server:app --host 0.0.0.0 --port 8080

# Servidor MCP (en otra terminal)
pip install -r mcp/requirements.txt
python3 mcp/server.py --transport sse --host 0.0.0.0 --port 8765
```

## Superficie de la API REST

| Método | Ruta | Cuerpo / parámetros |
|---|---|---|
| GET  | `/health`            | — lista los endpoints disponibles y la versión del servicio |
| GET  | `/queries`           | — descubrir las consultas canónicas |
| POST | `/extract`           | multipart `file=@cronograma.mpp` → bundle JSON estructurado |
| POST | `/query/{name}`      | multipart `file` + parámetros opcionales (`task_name`, `wbs`, `days`, `limit`) |
| POST | `/build`             | **JSON body** con spec de cronograma (task-céntrico); `?format=xml\|mpx\|mpp`, `?download=true\|false` |
| POST | `/build-from-phases` | **JSON body** con spec fase-céntrico; expande actividades por fase, inserta hitos Start/End y valida la red de precedencias antes de generar el XML |
| POST | `/dashboard`         | multipart `file=@cronograma.mpp` → HTML interactivo con KPIs, Gantt tracking, curva S EVM y uso de recursos |
| POST | `/dashboards/publish`| publica el dashboard como HTML firmado con TTL de 30 días, devuelve `{url, id}` |
| GET  | `/dashboards/{id}`   | sirve el HTML publicado (sólo lectura) |
| POST | `/wbs/publish`       | publica la EDT interactiva (árbol D3.js con buscador, toggle horizontal/vertical y panel de diccionario). Devuelve `{url, id}` |
| GET  | `/wbs/{id}`          | sirve la EDT publicada |

Nombres de consulta disponibles en `/query/{name}`: `status, critical, network, overdue, upcoming, slack, evm, baseline, resources, customfields, calendars, find, summary-tree`.

**Autenticación:** define `MPP_API_KEY=<secreto>` en el entorno del contenedor; los clientes deben enviar el header `X-API-Key: <secreto>` en cada petición. Las rutas públicas `/dashboards/{id}` y `/wbs/{id}` no piden API key porque están pensadas para compartir el enlace con stakeholders.

### Generar un cronograma (`/build`, `/build-from-phases`)

`/build` recibe un spec task-céntrico: `tasks[]` con `outline_level`, `duration`, `predecessors[]`. Sirve cuando ya tienes definida la estructura de EDT y quieres el XML.

`/build-from-phases` recibe un spec fase-céntrico (más cercano a como piensa un PM): `phases[]` con `activities[]` encadenadas por `predecessor` dentro de la misma fase. El endpoint:

- Inserta automáticamente los hitos `Start of <Fase>` y `End of <Fase>` por cada fase.
- Crea la red de precedencias entre fases (End de la fase N → Start de la fase N+1).
- Valida que toda actividad que no sea la primera tenga predecesora y que toda actividad que no sea la última tenga sucesora.
- Detecta tareas resumen que no deberían tener relaciones FS/SS/FF/SF (regla PMBOK: los summary tasks nunca se conectan) y falla temprano con un 400 si algo está mal armado.

Formato por defecto: **XML de Microsoft Project** (formato oficial de intercambio). MS Project lo abre nativo (Archivo → Abrir → `*.xml`) y permite guardarlo como `.mpp` con un solo clic.

El formato nativo `.mpp` sólo es posible con **Aspose.Tasks** (librería comercial, requiere licencia). Si está instalada en el contenedor, `?format=mpp` devuelve `.mpp`; si no, devuelve `501` con un mensaje claro.

Spec mínimo para `/build`:

```json
{
  "project": { "title": "Demo", "start_date": "2026-05-04" },
  "tasks": [
    { "id": 1, "name": "Kickoff", "milestone": true },
    { "id": 2, "name": "Diseño", "outline_level": 1, "duration": "5d",
      "predecessors": [{ "id": 1, "type": "FS" }] }
  ]
}
```

El schema completo está documentado en el docstring de `skill_scripts/build_project.py` y también expuesto en la descripción de las herramientas MCP.

### Llamar desde `curl`

```bash
curl -F "file=@CRONOGRAMA.mpp" http://localhost:8080/extract | jq .project.counts
curl -F "file=@CRONOGRAMA.mpp" http://localhost:8080/query/critical | jq -r .output_text
curl -F "file=@CRONOGRAMA.mpp" -F "task_name=SALA" http://localhost:8080/query/find | jq -r .output_text
curl -F "file=@CRONOGRAMA.mpp" http://localhost:8080/dashboard -o dashboard.html
```

### Llamar desde n8n

Importa `n8n/workflow-rest-http.json`. El flujo lee un `.mpp` del disco, lo ramifica a dos llamadas REST (estado + EVM) y fusiona el resultado. Para alimentarlo desde una carga por webhook, reemplaza el nodo *Read Binary File* por un *Webhook* con `Binary Data → Yes`.

## Superficie del servidor MCP

Herramientas expuestas al agente:

| Herramienta | Descripción |
|---|---|
| `list_queries` | Descubrir las consultas canónicas disponibles |
| `extract_project(file_b64, filename)` | Devuelve el bundle estructurado completo para análisis personalizado |
| `query_project(file_b64, filename, query, name?, days?, limit?, wbs?)` | Ejecuta una consulta canónica |
| `build_project(spec, format="xml")` | Genera el archivo de MS Project desde un spec task-céntrico. Devuelve `{filename, format, size_bytes, file_b64}` |
| `build_project_from_phases(spec, format="xml")` | Igual que el anterior pero desde spec fase-céntrico |
| `build_dashboard(file_b64, filename)` | Genera el HTML del dashboard EVM/Gantt como base64 |
| `publish_dashboard(file_b64, filename, title?)` | Publica el dashboard y devuelve URL firmada con TTL de 30 días |
| `publish_wbs(spec, title?)` | Publica la EDT interactiva desde un spec y devuelve URL firmada |

El archivo viaja como **string en base64** para sobrevivir al transporte JSON-RPC. En n8n, la conversión de binario a base64 es un solo nodo (*Move Binary Data → Mode: Binary to JSON, Encode With: base64*) antes de la llamada al MCP Client.

### Conectarlo al AI Agent de n8n

1. Añade un nodo *AI Agent* y asócialo a un modelo de chat (Claude / OpenAI / etc.).
2. Añade el nodo *MCP Client Tool* con **SSE Endpoint** = `http://mpp-reader-mcp:8765/sse`. Activa "incluir todas las herramientas".
3. Conecta el MCP Client en el slot `ai_tool` del AI Agent.
4. Pruébalo pidiéndole: *"Aquí te paso un .mpp adjunto, dame el SPI y el camino crítico"* — el agente llamará `query_project` dos veces y compondrá la respuesta.

`n8n/workflow-mcp-agent.json` trae una plantilla de arranque.

### Conectarlo a Claude Desktop

Edita `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "mpp-reader": {
      "command": "python3",
      "args": ["/ruta/absoluta/a/mpp-reader-service/mcp/server.py"],
      "env": { "MPP_SCRIPTS_DIR": "/ruta/absoluta/a/mpp-reader-service/skill_scripts" }
    }
  }
}
```

Reinicia Claude Desktop y las herramientas aparecen bajo el menú 🔌.

## Despliegue en Railway

El servicio está pensado para correr en Railway con auto-deploy desde GitHub. Recomendaciones mínimas:

- **Plan:** 8 GB RAM / 8 vCPU como piso. MPXJ arranca una JVM por cada request y para schedules grandes (>5 000 tareas) puede requerir hasta 4 GB de heap.
- **Variables de entorno a configurar:** `MPP_API_KEY`, `MPP_PUBLIC_BASE_URL` (apuntando al dominio público de Railway, usado para firmar los enlaces de `/wbs/publish` y `/dashboards/publish`), `MPP_PUBLISHED_TTL_DAYS` (por defecto 30) y `MPP_MAX_UPLOAD_MB`.
- **Volumen persistente** montado en `/data/published` si quieres que las EDTs y dashboards publicados sobrevivan a los redeploys.
- **Networking:** si n8n corre en el mismo proyecto Railway, conéctalos por red privada y usa la URL interna (`mpp-reader-service.railway.internal:8080`) en las credenciales de n8n.

La guía paso a paso está en `DESPLIEGUE-EDT-GENERATOR.md` en la raíz del proyecto.

## Checklist antes de exponer a internet

Antes de abrir el servicio más allá de tu máquina, como mínimo: define `MPP_API_KEY`, pon TLS al frente (Caddy, Traefik, nginx o el proxy de Railway), aplica un rate-limit por IP y decide si dejas `MPP_MAX_UPLOAD_MB` en 50 o lo subes para cronogramas empresariales grandes. El servidor MCP no tiene autenticación built-in en modo SSE — termínalo en la misma red privada que n8n (Docker Compose, namespace de k8s, red privada de Railway) o pon un reverse-proxy con autenticación delante.

Para archivos grandes (>10 MB o >5 000 tareas), cada request arranca una JVM fresca vía `extract_project.py`, lo que agrega ~1 segundo de overhead. Si el throughput importa, se puede persistir la JVM inline dentro del proceso FastAPI (`mpxj.startJVM()` una sola vez al arrancar la app) — el script está preparado para que sea un refactor pequeño.

## Pruebas locales sin Docker

Los scripts de la skill se pueden ejecutar directamente:

```bash
python3 skill_scripts/extract_project.py CRONOGRAMA.mpp --out /tmp/bundle
python3 skill_scripts/query_project.py /tmp/bundle critical
python3 skill_scripts/build_dashboard.py /tmp/bundle --out /tmp/dashboard.html
python3 skill_scripts/build_wbs_html.py wbs_spec.json --out /tmp/wbs.html
```

Si estos funcionan, la API y el MCP también — son wrappers finos.

## Troubleshooting rápido

| Síntoma | Causa probable | Solución |
|---|---|---|
| `MPXJ returned None` en `/extract` | Archivo truncado o formato corrupto | Verifica el tamaño del `.mpp` con `ls -la`; reenvía el archivo original sin pasar por un LLM |
| `Invalid Host header` en SSE | Protección DNS-rebinding del SDK MCP | Ya está deshabilitada en `mcp/server.py`; si regresa, revisa `TransportSecuritySettings` |
| `Resource not found` al llamar OpenAI / Claude | API key sin acceso al modelo especificado | Revisa el plan de la cuenta y el nombre exacto del modelo |
| Dashboard con barras Gantt apiladas al inicio | Regresión de CSS en las variantes de `.gantt-bar` | Verifica que las variantes hereden `position:absolute` de `.gantt-bar` |
| EDT publicada devuelve 404 después de redeploy | Falta el volumen persistente en `/data/published` | Monta el volumen en Railway o configura `MPP_PUBLISHED_DIR` fuera del almacenamiento efímero |

---

**Autor:** Mauricio Morales · Projectical · [mauricio.morales@projectical.com.co](mailto:mauricio.morales@projectical.com.co)

© 2026 Projectical. Este servicio consume la librería MPXJ (Apache 2.0) y se distribuye sin garantías.
