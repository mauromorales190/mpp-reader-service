#!/usr/bin/env python3
"""
build_dashboard.py — Render an extracted project bundle as a self-contained
HTML dashboard with KPIs, a CSS-based Gantt, an Earned Value S-curve, resource
utilization, and the top tasks at risk.

Input can be either:
  - A bundle directory produced by extract_project.py, OR
  - A raw .mpp/.xml/etc file (the script will extract first into a temp dir).

Usage:
    python3 build_dashboard.py <bundle-dir OR raw-file> --out dashboard.html
    python3 build_dashboard.py proyecto.mpp --out dashboard.html --title "Sprint 12"
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXTRACT = HERE / "extract_project.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_dt(s):
    if not s: return None
    try: return datetime.fromisoformat(str(s).replace("Z", ""))
    except Exception: return None


def fnum(x):
    try: return float(x)
    except Exception:
        try: return float(str(x).replace(",", "").replace("$", "").split()[0])
        except Exception: return 0.0


def fdate(s):
    d = parse_dt(s)
    return d.strftime("%Y-%m-%d") if d else ""


def esc(s):
    return ("" if s is None else str(s)
            ).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _fmt_money(x, cur):
    if x is None: return "n/a"
    sign = "-" if x < 0 else ""
    return f"{sign}{cur}{abs(x):,.0f}"


def _fmt_idx(x):
    return f"{x:.2f}" if x is not None else "n/a"


# ---------------------------------------------------------------------------
# EVM computation + S-curve
# ---------------------------------------------------------------------------

def _task_evm_inputs(tasks, bl_index):
    """Extract per-leaf-task baseline + progress data used by both the KPI
    totals and the S-curve weekly buckets."""
    out = []
    for t in tasks:
        if t["summary"]:
            continue
        bl = (t.get("baseline_sets") or {}).get(str(bl_index)) or (t.get("baseline_sets") or {}).get(bl_index)
        if not bl:
            # Task has no baseline (probably added after baseline was saved).
            # It still contributes actual_cost to ACWP but 0 to BAC/BCWS/BCWP.
            out.append({
                "bs": None, "bf": None, "bac": 0.0, "pct": 0.0,
                "acwp": fnum(t.get("actual_cost")),
            })
            continue
        bs = parse_dt(bl.get("start"))
        bf = parse_dt(bl.get("finish"))
        bac = fnum(bl.get("cost"))
        method = str(t.get("earned_value_method") or "").upper()
        if method == "PHYSICAL_PERCENT_COMPLETE":
            pct = fnum(t.get("physical_percent_complete"))
        else:
            pct = fnum(t.get("percent_complete"))
        out.append({
            "bs": bs, "bf": bf, "bac": bac, "pct": pct / 100.0,
            "acwp": fnum(t.get("actual_cost")),
        })
    return out


def compute_evm_totals(task_inputs, status_date):
    """Current totals at the status date."""
    bac = bcws = bcwp = acwp = 0.0
    for t in task_inputs:
        bac += t["bac"]
        if t["bs"] and t["bf"] and t["bac"]:
            if status_date >= t["bf"]:
                bcws += t["bac"]
            elif status_date <= t["bs"]:
                bcws += 0
            else:
                span = (t["bf"] - t["bs"]).total_seconds()
                elapsed = (status_date - t["bs"]).total_seconds()
                bcws += t["bac"] * (elapsed / span) if span > 0 else 0
        bcwp += t["bac"] * t["pct"]
        acwp += t["acwp"]
    cv = bcwp - acwp
    sv = bcwp - bcws
    cpi = (bcwp / acwp) if acwp else None
    spi = (bcwp / bcws) if bcws else None
    eac = (bac / cpi) if cpi else None
    vac = (bac - eac) if eac is not None else None
    return {"bac": bac, "bcws": bcws, "bcwp": bcwp, "acwp": acwp,
            "cv": cv, "sv": sv, "cpi": cpi, "spi": spi, "eac": eac, "vac": vac}


def compute_evm_curve(task_inputs, status_date):
    """Return a list of weekly {date, bcws, bcwp, acwp} points for a line chart.

    - BCWS is a true S-curve built from baseline start/finish/cost per task,
      accruing linearly across each task's baseline span.
    - BCWP and ACWP are ramped linearly from 0 at project start to their
      current totals at the status date (we don't have real timephased
      history in the .mpp, so this is the best visualization available).
      After the status date they stay flat at the current value to make the
      'current vs plan' gap easy to read.
    """
    tasks_w_baseline = [t for t in task_inputs if t["bs"] and t["bf"] and t["bac"]]
    if not tasks_w_baseline:
        return []

    start = min(t["bs"] for t in tasks_w_baseline)
    baseline_end = max(t["bf"] for t in tasks_w_baseline)
    curve_end = max(baseline_end, status_date) + timedelta(days=7)

    total_bcwp = sum(t["bac"] * t["pct"] for t in task_inputs)
    total_acwp = sum(t["acwp"] for t in task_inputs)

    points = []
    cur = start
    while cur <= curve_end:
        bcws = 0.0
        for t in tasks_w_baseline:
            if cur >= t["bf"]:
                bcws += t["bac"]
            elif cur <= t["bs"]:
                pass
            else:
                span = (t["bf"] - t["bs"]).total_seconds()
                elapsed = (cur - t["bs"]).total_seconds()
                bcws += t["bac"] * (elapsed / span) if span > 0 else 0
        # BCWP / ACWP linear ramp from 0 to current totals
        if cur <= start:
            bcwp = acwp = 0.0
        elif cur >= status_date:
            bcwp = total_bcwp
            acwp = total_acwp
        else:
            frac = (cur - start).total_seconds() / max((status_date - start).total_seconds(), 1)
            bcwp = total_bcwp * frac
            acwp = total_acwp * frac
        points.append({
            "date": cur.strftime("%Y-%m-%d"),
            "bcws": round(bcws, 2),
            "bcwp": round(bcwp, 2),
            "acwp": round(acwp, 2),
        })
        cur += timedelta(days=7)
    return points


# ---------------------------------------------------------------------------
# Resource utilization — compute from ASSIGNMENTS (more reliable than
# Resource.getWork which depends on MPXJ roll-ups being present)
# ---------------------------------------------------------------------------

def resource_utilization(resources, assignments):
    planned = defaultdict(float)
    actual = defaultdict(float)
    cost = defaultdict(float)
    rname = {r["id"]: r["name"] for r in resources}
    rtype = {r["id"]: r.get("type") for r in resources}
    for a in assignments:
        rid = a.get("resource_id")
        if rid is None or rid not in rname:
            continue
        if rtype.get(rid) != "WORK":
            continue  # only show work resources on the hour chart
        planned[rid] += fnum(a.get("work_hours"))
        actual[rid]  += fnum(a.get("actual_work_hours"))
        cost[rid]    += fnum(a.get("cost"))
    rows = []
    for rid, name in rname.items():
        if rtype.get(rid) != "WORK":
            continue
        rows.append({
            "id": rid, "name": name,
            "planned": round(planned[rid], 1),
            "actual":  round(actual[rid], 1),
            "cost":    round(cost[rid], 2),
        })
    # If no assignment-derived numbers at all, fall back to the resource's
    # own work_hours so the chart isn't empty.
    if all(r["planned"] == 0 and r["actual"] == 0 for r in rows):
        for r in rows:
            res = next((x for x in resources if x["id"] == r["id"]), None)
            if res:
                r["planned"] = fnum(res.get("work_hours"))
                r["actual"]  = fnum(res.get("actual_work_hours"))
    rows.sort(key=lambda r: r["planned"], reverse=True)
    return rows


# ---------------------------------------------------------------------------
# Risk ranking
# ---------------------------------------------------------------------------

def rank_risk_tasks(tasks, status_date, limit=10):
    out = []
    for t in tasks:
        if t["summary"]: continue
        pc = fnum(t.get("percent_complete"))
        if pc >= 100: continue
        finish = parse_dt(t.get("finish"))
        deadline = parse_dt(t.get("deadline"))
        slack_s = str(t.get("total_slack") or "0.0d")
        try:
            slack_d = float(slack_s.rstrip("dhwm"))
            if "h" in slack_s: slack_d /= 8
        except Exception:
            slack_d = 0
        days_late = (status_date - finish).days if finish and finish < status_date else 0
        risk_score = 0
        reason = []
        if days_late > 0:
            risk_score += days_late; reason.append(f"{days_late}d atrasada")
        if slack_d == 0 and pc < 100:
            risk_score += 5; reason.append("crítica")
        if deadline and finish and (deadline - finish).days < 0:
            risk_score += abs((deadline - finish).days) + 3
            reason.append(f"deadline excedido ({(deadline - finish).days}d)")
        if risk_score > 0:
            out.append({**t, "_risk_score": risk_score,
                        "_reason": ", ".join(reason) or "en curso sin holgura"})
    out.sort(key=lambda t: -t["_risk_score"])
    return out[:limit]


# ---------------------------------------------------------------------------
# HTML template
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
    --bg:#f7f8fa; --card:#fff; --ink:#1f2937; --mute:#6b7280; --line:#e5e7eb;
    --brand:#2563eb; --ok:#16a34a; --warn:#d97706; --bad:#dc2626; --crit:#7c3aed;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; font-size:14px; }}
  header {{ padding:22px 32px; background:var(--card); border-bottom:1px solid var(--line); }}
  h1 {{ margin:0 0 6px; font-size:22px; }}
  h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.06em; color:var(--mute); margin:0 0 12px; font-weight:600; }}
  .meta {{ color:var(--mute); font-size:13px; }}
  .meta span+span:before {{ content:" · "; opacity:.4; }}
  main {{ padding:22px 32px; max-width:1400px; margin:0 auto; }}
  .grid {{ display:grid; gap:18px; }}
  .kpis {{ grid-template-columns:repeat(auto-fit, minmax(200px, 1fr)); }}
  .row2 {{ grid-template-columns: 1.4fr 1fr; }}
  .row-full {{ grid-template-columns: 1fr; }}
  @media (max-width:960px) {{ .row2 {{ grid-template-columns: 1fr; }} }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:18px 20px; }}
  .kpi .v {{ font-size:30px; font-weight:700; margin-top:6px; }}
  .kpi .sub {{ font-size:12px; color:var(--mute); margin-top:4px; }}
  .ok {{ color:var(--ok); }} .warn {{ color:var(--warn); }}
  .bad {{ color:var(--bad); }} .crit {{ color:var(--crit); }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th,td {{ padding:8px 10px; text-align:left; border-bottom:1px solid var(--line); }}
  th {{ font-size:11px; text-transform:uppercase; letter-spacing:.05em; color:var(--mute); font-weight:600; }}
  tbody tr:hover {{ background:#f3f4f6; }}
  .pill {{ display:inline-block; padding:2px 8px; border-radius:999px; font-size:11px; font-weight:600; }}
  .pill.ok {{ background:#dcfce7; color:#166534; }}
  .pill.bad {{ background:#fee2e2; color:#991b1b; }}
  .pill.crit {{ background:#ede9fe; color:#5b21b6; }}
  .barwrap {{ background:#eef2f7; height:6px; border-radius:3px; margin-top:4px; }}
  .bar {{ background:var(--brand); height:6px; border-radius:3px; }}
  .flag {{ font-size:11px; color:var(--mute); padding:3px 8px; background:#f1f5f9; border-radius:4px; }}
  .flag.off {{ opacity:.5; text-decoration:line-through; }}
  footer {{ padding:16px 32px; color:var(--mute); font-size:12px; border-top:1px solid var(--line); }}
  canvas {{ max-width:100%; }}

  /* --- Gantt CSS nativo (no Chart.js) — estilo Tracking Gantt de MS Project --- */
  .gantt {{ font-size:12px; }}
  .gantt-header {{ display:flex; border-bottom:2px solid var(--line); height:28px; background:#f9fafb; }}
  .gantt-header-label {{ width:240px; padding:8px 10px; font-weight:600; color:var(--mute); font-size:11px; text-transform:uppercase; letter-spacing:.05em; flex-shrink:0; }}
  .gantt-header-axis {{ flex:1; position:relative; }}
  .gantt-tick {{ position:absolute; top:0; bottom:0; border-left:1px dashed #e5e7eb; font-size:10px; color:var(--mute); padding-left:3px; padding-top:8px; }}
  /* Each row holds TWO bars: the scheduled bar on top, and a baseline bar below it. */
  .gantt-row {{ display:flex; height:32px; border-bottom:1px solid #f3f4f6; align-items:center; }}
  .gantt-row:hover {{ background:#f9fafb; }}
  .gantt-label {{ width:240px; padding:0 10px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; flex-shrink:0; }}
  .gantt-label.summary {{ font-weight:700; color:#111827; }}
  .gantt-label.milestone {{ color:var(--crit); font-weight:600; }}
  .gantt-track {{ flex:1; position:relative; height:32px; background:#fafbfc; }}
  .gantt-today {{ position:absolute; top:0; bottom:0; width:1px; background:var(--bad); z-index:4; pointer-events:none; }}
  .gantt-today:before {{ content:'hoy'; position:absolute; top:-18px; left:-10px; font-size:9px; color:var(--bad); font-weight:700; white-space:nowrap; }}

  /* === Scheduled bar (top half of the row) === */
  .gantt-bar {{ position:absolute; top:4px; height:11px; border-radius:2px; min-width:3px; z-index:2; }}
  .gantt-bar.normal   {{ background:var(--brand); }}
  .gantt-bar.done     {{ background:var(--ok); }}
  .gantt-bar.critical {{ background:var(--crit); }}
  /* Summary (scheduled): thin black bar with triangular ends */
  .gantt-bar.summary  {{ background:#111827; height:6px; top:5px; border-radius:1px; }}
  .gantt-bar.summary:before,
  .gantt-bar.summary:after {{ content:''; position:absolute; top:6px; width:0; height:0;
     border-top:5px solid #111827; border-left:3px solid transparent; border-right:3px solid transparent; }}
  .gantt-bar.summary:before {{ left:0; }}
  .gantt-bar.summary:after  {{ right:0; }}
  /* Milestone (scheduled): diamond centered on the date */
  .gantt-bar.milestone {{ background:var(--crit); width:12px !important; height:12px;
     transform:translateX(-50%) rotate(45deg); border-radius:1px; top:4px; }}
  /* Progress overlay: blanquea la parte no completada */
  .gantt-progress {{ position:absolute; top:0; height:100%; background:rgba(255,255,255,0.45); border-radius:0 2px 2px 0; }}

  /* === Baseline bar (bottom half of the row) — Tracking Gantt style === */
  .gantt-baseline {{ position:absolute; top:18px; height:8px; border-radius:2px; min-width:3px;
                     background:#6b7280; opacity:0.85; z-index:1; }}
  .gantt-baseline.summary  {{ background:#4b5563; height:4px; top:20px; border-radius:1px; }}
  .gantt-baseline.summary:before,
  .gantt-baseline.summary:after {{ content:''; position:absolute; top:4px; width:0; height:0;
     border-top:4px solid #4b5563; border-left:3px solid transparent; border-right:3px solid transparent; }}
  .gantt-baseline.summary:before {{ left:0; }}
  .gantt-baseline.summary:after  {{ right:0; }}
  .gantt-baseline.milestone {{ background:#6b7280; width:9px !important; height:9px;
     transform:translateX(-50%) rotate(45deg); border-radius:1px; top:19px; opacity:0.85; }}

  /* === Legend === */
  .gantt-legend {{ display:flex; gap:14px; margin-top:10px; font-size:11px; color:var(--mute); align-items:center; flex-wrap:wrap; }}
  .gantt-legend .sw {{ display:inline-block; width:14px; height:8px; border-radius:2px; margin-right:5px; vertical-align:middle; }}
  .gantt-legend .sw-milestone {{ width:8px; height:8px; transform:rotate(45deg); border-radius:1px; }}
  .gantt-legend .sw-summary {{ height:4px; background:#111827; }}
  .gantt-legend .sw-baseline {{ background:#6b7280; opacity:0.85; }}
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
      <h2>VAC · Variance at Completion</h2>
      <div class="v {vac_color}">{vac_str}</div>
      <div class="sub">EAC = {eac_str} · BAC = {bac_str}</div>
    </div>
  </section>

  <section class="grid row-full">
    <div class="card">
      <h2>Gantt — todas las tareas</h2>
      {gantt_html}
    </div>
  </section>

  <section class="grid row2">
    <div class="card">
      <h2>Curva de Valor Ganado (BCWS / BCWP / ACWP)</h2>
      <canvas id="evm" height="280"></canvas>
      <table style="margin-top:14px;">
        <tbody>
          <tr><td>BAC</td><td style="text-align:right;">{bac_str}</td></tr>
          <tr><td>BCWS (Planned Value)</td><td style="text-align:right;">{bcws_str}</td></tr>
          <tr><td>BCWP (Earned Value)</td><td style="text-align:right;">{bcwp_str}</td></tr>
          <tr><td>ACWP (Actual Cost)</td><td style="text-align:right;">{acwp_str}</td></tr>
          <tr><td>EAC</td><td style="text-align:right;">{eac_str}</td></tr>
        </tbody>
      </table>
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
      <h2>Utilización de recursos — horas planeadas vs. reales</h2>
      <canvas id="resources" height="{res_h}"></canvas>
      <table style="margin-top:14px;">
        <thead><tr><th>Recurso</th><th style="text-align:right;">Planeado (h)</th><th style="text-align:right;">Real (h)</th><th style="text-align:right;">Costo</th></tr></thead>
        <tbody>{res_rows}</tbody>
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

<footer>Generado {generated_at} por mpp-reader · {counts}</footer>

<script>
const evmCurve = {evm_curve};
const resData  = {res_data};
const cur      = {currency_js};

function fmt(v) {{ return cur + v.toLocaleString('es-CO', {{maximumFractionDigits: 0}}); }}

new Chart(document.getElementById('evm'), {{
  type: 'line',
  data: {{
    labels: evmCurve.map(p => p.date),
    datasets: [
      {{ label: 'BCWS (planeado)', data: evmCurve.map(p => p.bcws),
         borderColor: '#60a5fa', backgroundColor: 'rgba(96,165,250,0.15)',
         borderWidth: 2, tension: 0.25, pointRadius: 0, fill: true }},
      {{ label: 'BCWP (ganado)',  data: evmCurve.map(p => p.bcwp),
         borderColor: '#16a34a', borderWidth: 2, tension: 0.25,
         borderDash: [], pointRadius: 0, fill: false }},
      {{ label: 'ACWP (real)',    data: evmCurve.map(p => p.acwp),
         borderColor: '#dc2626', borderWidth: 2, tension: 0.25,
         borderDash: [6,3], pointRadius: 0, fill: false }},
    ]
  }},
  options: {{
    responsive: true,
    interaction: {{ mode: 'index', intersect: false }},
    plugins: {{
      legend: {{ position: 'bottom', labels: {{ boxWidth: 12 }} }},
      tooltip: {{ callbacks: {{ label: c => c.dataset.label + ': ' + fmt(c.parsed.y) }} }}
    }},
    scales: {{
      x: {{ grid: {{ display: false }}, ticks: {{ maxTicksLimit: 10, maxRotation: 0 }} }},
      y: {{ ticks: {{ callback: v => fmt(v) }} }}
    }}
  }}
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
  options: {{
    indexAxis: 'y',
    responsive: true,
    plugins: {{ legend: {{ position: 'bottom', labels: {{ boxWidth: 12 }} }} }},
    scales: {{ x: {{ beginAtZero: true, title: {{ display: true, text: 'horas' }} }} }}
  }}
}});
</script>
</body>
</html>
"""


def _flag_html(caps):
    flags = [
        ("has_baseline",                 "Con línea base",     "Sin línea base"),
        ("has_actuals",                  "Con avance",         "Sin avance"),
        ("has_status_date",              "Fecha de corte",     "Sin fecha de corte"),
        ("has_costs",                    "Con costos",         "Sin costos"),
        ("has_predecessors",             "Con precedencias",   "Sin precedencias"),
        ("has_deadlines",                "Con deadlines",      "Sin deadlines"),
        ("uses_physical_percent_complete","EV físico activado",""),
    ]
    out = []
    for k, on, off in flags:
        v = caps.get(k)
        if v:     out.append(f'<span class="flag">{esc(on)}</span>')
        elif off: out.append(f'<span class="flag off">{esc(off)}</span>')
    return " ".join(out)


def _spi_color(x):
    if x is None: return ""
    if x >= 0.98: return "ok"
    if x >= 0.90: return "warn"
    return "bad"


def _pct_color(x):
    if x >= 66: return "ok"
    if x >= 33: return "warn"
    return "bad"


# ---------------------------------------------------------------------------
# CSS Gantt builder
# ---------------------------------------------------------------------------

def _build_gantt_html(tasks, status_date, bl_index=0, include_summaries=True, max_rows=120):
    """Render the Gantt as a block of nested divs — one row per task,
    with a scheduled bar on top and a baseline bar below (à la MS Project's
    Tracking Gantt view), each absolutely positioned and sized by their dates.

    The time axis runs from the earliest of (scheduled start, baseline start)
    to the latest of (scheduled finish, baseline finish) across all rendered
    rows, so a baseline that's earlier or later than the current schedule
    still fits visibly inside the chart.

    Milestones render as diamonds centered on their date; summary tasks render
    as thin dark bars with notched ends; leaf tasks render as colored
    rectangles with a translucent overlay marking the % not yet completed."""
    rows = []
    for t in tasks:
        if not include_summaries and t["summary"]:
            continue
        s = parse_dt(t.get("start"))
        f = parse_dt(t.get("finish"))
        if s is None or f is None:
            continue
        rows.append(t)
    if not rows:
        return "<p style='color:var(--mute);'>No hay tareas con fechas para graficar.</p>"

    # Preserve the schedule's visual order: sort by outline number if present,
    # else by (start, id). This keeps summaries above their children.
    def _sort_key(t):
        on = t.get("outline_number") or ""
        try:
            parts = [int(p) for p in str(on).split(".") if p]
        except Exception:
            parts = []
        return (parts or [10**9], parse_dt(t["start"]), t.get("id") or 0)
    rows.sort(key=_sort_key)
    rows = rows[:max_rows]

    # The time axis has to cover both scheduled AND baseline ranges so
    # neither gets clipped when the schedule slipped a lot vs the baseline.
    all_dates = []
    for t in rows:
        s = parse_dt(t["start"]); f = parse_dt(t["finish"])
        if s: all_dates.append(s)
        if f: all_dates.append(f)
        bl = (t.get("baseline_sets") or {}).get(str(bl_index)) or (t.get("baseline_sets") or {}).get(bl_index)
        if bl:
            bs = parse_dt(bl.get("start")); bf = parse_dt(bl.get("finish"))
            if bs: all_dates.append(bs)
            if bf: all_dates.append(bf)
    start_min = min(all_dates) if all_dates else parse_dt(rows[0]["start"])
    finish_max = max(all_dates) if all_dates else parse_dt(rows[0]["finish"])
    span = (finish_max - start_min).total_seconds()
    if span <= 0:
        span = 86400  # guard against divide-by-zero (single-day project)

    # Build month ticks for the header axis
    ticks = []
    cur = datetime(start_min.year, start_min.month, 1)
    while cur <= finish_max:
        pct = max(0, (cur - start_min).total_seconds() / span * 100)
        ticks.append((cur.strftime("%b %Y"), pct))
        cur = datetime(cur.year + (1 if cur.month == 12 else 0),
                       1 if cur.month == 12 else cur.month + 1, 1)

    today_pct = max(0, min(100, (status_date - start_min).total_seconds() / span * 100))

    parts = ['<div class="gantt">']

    # Header with month labels
    parts.append('<div class="gantt-header">')
    parts.append('<div class="gantt-header-label">Tarea</div>')
    parts.append('<div class="gantt-header-axis">')
    for label, pct in ticks:
        parts.append(f'<div class="gantt-tick" style="left:{pct:.2f}%">{esc(label)}</div>')
    parts.append(f'<div class="gantt-today" style="left:{today_pct:.2f}%"></div>')
    parts.append('</div></div>')

    for t in rows:
        s = parse_dt(t["start"])
        f = parse_dt(t["finish"])
        left  = (s - start_min).total_seconds() / span * 100
        width = max(0.2, (f - s).total_seconds() / span * 100)
        pc = fnum(t.get("percent_complete"))
        is_summary   = bool(t.get("summary"))
        is_milestone = bool(t.get("milestone"))
        is_critical  = bool(t.get("critical"))

        # Determine bar variant (always combined with "gantt-bar" base class)
        if is_milestone:
            variant = "milestone"
        elif is_summary:
            variant = "summary"
        elif pc >= 100:
            variant = "done"
        elif is_critical:
            variant = "critical"
        else:
            variant = "normal"

        label_cls = "gantt-label"
        if is_summary:   label_cls += " summary"
        if is_milestone: label_cls += " milestone"
        indent = "&nbsp;" * (2 * int(t.get("outline_level") or 0))

        title_attr = (f'{esc(t["name"])} · {fdate(t["start"])} → {fdate(t["finish"])} · '
                      f'{pc:.0f}%{" · crítica" if is_critical else ""}'
                      f'{" · hito" if is_milestone else ""}')

        # Progress overlay only for regular/critical/done tasks (not summary/milestone)
        progress_html = ""
        if variant in ("normal", "critical") and 0 < pc < 100:
            progress_html = (f'<div class="gantt-progress" '
                             f'style="width:{100 - min(100,pc):.0f}%;right:0;left:auto;"></div>')

        # Milestone inline style: width handled by CSS (!important), only set left
        if is_milestone:
            bar_style = f'left:{left:.2f}%;'
        else:
            bar_style = f'left:{left:.2f}%;width:{width:.2f}%;'

        # ----- Baseline bar (below the scheduled bar) -----
        bl = (t.get("baseline_sets") or {}).get(str(bl_index)) or (t.get("baseline_sets") or {}).get(bl_index)
        baseline_html = ""
        if bl:
            bs = parse_dt(bl.get("start"))
            bf = parse_dt(bl.get("finish"))
            if bs and bf:
                bl_left  = (bs - start_min).total_seconds() / span * 100
                bl_width = max(0.2, (bf - bs).total_seconds() / span * 100)
                bl_title = (f'Línea base: {fdate(bl.get("start"))} → {fdate(bl.get("finish"))}')
                if is_milestone:
                    baseline_html = (f'<div class="gantt-baseline {variant}" '
                                     f'style="left:{bl_left:.2f}%" title="{bl_title}"></div>')
                else:
                    baseline_html = (f'<div class="gantt-baseline {variant}" '
                                     f'style="left:{bl_left:.2f}%;width:{bl_width:.2f}%" '
                                     f'title="{bl_title}"></div>')

        parts.append('<div class="gantt-row">')
        parts.append(f'<div class="{label_cls}" title="{title_attr}">{indent}{esc(t["name"])}</div>')
        parts.append('<div class="gantt-track">')
        parts.append(f'<div class="gantt-today" style="left:{today_pct:.2f}%"></div>')
        # Baseline drawn first so it sits below the scheduled bar via z-index
        parts.append(baseline_html)
        parts.append(f'<div class="gantt-bar {variant}" style="{bar_style}" title="{title_attr}">{progress_html}</div>')
        parts.append('</div></div>')

    parts.append('</div>')

    legend = (
        '<div class="gantt-legend">'
        '<span><span class="sw" style="background:var(--brand)"></span>Tarea normal</span>'
        '<span><span class="sw" style="background:var(--crit)"></span>Crítica</span>'
        '<span><span class="sw" style="background:var(--ok)"></span>Completada</span>'
        '<span><span class="sw sw-summary"></span>Resumen de fase</span>'
        '<span><span class="sw sw-milestone" style="background:var(--crit)"></span>Hito</span>'
        '<span><span class="sw sw-baseline"></span>Línea base</span>'
        '<span><span class="sw" style="background:var(--bad);width:2px;"></span>Hoy</span>'
        '</div>'
    )
    return "".join(parts) + legend


# ---------------------------------------------------------------------------
# Main renderer
# ---------------------------------------------------------------------------

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

    overall_pct = fnum(header.get("project_percent_complete"))
    if overall_pct == 0:
        total_w = sum(fnum(t.get("work_hours")) for t in tasks if not t["summary"])
        done_w = sum(fnum(t.get("work_hours")) * fnum(t.get("percent_complete")) / 100
                     for t in tasks if not t["summary"])
        overall_pct = (done_w / total_w * 100) if total_w else 0.0

    leaf = [t for t in tasks if not t["summary"]]
    n_done = sum(1 for t in leaf if fnum(t.get("percent_complete")) >= 100)
    n_inprog = sum(1 for t in leaf if 0 < fnum(t.get("percent_complete")) < 100)
    n_notyet = sum(1 for t in leaf if fnum(t.get("percent_complete")) == 0)

    status_dt = (parse_dt(header.get("status_date"))
                 or parse_dt(header.get("current_date"))
                 or datetime.now())
    bl_index = header.get("baseline_for_earned_value") or 0

    task_inputs = _task_evm_inputs(tasks, bl_index)
    evm = compute_evm_totals(task_inputs, status_dt)
    evm_curve = compute_evm_curve(task_inputs, status_dt)

    res_rows_data = resource_utilization(resources, assignments)
    risk = rank_risk_tasks(tasks, status_dt, limit=10)

    # Critical path
    crit = [t for t in leaf if t.get("critical")]
    crit.sort(key=lambda t: parse_dt(t.get("start")) or datetime.max)

    # --- HTML chunks ---
    gantt_html = _build_gantt_html(tasks, status_dt, bl_index=bl_index,
                                   include_summaries=True, max_rows=120)

    risk_rows = []
    for t in risk:
        pc = int(fnum(t.get("percent_complete")))
        risk_rows.append(
            f"<tr><td>{esc(t['id'])}</td><td>{esc(t['name'])}</td>"
            f"<td>{esc(fdate(t.get('finish')))}</td>"
            f"<td>{pc}%</td><td><span class='pill bad'>{esc(t['_reason'])}</span></td></tr>"
        )
    if not risk_rows:
        risk_rows.append("<tr><td colspan='5' style='color:#16a34a;'>Sin tareas en riesgo detectadas</td></tr>")

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

    res_table_rows = []
    for r in res_rows_data:
        res_table_rows.append(
            f"<tr><td>{esc(r['name'])}</td>"
            f"<td style='text-align:right;'>{r['planned']:,.1f}</td>"
            f"<td style='text-align:right;'>{r['actual']:,.1f}</td>"
            f"<td style='text-align:right;'>{esc(cur)}{r['cost']:,.0f}</td></tr>"
        )
    if not res_table_rows:
        res_table_rows.append("<tr><td colspan='4' style='color:var(--mute);'>Sin recursos de trabajo con asignaciones</td></tr>")

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
        cpi_str=_fmt_idx(evm["cpi"]), cpi_color=_spi_color(evm["cpi"]),
        sv_str=_fmt_money(evm["sv"], cur), cv_str=_fmt_money(evm["cv"], cur),
        vac_str=_fmt_money(evm["vac"], cur),
        vac_color="ok" if (evm["vac"] or 0) >= 0 else "bad",
        eac_str=_fmt_money(evm["eac"], cur),
        bac_str=_fmt_money(evm["bac"], cur),
        bcws_str=_fmt_money(evm["bcws"], cur),
        bcwp_str=_fmt_money(evm["bcwp"], cur),
        acwp_str=_fmt_money(evm["acwp"], cur),
        gantt_html=gantt_html,
        evm_curve=json.dumps(evm_curve, ensure_ascii=False),
        res_data=json.dumps({
            "names":   [r["name"]    for r in res_rows_data],
            "planned": [r["planned"] for r in res_rows_data],
            "actual":  [r["actual"]  for r in res_rows_data],
        }, ensure_ascii=False),
        res_h=max(160, len(res_rows_data) * 38),
        currency_js=json.dumps(cur),
        risk_rows="\n".join(risk_rows),
        critical_rows="\n".join(critical_rows),
        res_rows="\n".join(res_table_rows),
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
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "bundle"
            r = subprocess.run(
                ["python3", str(EXTRACT), str(inp), "--out", str(bundle)],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                print(r.stderr, file=sys.stderr); sys.exit(1)
            out = render(bundle, Path(args.out).expanduser().resolve(), args.title)
    elif inp.is_dir():
        out = render(inp, Path(args.out).expanduser().resolve(), args.title)
    else:
        print(f"[ERROR] not a file or directory: {inp}", file=sys.stderr); sys.exit(1)
    print(f"[mpp-reader] Wrote {out} ({out.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
