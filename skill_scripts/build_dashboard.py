#!/usr/bin/env python3
"""
build_dashboard.py — Render an extracted project bundle as a self-contained
interactive HTML dashboard with KPIs, a Gantt overview, Earned Value summary,
resource utilization and risk-task table.

Input can be either:
  - A bundle directory produced by extract_project.py, OR
  - A raw .mpp/.xml/etc file (the script will extract first into a temp dir).

Usage:
    python3 build_dashboard.py <bundle-dir OR raw-file> --out dashboard.html
    python3 build_dashboard.py proyecto.mpp --out dashboard.html --title "Sprint 12"

The HTML embeds Chart.js from cdn.jsdelivr.net; everything else is inline so
the dashboard can be opened locally or attached to an email without assets.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXTRACT = HERE / "extract_project.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", ""))
    except Exception:
        return None


def fnum(x):
    try:
        return float(str(x).replace(",", "").replace("$", "").split()[0])
    except Exception:
        return 0.0


def fdate(s):
    d = parse_dt(s)
    return d.strftime("%Y-%m-%d") if d else ""


def esc(s):
    return ("" if s is None else str(s)
            ).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ---------------------------------------------------------------------------
# Computation — mirrors query_project.py so KPIs match
# ---------------------------------------------------------------------------

def compute_evm(tasks, status_date, bl_index):
    bac = bcws = bcwp = acwp = 0.0
    rows = []
    for t in tasks:
        if t["summary"]:
            continue
        bl = (t.get("baseline_sets") or {}).get(str(bl_index)) or (t.get("baseline_sets") or {}).get(bl_index)
        if not bl:
            rows.append({**t, "_bac": 0, "_bcws": 0, "_bcwp": 0,
                         "_acwp": fnum(t.get("actual_cost"))})
            acwp += fnum(t.get("actual_cost"))
            continue
        t_bac = fnum(bl.get("cost"))
        bs = parse_dt(bl.get("start")); bf = parse_dt(bl.get("finish"))
        if t_bac == 0 or not bs or not bf:
            t_bcws = 0.0
        elif status_date >= bf:
            t_bcws = t_bac
        elif status_date <= bs:
            t_bcws = 0.0
        else:
            span = (bf - bs).total_seconds()
            elapsed = (status_date - bs).total_seconds()
            t_bcws = t_bac * (elapsed / span) if span > 0 else 0.0
        method = str(t.get("earned_value_method", "")).upper()
        pc = fnum(t.get("physical_percent_complete")) if method == "PHYSICAL_PERCENT_COMPLETE" \
             else fnum(t.get("percent_complete"))
        t_bcwp = t_bac * pc / 100.0
        t_acwp = fnum(t.get("actual_cost"))
        bac += t_bac; bcws += t_bcws; bcwp += t_bcwp; acwp += t_acwp
        rows.append({**t, "_bac": t_bac, "_bcws": t_bcws, "_bcwp": t_bcwp, "_acwp": t_acwp})
    cv = bcwp - acwp
    sv = bcwp - bcws
    cpi = (bcwp / acwp) if acwp else None
    spi = (bcwp / bcws) if bcws else None
    eac = (bac / cpi) if cpi else None
    vac = (bac - eac) if eac is not None else None
    return {"bac": bac, "bcws": bcws, "bcwp": bcwp, "acwp": acwp,
            "cv": cv, "sv": sv, "cpi": cpi, "spi": spi, "eac": eac, "vac": vac,
            "rows": rows}


def rank_risk_tasks(tasks, status_date, limit=8):
    out = []
    for t in tasks:
        if t["summary"]:
            continue
        pc = fnum(t.get("percent_complete"))
        if pc >= 100:
            continue
        finish = parse_dt(t.get("finish"))
        deadline = parse_dt(t.get("deadline"))
        slack_s = str(t.get("total_slack") or "0.0d")
        slack_d = 0.0
        try:
            slack_d = float(slack_s.rstrip("d").rstrip("h").rstrip("w"))
            if "h" in slack_s: slack_d /= 8
        except Exception:
            pass
        days_late = (status_date - finish).days if finish and finish < status_date else 0
        deadline_gap = (deadline - finish).days if deadline and finish else None
        risk_score = 0
        reason = []
        if days_late > 0:
            risk_score += days_late; reason.append(f"{days_late}d atrasada")
        if slack_d == 0 and pc < 100:
            risk_score += 5; reason.append("crítica")
        if deadline_gap is not None and deadline_gap < 0:
            risk_score += abs(deadline_gap) + 3; reason.append(f"deadline excedido ({deadline_gap}d)")
        elif deadline_gap is not None and deadline_gap < 3:
            risk_score += 2; reason.append(f"deadline ajustado ({deadline_gap}d)")
        if risk_score > 0:
            out.append({**t, "_risk_score": risk_score,
                        "_reason": ", ".join(reason) or "en curso sin holgura"})
    out.sort(key=lambda t: -t["_risk_score"])
    return out[:limit]


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dashboard — {title}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root {{
    --bg: #f7f8fa;
    --card: #ffffff;
    --ink: #1f2937;
    --mute: #6b7280;
    --line: #e5e7eb;
    --brand: #2563eb;
    --ok: #16a34a;
    --warn: #d97706;
    --bad: #dc2626;
    --crit: #7c3aed;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
  header {{ padding:24px 32px; background:var(--card); border-bottom:1px solid var(--line); }}
  h1 {{ margin:0 0 4px; font-size:22px; }}
  h2 {{ font-size:14px; text-transform:uppercase; letter-spacing:.06em; color:var(--mute); margin:0 0 12px; }}
  .meta {{ color:var(--mute); font-size:13px; }}
  .meta span + span:before {{ content:"  •  "; opacity:.4; }}
  main {{ padding:24px 32px; max-width:1400px; margin:0 auto; }}
  .grid {{ display:grid; gap:16px; }}
  .kpis {{ grid-template-columns:repeat(auto-fit, minmax(200px, 1fr)); }}
  .row2 {{ grid-template-columns: 1.4fr 1fr; }}
  .row-full {{ grid-template-columns: 1fr; }}
  @media (max-width: 960px) {{ .row2 {{ grid-template-columns: 1fr; }} }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:18px 20px; }}
  .kpi {{ text-align:left; }}
  .kpi .v {{ font-size:30px; font-weight:700; margin-top:6px; }}
  .kpi .sub {{ font-size:12px; color:var(--mute); margin-top:4px; }}
  .ok  {{ color:var(--ok); }} .warn {{ color:var(--warn); }}
  .bad {{ color:var(--bad); }} .crit{{ color:var(--crit); }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th, td {{ padding:8px 10px; text-align:left; border-bottom:1px solid var(--line); }}
  th {{ font-size:11px; text-transform:uppercase; letter-spacing:.05em; color:var(--mute); font-weight:600; }}
  tbody tr:hover {{ background:#f3f4f6; }}
  .pill {{ display:inline-block; padding:2px 8px; border-radius:999px; font-size:11px; font-weight:600; }}
  .pill.ok  {{ background:#dcfce7; color:#166534; }}
  .pill.bad {{ background:#fee2e2; color:#991b1b; }}
  .pill.warn{{ background:#fef3c7; color:#92400e; }}
  .pill.crit{{ background:#ede9fe; color:#5b21b6; }}
  .barwrap {{ background:#eef2f7; height:6px; border-radius:3px; margin-top:4px; }}
  .bar     {{ background:var(--brand); height:6px; border-radius:3px; }}
  .flag    {{ font-size:11px; color:var(--mute); padding:3px 8px; background:#f1f5f9; border-radius:4px; }}
  .flag.off {{ opacity:.5; text-decoration:line-through; }}
  footer {{ padding:16px 32px; color:var(--mute); font-size:12px; border-top:1px solid var(--line); }}
  canvas {{ max-width:100%; }}
</style>
</head>
<body>
<header>
  <h1>{title}</h1>
  <div class="meta">
    <span>Inicio: {start}</span>
    <span>Fin: {finish}</span>
    <span>Corte: {status}</span>
    <span>Moneda: {currency}</span>
    <span>Autor: {author}</span>
  </div>
  <div style="margin-top:10px;">{flags_html}</div>
</header>

<main class="grid" style="gap:24px;">

  <section class="grid kpis">
    <div class="card kpi">
      <h2>% Completo</h2>
      <div class="v {pct_color}">{overall_pct}%</div>
      <div class="sub">{n_done} hechas · {n_inprog} en curso · {n_notyet} pendientes</div>
      <div class="barwrap"><div class="bar" style="width:{overall_pct}%;"></div></div>
    </div>
    <div class="card kpi">
      <h2>SPI · Schedule</h2>
      <div class="v {spi_color}">{spi_str}</div>
      <div class="sub">SV = {sv_str}</div>
    </div>
    <div class="card kpi">
      <h2>CPI · Cost</h2>
      <div class="v {cpi_color}">{cpi_str}</div>
      <div class="sub">CV = {cv_str}</div>
    </div>
    <div class="card kpi">
      <h2>VAC · Budget at Completion</h2>
      <div class="v {vac_color}">{vac_str}</div>
      <div class="sub">EAC = {eac_str} · BAC = {bac_str}</div>
    </div>
  </section>

  <section class="grid row2">
    <div class="card">
      <h2>Gantt — todas las tareas</h2>
      <canvas id="gantt" height="{gantt_h}"></canvas>
    </div>
    <div class="card">
      <h2>Earned Value</h2>
      <canvas id="evm" height="240"></canvas>
      <table style="margin-top:14px;">
        <tbody>
          <tr><td>BAC</td><td style="text-align:right;">{bac_str}</td></tr>
          <tr><td>BCWS (PV)</td><td style="text-align:right;">{bcws_str}</td></tr>
          <tr><td>BCWP (EV)</td><td style="text-align:right;">{bcwp_str}</td></tr>
          <tr><td>ACWP (AC)</td><td style="text-align:right;">{acwp_str}</td></tr>
          <tr><td>EAC</td><td style="text-align:right;">{eac_str}</td></tr>
        </tbody>
      </table>
    </div>
  </section>

  <section class="grid row2">
    <div class="card">
      <h2>Recursos — trabajo planeado vs. real (horas)</h2>
      <canvas id="resources" height="240"></canvas>
    </div>
    <div class="card">
      <h2>Top tareas en riesgo</h2>
      <table>
        <thead><tr><th>ID</th><th>Tarea</th><th>Fin</th><th>%</th><th>Motivo</th></tr></thead>
        <tbody>{risk_rows}</tbody>
      </table>
    </div>
  </section>

  <section class="grid row-full">
    <div class="card">
      <h2>Ruta crítica</h2>
      <table>
        <thead><tr><th>ID</th><th>WBS</th><th>Tarea</th><th>Duración</th><th>Inicio</th><th>Fin</th><th>Slack</th><th>%</th></tr></thead>
        <tbody>{critical_rows}</tbody>
      </table>
    </div>
  </section>

</main>

<footer>
  Generado {generated_at} por mpp-reader · {counts}
</footer>

<script>
const ganttData = {gantt_data};
const ganttLabels = {gantt_labels};
const resData = {res_data};
const evmData = {evm_data};

// Gantt as horizontal floating bars via Chart.js
new Chart(document.getElementById('gantt'), {{
  type: 'bar',
  data: {{
    labels: ganttLabels,
    datasets: [{{
      label: 'Tareas',
      data: ganttData,
      backgroundColor: ganttData.map(d => d.critical ? '#7c3aed' : (d.done ? '#16a34a' : '#2563eb')),
      borderSkipped: false,
      barPercentage: 0.7,
    }}]
  }},
  options: {{
    indexAxis: 'y',
    responsive: true,
    plugins: {{ legend: {{ display: false }},
               tooltip: {{ callbacks: {{
                 label: (ctx) => `${{ctx.label}}: ${{ctx.raw.startLabel}} → ${{ctx.raw.finishLabel}} (${{ctx.raw.percent}}%)`
               }} }} }},
    scales: {{
      x: {{ type: 'time', time: {{ unit: 'week' }}, min: '{scale_min}', max: '{scale_max}' }},
      y: {{ ticks: {{ autoSkip: false, font: {{ size: 11 }} }} }}
    }},
    parsing: {{ xAxisKey: 'range', yAxisKey: 'y' }}
  }}
}});

new Chart(document.getElementById('evm'), {{
  type: 'bar',
  data: {{
    labels: ['BAC','BCWS','BCWP','ACWP','EAC'],
    datasets: [{{ label: 'Monto',
      data: evmData,
      backgroundColor: ['#94a3b8','#60a5fa','#34d399','#f97316','#a78bfa']
    }}]
  }},
  options: {{ responsive: true, plugins: {{ legend: {{ display:false }} }} }}
}});

new Chart(document.getElementById('resources'), {{
  type: 'bar',
  data: {{
    labels: resData.names,
    datasets: [
      {{ label: 'Planeado', data: resData.planned, backgroundColor: '#60a5fa' }},
      {{ label: 'Real',     data: resData.actual,  backgroundColor: '#f97316' }},
    ]
  }},
  options: {{ responsive: true, scales: {{ x: {{ stacked:false }}, y: {{ beginAtZero:true }} }} }}
}});
</script>
</body>
</html>
"""


def _flag_html(caps):
    flags = [
        ("has_baseline", "Con línea base", "Sin línea base"),
        ("has_actuals", "Con avance", "Sin avance"),
        ("has_status_date", "Fecha de corte", "Sin fecha de corte"),
        ("has_costs", "Con costos", "Sin costos"),
        ("has_predecessors", "Con precedencias", "Sin precedencias"),
        ("has_deadlines", "Con deadlines", "Sin deadlines"),
        ("uses_physical_percent_complete", "EV físico activado", ""),
    ]
    out = []
    for k, on, off in flags:
        v = caps.get(k)
        if v:
            out.append(f'<span class="flag">{esc(on)}</span>')
        elif off:
            out.append(f'<span class="flag off">{esc(off)}</span>')
    return " ".join(out)


def _spi_color(x):
    if x is None: return ""
    if x >= 0.98: return "ok"
    if x >= 0.90: return "warn"
    return "bad"

_cpi_color = _spi_color  # same thresholds for CPI

def _pct_color(x):
    if x >= 66: return "ok"
    if x >= 33: return "warn"
    return "bad"


def _fmt_money(x, cur):
    if x is None: return "n/a"
    sign = "-" if x < 0 else ""
    return f"{sign}{cur}{abs(x):,.0f}"


def _fmt_idx(x):
    return f"{x:.2f}" if x is not None else "n/a"


def render(bundle_dir: Path, out_path: Path, title_override: str | None = None):
    project = json.loads((bundle_dir / "project.json").read_text())
    tasks = json.loads((bundle_dir / "tasks.json").read_text())
    resources = json.loads((bundle_dir / "resources.json").read_text())
    assignments = json.loads((bundle_dir / "assignments.json").read_text())

    header = project["header"]
    caps = project["capabilities"]
    counts = project["counts"]
    cur = header.get("currency_symbol") or "$"
    title = title_override or header.get("title") or header.get("name") or "Proyecto"

    # Overall %
    overall_pct = fnum(header.get("project_percent_complete")) or 0.0
    if overall_pct == 0:
        total_w = sum(fnum(t.get("work_hours")) for t in tasks if not t["summary"])
        done_w = sum(fnum(t.get("work_hours")) * fnum(t.get("percent_complete")) / 100
                     for t in tasks if not t["summary"])
        overall_pct = (done_w / total_w * 100) if total_w else 0.0

    leaf = [t for t in tasks if not t["summary"]]
    n_done   = sum(1 for t in leaf if fnum(t.get("percent_complete")) >= 100)
    n_inprog = sum(1 for t in leaf if 0 < fnum(t.get("percent_complete")) < 100)
    n_notyet = sum(1 for t in leaf if fnum(t.get("percent_complete")) == 0)

    # Status date (fallback current → today)
    status_dt = (parse_dt(header.get("status_date"))
                 or parse_dt(header.get("current_date"))
                 or datetime.now())

    # EVM
    bl_index = header.get("baseline_for_earned_value") or 0
    evm = compute_evm(tasks, status_dt, bl_index)

    # Risk tasks
    risk = rank_risk_tasks(tasks, status_dt, limit=8)

    # Critical path (leaf, critical) sorted by start
    crit = [t for t in leaf if t.get("critical")]
    crit.sort(key=lambda t: parse_dt(t.get("start")) or datetime.max)

    # Resource utilization
    res_names = [r["name"] for r in resources if r.get("type") == "WORK"]
    res_planned = [fnum(r.get("work_hours")) for r in resources if r.get("type") == "WORK"]
    res_actual  = [fnum(r.get("actual_work_hours")) for r in resources if r.get("type") == "WORK"]

    # Gantt data
    gantt_tasks = [t for t in leaf if parse_dt(t.get("start")) and parse_dt(t.get("finish"))]
    gantt_tasks.sort(key=lambda t: parse_dt(t.get("start")))
    gantt_labels = [t["name"][:45] for t in gantt_tasks]
    gantt_data = []
    for t in gantt_tasks:
        s = parse_dt(t["start"]); f = parse_dt(t["finish"])
        gantt_data.append({
            "y": t["name"][:45],
            "range": [s.strftime("%Y-%m-%dT%H:%M:%S"), f.strftime("%Y-%m-%dT%H:%M:%S")],
            "startLabel": s.strftime("%Y-%m-%d"),
            "finishLabel": f.strftime("%Y-%m-%d"),
            "percent": int(fnum(t.get("percent_complete"))),
            "critical": bool(t.get("critical")),
            "done": fnum(t.get("percent_complete")) >= 100,
        })
    scale_min = (parse_dt(header.get("start_date")) or datetime.now()).strftime("%Y-%m-%d")
    scale_max = (parse_dt(header.get("finish_date")) or (status_dt + timedelta(days=30))).strftime("%Y-%m-%d")

    # Risk rows HTML
    risk_rows = []
    for t in risk:
        pc = int(fnum(t.get("percent_complete")))
        risk_rows.append(
            f"<tr><td>{esc(t['id'])}</td><td>{esc(t['name'])}</td>"
            f"<td>{esc(fdate(t.get('finish')))}</td>"
            f"<td>{pc}%</td><td><span class='pill bad'>{esc(t['_reason'])}</span></td></tr>"
        )
    if not risk_rows:
        risk_rows.append("<tr><td colspan='5' style='color:#16a34a;'>Sin tareas en riesgo</td></tr>")

    # Critical path rows HTML
    critical_rows = []
    for t in crit:
        critical_rows.append(
            f"<tr><td>{esc(t['id'])}</td><td>{esc(t['wbs'])}</td>"
            f"<td>{esc(t['name'])}</td><td>{esc(t.get('duration'))}</td>"
            f"<td>{esc(fdate(t['start']))}</td><td>{esc(fdate(t['finish']))}</td>"
            f"<td>{esc(t.get('total_slack'))}</td>"
            f"<td>{int(fnum(t.get('percent_complete')))}%</td></tr>"
        )
    if not critical_rows:
        critical_rows.append("<tr><td colspan='8'>No se detectaron tareas críticas</td></tr>")

    html = HTML_TEMPLATE.format(
        title=esc(title),
        start=esc(fdate(header.get("start_date"))),
        finish=esc(fdate(header.get("finish_date"))),
        status=esc(fdate(header.get("status_date")) or f"{fdate(header.get('current_date'))} (fallback)"),
        currency=esc(f"{header.get('currency_symbol') or ''} {header.get('currency_code') or ''}"),
        author=esc(header.get("author") or ""),
        flags_html=_flag_html(caps),
        overall_pct=f"{overall_pct:.0f}",
        pct_color=_pct_color(overall_pct),
        n_done=n_done, n_inprog=n_inprog, n_notyet=n_notyet,
        spi_str=_fmt_idx(evm["spi"]), spi_color=_spi_color(evm["spi"]),
        cpi_str=_fmt_idx(evm["cpi"]), cpi_color=_cpi_color(evm["cpi"]),
        sv_str=_fmt_money(evm["sv"], cur), cv_str=_fmt_money(evm["cv"], cur),
        vac_str=_fmt_money(evm["vac"], cur),
        vac_color="ok" if (evm["vac"] or 0) >= 0 else "bad",
        eac_str=_fmt_money(evm["eac"], cur),
        bac_str=_fmt_money(evm["bac"], cur),
        bcws_str=_fmt_money(evm["bcws"], cur),
        bcwp_str=_fmt_money(evm["bcwp"], cur),
        acwp_str=_fmt_money(evm["acwp"], cur),
        gantt_h=max(240, len(gantt_labels) * 22),
        gantt_data=json.dumps(gantt_data, ensure_ascii=False),
        gantt_labels=json.dumps(gantt_labels, ensure_ascii=False),
        res_data=json.dumps({"names": res_names, "planned": res_planned, "actual": res_actual}, ensure_ascii=False),
        evm_data=json.dumps([evm["bac"], evm["bcws"], evm["bcwp"], evm["acwp"], evm["eac"] or 0]),
        scale_min=scale_min, scale_max=scale_max,
        risk_rows="\n".join(risk_rows),
        critical_rows="\n".join(critical_rows),
        counts=(f"{counts['tasks']} tareas · {counts['resources']} recursos · "
                f"{counts['assignments']} asignaciones · {counts['calendars']} calendarios"),
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )

    out_path.write_text(html, encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Render a project bundle as an HTML dashboard.")
    ap.add_argument("input", help="Either a bundle dir (output of extract_project.py) OR a raw .mpp/.xml/.xer file")
    ap.add_argument("--out", required=True, help="Output HTML path")
    ap.add_argument("--title", default=None, help="Override project title")
    args = ap.parse_args()

    inp = Path(args.input).expanduser().resolve()
    if inp.is_file():
        # Extract first into a temp bundle
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "bundle"
            r = subprocess.run(
                ["python3", str(EXTRACT), str(inp), "--out", str(bundle)],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                print(r.stderr, file=sys.stderr)
                sys.exit(1)
            out = render(bundle, Path(args.out).expanduser().resolve(), args.title)
    elif inp.is_dir():
        out = render(inp, Path(args.out).expanduser().resolve(), args.title)
    else:
        print(f"[ERROR] not a file or directory: {inp}", file=sys.stderr)
        sys.exit(1)
    print(f"[mpp-reader] Wrote {out} ({out.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
