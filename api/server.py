"""
mpp-reader-service / REST API
-----------------------------

FastAPI wrapper around the mpp-reader skill scripts. Endpoints:

    GET  /health                       → service + Java + MPXJ readiness
    GET  /queries                      → list of available queries + params
    POST /extract  (multipart: file)   → full JSON bundle (project+tasks+resources+assignments)
    POST /query/{name} (multipart: file + form params) → canned query result (JSON)

Auth (optional but recommended): set env MPP_API_KEY to require header `X-API-Key`.
Limits: set env MPP_MAX_UPLOAD_MB (default 50).

Run locally:
    pip install -r requirements.txt
    uvicorn server:app --host 0.0.0.0 --port 8080

Run in Docker:
    docker build -t mpp-reader-api .
    docker run -p 8080:8080 -e MPP_API_KEY=secret mpp-reader-api
"""

import base64
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

# ---- configuration ----------------------------------------------------------

HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = Path(os.environ.get("MPP_SCRIPTS_DIR", HERE.parent / "skill_scripts"))
EXTRACT   = SCRIPTS_DIR / "extract_project.py"
QUERY     = SCRIPTS_DIR / "query_project.py"
BUILD     = SCRIPTS_DIR / "build_project.py"
DASHBOARD = SCRIPTS_DIR / "build_dashboard.py"
MAX_UPLOAD_MB = int(os.environ.get("MPP_MAX_UPLOAD_MB", "50"))
API_KEY = os.environ.get("MPP_API_KEY", "")  # empty = no auth

SUPPORTED_EXT = {".mpp", ".mpx", ".xml", ".mpd", ".xer", ".pmxml",
                 ".pod", ".planner", ".pp", ".pep", ".fts", ".cdpx", ".gan", ".prx"}

QUERIES = {
    "status":       "Overall % complete, dates, behind-schedule tasks",
    "critical":     "Critical path (Total Slack ≤ Critical Slack Limit)",
    "network":      "Predecessor/successor table",
    "overdue":      "Tasks whose finish < status-date and %<100",
    "upcoming":     "Tasks starting in the next N days (param: days)",
    "slack":        "Distribution of Total Slack / Free Slack",
    "evm":          "BCWS/BCWP/ACWP, CV/SV/CPI/SPI/EAC/ETC/VAC/TCPI",
    "baseline":     "Variance vs baseline (dates, work, cost)",
    "resources":    "Resource utilization & over-allocation",
    "customfields": "Populated custom fields, grouped by alias",
    "calendars":    "Calendars and exceptions",
    "find":         "Filter tasks by name (param: name) or WBS (param: wbs)",
    "summary-tree": "Outline / WBS tree with %complete",
}

# ---- app --------------------------------------------------------------------

app = FastAPI(
    title="mpp-reader API",
    version="1.0.0",
    description="Parse MS Project / Primavera schedule files and answer PM questions over HTTP.",
)


def _auth(x_api_key: Optional[str] = Header(default=None)):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


async def _save_upload(file: UploadFile, dest_dir: Path) -> Path:
    ext = Path(file.filename or "").suffix.lower()
    if ext and ext not in SUPPORTED_EXT:
        raise HTTPException(415, f"Unsupported file extension '{ext}'. "
                                 f"Supported: {', '.join(sorted(SUPPORTED_EXT))}")
    dest = dest_dir / (file.filename or "input.mpp")
    # stream-copy with limit
    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    total = 0
    with open(dest, "wb") as f:
        while chunk := await file.read(64 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise HTTPException(413, f"Upload exceeds {MAX_UPLOAD_MB} MB")
            f.write(chunk)
    if total == 0:
        raise HTTPException(400, "Empty upload")
    return dest


def _run(cmd: list[str]) -> tuple[str, str, int]:
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.stdout, r.stderr, r.returncode


def _extract_bundle(in_file: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    stdout, stderr, code = _run(["python3", str(EXTRACT), str(in_file), "--out", str(out_dir)])
    if code != 0:
        raise HTTPException(500, f"extract_project.py failed: {stderr or stdout}")


def _load_bundle(bundle_dir: Path) -> dict:
    return {
        "project":     json.loads((bundle_dir / "project.json").read_text()),
        "tasks":       json.loads((bundle_dir / "tasks.json").read_text()),
        "resources":   json.loads((bundle_dir / "resources.json").read_text()),
        "assignments": json.loads((bundle_dir / "assignments.json").read_text()),
    }


# ---- routes -----------------------------------------------------------------

@app.get("/health")
def health():
    """Liveness + readiness: checks Java, MPXJ import, and scripts on disk."""
    info = {
        "service": "mpp-reader",
        "version": app.version,
        "scripts_dir": str(SCRIPTS_DIR),
        "extract_present": EXTRACT.exists(),
        "query_present": QUERY.exists(),
        "auth_required": bool(API_KEY),
        "max_upload_mb": MAX_UPLOAD_MB,
    }
    # quick Java probe
    jv, _, code = _run(["java", "-version"])
    info["java"] = "ok" if code == 0 else "missing"
    # quick MPXJ probe
    out, err, code = _run(["python3", "-c", "import mpxj; print('ok')"])
    info["mpxj"] = out.strip() if code == 0 else f"missing: {err.strip()}"
    return info


@app.get("/queries")
def queries():
    return {"queries": QUERIES}


@app.post("/extract", dependencies=[Depends(_auth)])
async def extract(file: UploadFile = File(..., description="MS Project / Primavera file")):
    """Extract the full project bundle: header, capabilities, tasks, resources, assignments, calendars."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        in_path = await _save_upload(file, tmp)
        out_dir = tmp / "bundle"
        _extract_bundle(in_path, out_dir)
        return JSONResponse(_load_bundle(out_dir))


@app.post("/query/{name}", dependencies=[Depends(_auth)])
async def query(
    name: str,
    file: UploadFile = File(...),
    days: Optional[int] = Form(default=14),
    limit: Optional[int] = Form(default=50),
    task_name: Optional[str] = Form(default=None),  # maps to --name (renamed to avoid clash)
    wbs: Optional[str] = Form(default=None),
):
    """Run one of the canned queries on the uploaded file. See /queries for the list."""
    if name not in QUERIES:
        raise HTTPException(400, f"Unknown query '{name}'. See /queries.")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        in_path = await _save_upload(file, tmp)
        out_dir = tmp / "bundle"
        _extract_bundle(in_path, out_dir)

        cmd = ["python3", str(QUERY), str(out_dir), name,
               "--days", str(days), "--limit", str(limit)]
        if task_name:
            cmd += ["--name", task_name]
        if wbs:
            cmd += ["--wbs", wbs]
        stdout, stderr, code = _run(cmd)
        if code != 0:
            raise HTTPException(500, f"query_project.py failed: {stderr or stdout}")

        return {
            "query": name,
            "capabilities": _load_bundle(out_dir)["project"]["capabilities"],
            "output_text": stdout,
            "output_stderr": stderr,
        }


# ---- BUILD ENDPOINT ---------------------------------------------------------
#
# Generate a Microsoft Project XML (or, optionally, native .mpp via Aspose) from
# a JSON spec. Project XML opens natively in MS Project — File → Abrir → *.xml
# — and can be Saved As .mpp with one click.

@app.post("/build", dependencies=[Depends(_auth)])
async def build(
    spec: dict = Body(..., description="Project spec (see README for schema)"),
    format: str = Query("xml", pattern="^(xml|mpx|mpp)$"),
    download: bool = Query(True, description="If true, return the file as attachment; else return base64 JSON"),
):
    """Generate a Microsoft Project file from a JSON spec.

    Default format is **xml** — the Microsoft Project interchange format that
    MS Project opens natively. Pass `format=mpp` to attempt native .mpp via
    Aspose.Tasks (returns 501 if Aspose isn't installed).

    Send the spec as a JSON request body. Minimum required fields:
      - project.title
      - tasks: list with at least {id, name, duration} entries
    See README for the full schema.
    """
    if not isinstance(spec, dict) or not spec.get("project"):
        raise HTTPException(400, "Body must be a JSON object with a 'project' key.")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        spec_path = tmp / "spec.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        ext = {"xml": ".xml", "mpx": ".mpx", "mpp": ".mpp"}[format]
        out_path = tmp / f"project{ext}"
        stdout, stderr, code = _run(
            ["python3", str(BUILD), str(spec_path), "--out", str(out_path), "--format", format]
        )
        if code != 0:
            status = 501 if "Aspose" in stderr else 500
            raise HTTPException(status, f"build_project.py failed: {stderr or stdout}")

        data = out_path.read_bytes()
        filename = f"{(spec.get('project') or {}).get('title', 'project').replace(' ','_')}{ext}"
        if download:
            media = {"xml": "application/xml",
                     "mpx": "text/plain",
                     "mpp": "application/vnd.ms-project"}[format]
            return Response(content=data, media_type=media,
                            headers={"Content-Disposition": f'attachment; filename="{filename}"'})
        return {
            "filename": filename,
            "format": format,
            "size_bytes": len(data),
            "file_b64": base64.b64encode(data).decode("ascii"),
            "log": stdout.strip(),
        }


# ---- DASHBOARD ENDPOINT -----------------------------------------------------

@app.post("/dashboard", dependencies=[Depends(_auth)])
async def dashboard(
    file: UploadFile = File(..., description="MS Project / Primavera file"),
    title: Optional[str] = Form(default=None),
    download: bool = Query(True, description="If true, return HTML as attachment; else JSON with base64"),
):
    """Render an HTML dashboard for the uploaded schedule.

    The HTML is a single self-contained page with KPIs (overall %, SPI, CPI,
    VAC), a Gantt-style overview, an Earned Value summary, resource utilization
    and a risk-task table. It loads Chart.js from cdn.jsdelivr.net; everything
    else is inlined so it opens offline after the first fetch.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        in_path = await _save_upload(file, tmp)
        html_path = tmp / "dashboard.html"
        cmd = ["python3", str(DASHBOARD), str(in_path), "--out", str(html_path)]
        if title:
            cmd += ["--title", title]
        stdout, stderr, code = _run(cmd)
        if code != 0:
            raise HTTPException(500, f"build_dashboard.py failed: {stderr or stdout}")
        data = html_path.read_bytes()
        base = in_path.stem.replace(" ", "_")
        fname = f"{base}-dashboard.html"
        if download:
            return Response(content=data, media_type="text/html",
                            headers={"Content-Disposition": f'attachment; filename="{fname}"'})
        return {
            "filename": fname,
            "size_bytes": len(data),
            "file_b64": base64.b64encode(data).decode("ascii"),
            "log": stdout.strip(),
        }
