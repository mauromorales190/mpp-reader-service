# mpp-reader-service

Two transport wrappers around the **mpp-reader** skill, so you can call the same MS Project / Primavera analytics from anywhere. Both share one copy of the parsing scripts (`skill_scripts/`) and reuse the same MPXJ-backed extractor and query helper.

```
mpp-reader-service/
├── skill_scripts/        ← single source of truth (extract_project.py + query_project.py)
├── api/                  ← FastAPI REST server
│   ├── server.py
│   ├── requirements.txt
│   └── Dockerfile        → port 8080
├── mcp/                  ← MCP server (stdio for desktop clients, SSE for n8n)
│   ├── server.py
│   ├── requirements.txt
│   └── Dockerfile        → port 8765
├── n8n/
│   ├── workflow-rest-http.json   ← example: HTTP Request → REST API
│   └── workflow-mcp-agent.json   ← example: AI Agent → MCP Client → MCP server
├── docker-compose.yml    ← brings both up together
└── README.md
```

## Which one do I use?

| If you want to… | Use |
|---|---|
| Wire deterministic steps in n8n (form upload → query → Slack message) | **REST API** with the *HTTP Request* node |
| Let an AI Agent decide which query to run, in plain language | **MCP server** with the *AI Agent → MCP Client* nodes |
| Use the same backend from Claude Desktop, Cursor or other LLM clients | **MCP server** in stdio mode |
| Both | Run both — they share the scripts, no duplicated logic |

## Quick start (Docker Compose)

```bash
cd mpp-reader-service
docker compose up --build
```

That brings up:

- **REST API** at `http://localhost:8080` — visit `/docs` for interactive Swagger.
- **MCP server (SSE)** at `http://localhost:8765/sse` — point n8n's MCP Client at this URL.

If your n8n is also containerized, attach both to the same Docker network and use service names (`mpp-reader-api:8080`, `mpp-reader-mcp:8765`) instead of `localhost`.

## Quick start (local Python, no Docker)

```bash
# Java 11+ is required by MPXJ
java -version

# REST API
pip install -r api/requirements.txt
uvicorn api.server:app --host 0.0.0.0 --port 8080

# MCP server (in another shell)
pip install -r mcp/requirements.txt
python3 mcp/server.py --transport sse --host 0.0.0.0 --port 8765
```

## REST API surface

| Method | Path | Body |
|---|---|---|
| GET  | `/health`         | — |
| GET  | `/queries`        | — |
| POST | `/extract`        | multipart `file=@cronograma.mpp` |
| POST | `/query/{name}`   | multipart `file` + optional `task_name`, `wbs`, `days`, `limit` |
| POST | `/build`          | **JSON body** with project spec; `?format=xml\|mpx\|mpp`, `?download=true\|false` |

Available query names: `status, critical, network, overdue, upcoming, slack, evm, baseline, resources, customfields, calendars, find, summary-tree`.

Authentication: set `MPP_API_KEY=<secret>` and clients must send `X-API-Key: <secret>`.

### Generating a schedule (`/build`)

`/build` accepts a structured JSON spec and returns a Microsoft Project file. The default format is **Microsoft Project XML** — the official interchange format. MS Project opens it natively (File → Abrir → *.xml) and can Save As .mpp with one click.

Native `.mpp` output is only possible with **Aspose.Tasks** (commercial library, requires a license). If installed in the container, `?format=mpp` returns native .mpp; otherwise it returns `501` with a clear hint.

Minimum valid spec:

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

Full schema is documented in `skill_scripts/build_project.py` (module docstring) and surfaced in the MCP tool's description.

### Calling from `curl`

```bash
curl -F "file=@CRONOGRAMA.mpp" http://localhost:8080/extract | jq .project.counts
curl -F "file=@CRONOGRAMA.mpp" http://localhost:8080/query/critical | jq -r .output_text
curl -F "file=@CRONOGRAMA.mpp" -F "task_name=SALA" http://localhost:8080/query/find | jq -r .output_text
```

### Calling from n8n

Import `n8n/workflow-rest-http.json`. The flow reads a binary `.mpp` from disk, fans it out to two REST calls (status + EVM), and merges the result. To send the file from a webhook upload, swap the *Read Binary File* node for a *Webhook* node with `Binary Data → Yes`.

## MCP server surface

Four tools:

| Tool | Description |
|---|---|
| `list_queries` | Discover the canned analytical queries |
| `extract_project(file_b64, filename)` | Get the full structured bundle for custom analysis |
| `query_project(file_b64, filename, query, name?, days?, limit?, wbs?)` | Run one canned query |
| `build_project(spec, format="xml")` | Generate a Microsoft Project file from a structured JSON spec — returns `{filename, format, size_bytes, file_b64}` |

The file is passed as a **base64 string** so it survives JSON-RPC transport. n8n's binary-to-base64 conversion is one node (`Move Binary Data → Mode: Binary to JSON, Encode With: base64`) before the MCP Client call.

### Connecting from n8n's AI Agent

1. Add an *AI Agent* node, attach a chat model (Claude/OpenAI/etc.).
2. Add the *MCP Client Tool* node with **SSE Endpoint** = `http://mpp-reader-mcp:8765/sse`. Toggle "include all tools".
3. Wire the MCP Client into the AI Agent's `ai_tool` slot.
4. Test by asking: "Acá te paso un .mpp adjunto, dame el SPI y el camino crítico" — the agent will call `query_project` twice and compose the answer.

`n8n/workflow-mcp-agent.json` is a starter template.

### Connecting from Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "mpp-reader": {
      "command": "python3",
      "args": ["/absolute/path/to/mpp-reader-service/mcp/server.py"],
      "env": { "MPP_SCRIPTS_DIR": "/absolute/path/to/mpp-reader-service/skill_scripts" }
    }
  }
}
```

Restart Claude Desktop and the three tools appear under the 🔌 menu.

## Production checklist

Before exposing the API beyond your machine, do at minimum: set `MPP_API_KEY`, put it behind TLS (Caddy, Traefik, nginx), enforce a per-IP rate limit, and decide whether you want to keep `MPP_MAX_UPLOAD_MB` at 50 or raise it for very large enterprise schedules. The MCP server has no built-in auth in SSE mode — terminate it on the same private network as n8n (Docker Compose, k8s namespace) or front it with an auth proxy.

For larger files (>10 MB or >5,000 tasks), each request spawns a fresh JVM via `extract_project.py` which adds ~1 s of overhead. If throughput matters, you can persist the JVM by inlining the extractor inside the FastAPI process (`mpxj.startJVM()` once at app start) — the script is set up so this is a small refactor.

## Testing locally without Docker

The skill scripts are runnable on their own:

```bash
python3 skill_scripts/extract_project.py CRONOGRAMA.mpp --out /tmp/bundle
python3 skill_scripts/query_project.py /tmp/bundle critical
```

If those work, the API and MCP server will too — they're thin wrappers.
