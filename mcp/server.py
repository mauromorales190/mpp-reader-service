"""
mpp-reader-service / MCP server
-------------------------------

Exposes the mpp-reader skill as MCP tools so AI agents (Claude Desktop, Cursor,
n8n's "MCP Client" / "AI Agent" nodes, etc.) can read MS Project / Primavera
schedules and answer questions about them.

Tools exposed:

    extract_project(file_b64, filename)
        → full project bundle (header, capabilities, tasks, resources,
          assignments, calendars). Use this when you need rich data to filter
          or transform yourself.

    query_project(file_b64, filename, query, name=None, days=14, limit=50, wbs=None)
        → result of one canned analytical query. Use this for direct PM
          questions: status, critical path, EVM, baseline variance, overdue,
          upcoming, slack distribution, resources, custom fields, calendars,
          network, find, summary-tree.

    list_queries()
        → discover what `query_project` accepts and what each one does.

Run modes:

    # Default: stdio transport — Claude Desktop / Cursor will spawn this directly
    python3 server.py

    # SSE transport — for n8n (AI Agent → MCP Client tool) over HTTP
    python3 server.py --transport sse --host 0.0.0.0 --port 8765

Auth (SSE only):
    Set MPP_MCP_API_KEY; clients must send header `X-API-Key: <value>`.
"""

import argparse
import base64
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

# ---- configuration ----------------------------------------------------------

HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = Path(os.environ.get("MPP_SCRIPTS_DIR", HERE.parent / "skill_scripts"))
EXTRACT   = SCRIPTS_DIR / "extract_project.py"
QUERY     = SCRIPTS_DIR / "query_project.py"
BUILD     = SCRIPTS_DIR / "build_project.py"
DASHBOARD = SCRIPTS_DIR / "build_dashboard.py"

QUERIES = {
    "status":       "Overall % complete, dates and a list of tasks behind schedule.",
    "critical":     "Critical path: tasks whose Total Slack ≤ Critical Slack Limit.",
    "network":      "Predecessor/successor table for every leaf task.",
    "overdue":      "Tasks whose finish < status-date and % complete < 100.",
    "upcoming":     "Tasks starting within the next N days (param: days).",
    "slack":        "Total Slack distribution bucketed by float days.",
    "evm":          "Earned value: BCWS, BCWP, ACWP, CV, SV, CPI, SPI, EAC, ETC, VAC, TCPI.",
    "baseline":     "Variance against saved baseline (dates, work, cost).",
    "resources":    "Resource utilization, peak units, over-allocation flag.",
    "customfields": "Populated custom fields (TEXT*, NUMBER*, FLAG*, COST*, OUTLINE_CODE*) with aliases.",
    "calendars":    "Project + resource + task calendars and their exceptions.",
    "find":         "Filter tasks by name substring (param: name) or exact WBS (param: wbs).",
    "summary-tree": "Outline / WBS tree with %complete roll-up.",
}

# DNS-rebinding protection is on by default in the MCP SDK and only accepts
# host headers of 127.0.0.1 / localhost. Behind a reverse proxy / PaaS
# (Railway, Fly, Render, etc.) the Host header is the public hostname, so we
# disable the default protection. Over HTTPS the remaining attack surface is
# negligible, and callers still authenticate to the LLM/n8n side.
_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=False,
    allowed_hosts=["*"],
    allowed_origins=["*"],
)
mcp = FastMCP("mpp-reader", transport_security=_security)


# ---- internals --------------------------------------------------------------

def _decode_to_tempdir(file_b64: str, filename: str) -> tuple[Path, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    tmp_path = Path(tmp.name)
    in_path = tmp_path / filename
    in_path.write_bytes(base64.b64decode(file_b64))
    return tmp_path, tmp  # caller must keep tmp alive


def _extract(in_path: Path) -> Path:
    out_dir = in_path.parent / "bundle"
    out_dir.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["python3", str(EXTRACT), str(in_path), "--out", str(out_dir)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        parts = ["extract_project.py failed."]
        if r.stderr.strip(): parts.append(f"STDERR:\n{r.stderr.strip()}")
        if r.stdout.strip(): parts.append(f"STDOUT:\n{r.stdout.strip()}")
        raise RuntimeError("\n".join(parts))
    return out_dir


def _load_bundle(bundle: Path) -> dict[str, Any]:
    return {n: json.loads((bundle / f"{n}.json").read_text())
            for n in ("project", "tasks", "resources", "assignments")}


# ---- tools ------------------------------------------------------------------

@mcp.tool()
def list_queries() -> dict[str, str]:
    """List the canned queries that `query_project` accepts.

    Returns:
        A mapping of query name → short description.
    """
    return QUERIES


@mcp.tool()
def extract_project(file_b64: str, filename: str) -> dict[str, Any]:
    """Parse a MS Project / Primavera file and return the full structured bundle.

    Use this when you need to filter, transform, or compute over the raw schedule
    data yourself (e.g., custom report, joining with external data).

    Args:
        file_b64:  Base64-encoded contents of the file.
        filename:  Original filename — extension is used to auto-detect format
                   (.mpp, .mpx, .xml, .mpd, .xer, .pmxml, .pod, ...).

    Returns:
        Dict with keys: project (header + counts + capabilities + calendars +
        custom_fields), tasks, resources, assignments. Capabilities flags tell
        you what the file actually supports (`has_baseline`, `has_actuals`,
        `has_status_date`, `has_costs`, `has_custom_fields`).
    """
    base, tmp = _decode_to_tempdir(file_b64, filename)
    try:
        in_path = base / filename
        bundle = _extract(in_path)
        return _load_bundle(bundle)
    finally:
        tmp.cleanup()


@mcp.tool()
def build_dashboard(file_b64: str, filename: str, title: Optional[str] = None) -> dict[str, Any]:
    """Generate a self-contained HTML dashboard for the uploaded schedule.

    The dashboard includes: project header and capability flags, four KPI cards
    (overall % complete, SPI, CPI, VAC), a horizontal Gantt with color coding
    (green=done, blue=on track, purple=critical), an Earned Value summary table
    + bar chart (BAC, BCWS, BCWP, ACWP, EAC), resource utilization (planned vs
    actual hours), the top 8 tasks at risk, and the full critical path.

    Use this tool whenever the user asks for a "dashboard", "resumen visual",
    "reporte ejecutivo", "informe", or wants a shareable summary of the
    project status that can be emailed, opened in a browser or attached to a
    chat message.

    Args:
        file_b64:  Base64-encoded contents of the .mpp / .xml / .xer file.
        filename:  Original file name — extension used for format detection.
        title:     Optional override for the dashboard heading. If absent, uses
                   the project's own title.

    Returns:
        {filename, size_bytes, file_b64} — a ready-to-serve HTML file.
    """
    base, tmp = _decode_to_tempdir(file_b64, filename)
    try:
        in_path = base / filename
        html_path = base / "dashboard.html"
        cmd = ["python3", str(DASHBOARD), str(in_path), "--out", str(html_path)]
        if title:
            cmd += ["--title", title]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return {"error": r.stderr.strip() or r.stdout.strip()}
        data = html_path.read_bytes()
        out_name = (Path(filename).stem.replace(" ", "_") + "-dashboard.html")
        return {
            "filename": out_name,
            "size_bytes": len(data),
            "file_b64": base64.b64encode(data).decode("ascii"),
            "log": r.stdout.strip(),
        }
    finally:
        tmp.cleanup()


@mcp.tool()
def build_project(spec: dict, format: str = "xml") -> dict[str, Any]:
    """Generate a Microsoft Project schedule from a structured JSON spec.

    Default output is **Microsoft Project XML** (`.xml`) — the format that MS
    Project opens natively (File → Open → *.xml) and can immediately Save As
    a .mpp binary. Pass `format="mpp"` to attempt native binary output via
    Aspose.Tasks (commercial plugin; returns an error if not installed).

    Spec schema (all fields optional unless marked required):

      {
        "project": {                              # REQUIRED object
          "title": "string",                      # REQUIRED
          "author": "string", "manager": "string", "company": "string",
          "start_date": "YYYY-MM-DD",             # recommended
          "default_calendar": "Standard",
          "currency_symbol": "$", "currency_code": "COP",
          "minutes_per_day": 480, "minutes_per_week": 2400, "days_per_month": 20,
          "default_task_type": "FIXED_UNITS | FIXED_DURATION | FIXED_WORK",
          "default_task_ev_method": "PERCENT_COMPLETE | PHYSICAL_PERCENT_COMPLETE",
          "status_date": "YYYY-MM-DD"
        },
        "calendars": [                            # optional; Standard is auto-created
          {"name": "Standard",
           "working_days": ["MON","TUE","WED","THU","FRI"],
           "daily_hours": ["08:00-12:00","13:00-17:00"],
           "exceptions": [{"name":"Navidad","date":"2026-12-25","working": false}]}
        ],
        "resources": [
          {"id": 1, "name": "Ana", "type": "WORK",
           "max_units": 100, "standard_rate": "50/h",
           "overtime_rate": "75/h", "email": "...", "group": "..."},
          {"id": 2, "name": "Cemento", "type": "MATERIAL",
           "material_label": "m3", "standard_rate": 200},
          {"id": 3, "name": "Viáticos", "type": "COST"}
        ],
        "tasks": [                                # REQUIRED list
          {"id": 1, "name": "Fase 1", "outline_level": 1, "summary": true},
          {"id": 2, "name": "Diseño", "outline_level": 2,
           "duration": "5d",                       # "5d" | "40h" | "2w" | "30m"
           "start": "2026-05-04",                  # optional; scheduler computes if absent
           "type": "FIXED_WORK",
           "earned_value_method": "PHYSICAL_PERCENT_COMPLETE",
           "deadline": "2026-05-18",
           "percent_complete": 30,
           "physical_percent_complete": 40,
           "notes": "...", "wbs": "1.1",
           "milestone": false,
           "constraint_type": "ASAP | MUST_START_ON | ...",
           "constraint_date": "YYYY-MM-DD",
           "fixed_cost": 0,
           "actual_start": "YYYY-MM-DD",
           "actual_finish": "YYYY-MM-DD",
           "custom_fields": {"TEXT1": "Juan", "NUMBER3": 42},
           "predecessors": [{"id": 1, "type": "FS", "lag": "0d"}]}
        ],
        "assignments": [
          {"task_id": 2, "resource_id": 1, "units": 100, "work": "40h"},
          {"task_id": 2, "resource_id": 2, "units": 1, "work": "10 m3"}
        ],
        "options": {"save_baseline": true}        # snapshot current values as Baseline
      }

    Returns:
        {
          "filename": "ProjectTitle.xml",
          "format":   "xml" | "mpx" | "mpp",
          "size_bytes": 26800,
          "file_b64": "<base64 content of the file>",
          "log": "..."
        }
    Decode `file_b64` and save with the given `filename` on the client side.
    """
    if not isinstance(spec, dict) or not spec.get("project"):
        return {"error": "spec must be a dict with a 'project' key; see tool description."}
    fmt = (format or "xml").lower()
    if fmt not in ("xml", "mpx", "mpp"):
        return {"error": f"Unsupported format '{format}'. Use xml, mpx, or mpp."}
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        spec_path = tmp_p / "spec.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        ext = {"xml": ".xml", "mpx": ".mpx", "mpp": ".mpp"}[fmt]
        out = tmp_p / f"project{ext}"
        r = subprocess.run(
            ["python3", str(BUILD), str(spec_path), "--out", str(out), "--format", fmt],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return {"error": r.stderr.strip() or r.stdout.strip(),
                    "hint": "If format='mpp' failed, Aspose.Tasks isn't installed; "
                            "use format='xml' and open the file in MS Project, "
                            "then Save As → Project (*.mpp)."}
        data = out.read_bytes()
        title = (spec.get("project") or {}).get("title") or "project"
        fname = title.replace(" ", "_") + ext
        return {
            "filename": fname,
            "format": fmt,
            "size_bytes": len(data),
            "file_b64": base64.b64encode(data).decode("ascii"),
            "log": r.stdout.strip(),
        }


@mcp.tool()
def query_project(
    file_b64: str,
    filename: str,
    query: str,
    name: Optional[str] = None,
    days: int = 14,
    limit: int = 50,
    wbs: Optional[str] = None,
) -> dict[str, Any]:
    """Run one canned analytical query against an uploaded schedule file.

    Pick this over `extract_project` when the user asks a focused question
    that maps to a known query (status, critical path, earned value, etc.).

    Args:
        file_b64:  Base64-encoded contents of the file.
        filename:  Original filename (used to detect format by extension).
        query:     One of: status, critical, network, overdue, upcoming, slack,
                   evm, baseline, resources, customfields, calendars, find,
                   summary-tree. Call `list_queries` to see descriptions.
        name:      For `find`: substring match against task names.
        days:      For `upcoming`: window size in days (default 14).
        limit:     Max rows in tabular output (default 50).
        wbs:       For `find`: exact WBS match.

    Returns:
        Dict with keys:
          - `query`: the query name that was run
          - `capabilities`: capability flags (so the caller can warn the user
             when, e.g., EVM was requested but no baseline is saved)
          - `output_text`: the formatted, human-readable table/report
          - `output_stderr`: any warnings from the query script
    """
    if query not in QUERIES:
        return {"error": f"Unknown query '{query}'. Try one of: {', '.join(QUERIES)}"}

    base, tmp = _decode_to_tempdir(file_b64, filename)
    try:
        in_path = base / filename
        bundle = _extract(in_path)
        cmd = ["python3", str(QUERY), str(bundle), query,
               "--days", str(days), "--limit", str(limit)]
        if name:
            cmd += ["--name", name]
        if wbs:
            cmd += ["--wbs", wbs]
        r = subprocess.run(cmd, capture_output=True, text=True)
        return {
            "query": query,
            "capabilities": _load_bundle(bundle)["project"]["capabilities"],
            "output_text": r.stdout,
            "output_stderr": r.stderr,
        }
    finally:
        tmp.cleanup()


# ---- entry point ------------------------------------------------------------

def _build_http_app():
    """Build a Starlette app that serves the MCP SSE endpoint AND plain HTTP
    endpoints for direct file upload (bypassing the LLM entirely). This is how
    n8n avoids the base64-through-LLM truncation problem: it posts the binary
    directly to /extract or /dashboard, and only passes the small JSON bundle
    (or the final HTML) to the LLM."""
    from starlette.routing import Route
    from starlette.responses import JSONResponse, Response

    app = mcp.sse_app()

    async def http_health(request):
        return JSONResponse({
            "service": "mpp-reader-mcp",
            "status": "ok",
            "endpoints": ["/sse", "/messages/", "/extract", "/query/{name}",
                          "/dashboard", "/build", "/health"],
            "tools": ["list_queries", "extract_project", "query_project",
                      "build_project", "build_dashboard"],
        })

    async def http_extract(request):
        """POST multipart 'file' → full JSON bundle."""
        form = await request.form()
        upload = form.get("file")
        if upload is None:
            return JSONResponse({"error": "multipart field 'file' is required"}, status_code=400)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            in_path = tmp_p / (upload.filename or "project.mpp")
            in_path.write_bytes(await upload.read())
            try:
                bundle = _extract(in_path)
                return JSONResponse(_load_bundle(bundle))
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

    async def http_query(request):
        """POST multipart 'file' + path name → canned query result."""
        name = request.path_params["name"]
        if name not in QUERIES:
            return JSONResponse({"error": f"Unknown query '{name}'"}, status_code=400)
        form = await request.form()
        upload = form.get("file")
        if upload is None:
            return JSONResponse({"error": "multipart field 'file' is required"}, status_code=400)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            in_path = tmp_p / (upload.filename or "project.mpp")
            in_path.write_bytes(await upload.read())
            try:
                bundle = _extract(in_path)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)
            cmd = ["python3", str(QUERY), str(bundle), name,
                   "--days", str(form.get("days") or 14),
                   "--limit", str(form.get("limit") or 50)]
            if form.get("task_name"):
                cmd += ["--name", str(form.get("task_name"))]
            if form.get("wbs"):
                cmd += ["--wbs", str(form.get("wbs"))]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                return JSONResponse({"error": r.stderr or r.stdout}, status_code=500)
            return JSONResponse({
                "query": name,
                "capabilities": _load_bundle(bundle)["project"]["capabilities"],
                "output_text": r.stdout,
            })

    async def http_dashboard(request):
        """POST multipart 'file' (+ optional 'title') → HTML dashboard."""
        form = await request.form()
        upload = form.get("file")
        if upload is None:
            return JSONResponse({"error": "multipart field 'file' is required"}, status_code=400)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            in_path = tmp_p / (upload.filename or "project.mpp")
            in_path.write_bytes(await upload.read())
            html_path = tmp_p / "dashboard.html"
            cmd = ["python3", str(DASHBOARD), str(in_path), "--out", str(html_path)]
            if form.get("title"):
                cmd += ["--title", str(form.get("title"))]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                return JSONResponse({"error": r.stderr or r.stdout}, status_code=500)
            data = html_path.read_bytes()
            name = (in_path.stem.replace(" ", "_") or "project") + "-dashboard.html"
            return Response(
                content=data, media_type="text/html",
                headers={"Content-Disposition": f'attachment; filename="{name}"'}
            )

    app.routes.extend([
        Route("/health", http_health, methods=["GET"]),
        Route("/extract", http_extract, methods=["POST"]),
        Route("/query/{name}", http_query, methods=["POST"]),
        Route("/dashboard", http_dashboard, methods=["POST"]),
    ])
    return app


def main() -> None:
    ap = argparse.ArgumentParser(description="mpp-reader MCP server")
    ap.add_argument("--transport", default="stdio", choices=["stdio", "sse"],
                    help="stdio = local clients (Claude Desktop); sse = HTTP for remote clients (n8n)")
    ap.add_argument("--host", default="0.0.0.0", help="(sse) bind host")
    ap.add_argument("--port", type=int, default=8765, help="(sse) bind port")
    args = ap.parse_args()

    if args.transport == "sse":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        import uvicorn
        uvicorn.run(_build_http_app(), host=args.host, port=args.port)
    else:
        mcp.run()  # stdio


if __name__ == "__main__":
    main()
