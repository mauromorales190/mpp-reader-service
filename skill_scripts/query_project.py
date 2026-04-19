#!/usr/bin/env python3
"""
query_project.py — Canned analytical queries against an extracted project bundle.

Reads the JSON files produced by extract_project.py and answers common PM questions
the way PMI/PMBOK defines them. Emits a human-readable table AND the raw JSON (on
stderr-like section) so answers can be composed into larger reports.

Usage:
    python3 query_project.py <bundle-dir> <query> [options]

Queries:
    status          Overall %complete, dates, behind-schedule tasks
    critical        Critical path (Total Slack ≤ Critical Slack Limit)
    network         Predecessor/successor table
    overdue         Tasks whose finish < status-date (or today) and %<100
    upcoming        Tasks starting in the next N days (default 14)
    slack           Distribution of Total Slack / Free Slack
    evm             BCWS/BCWP/ACWP + CV/SV/CPI/SPI/EAC/ETC/VAC/TCPI
    baseline        Variance vs baseline (dates, work, cost)
    resources       Resource utilization and over-allocation check
    customfields    All populated custom fields, grouped by alias
    calendars       Calendars and exceptions
    find            Filter tasks by name (--name) or WBS (--wbs)
    summary-tree    Outline / WBS tree with %complete roll-up
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_bundle(bundle_dir):
    b = Path(bundle_dir)
    project = json.loads((b / "project.json").read_text())
    tasks = json.loads((b / "tasks.json").read_text())
    resources = json.loads((b / "resources.json").read_text())
    assignments = json.loads((b / "assignments.json").read_text())
    return project, tasks, resources, assignments


def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", ""))
    except Exception:
        return None


def parse_cost(c):
    if c is None:
        return 0.0
    try:
        return float(c)
    except Exception:
        try:
            # "$1,234.56" or "1234.56/h"
            s = str(c).replace("$", "").replace(",", "").split("/")[0].strip()
            return float(s)
        except Exception:
            return 0.0


def parse_num(s):
    if s is None:
        return 0.0
    try:
        return float(s)
    except Exception:
        try:
            return float(str(s).split()[0])
        except Exception:
            return 0.0


def fmt_date(s):
    if not s:
        return ""
    try:
        return parse_dt(s).strftime("%Y-%m-%d")
    except Exception:
        return str(s)


def table(rows, cols, max_widths=None, limit=None):
    """Pretty-print a table from list-of-dict."""
    max_widths = max_widths or {}
    if limit:
        rows = rows[:limit]
    if not rows:
        print("(no rows)")
        return
    widths = {c: max(len(c), *(len(str(r.get(c, "") or "")) for r in rows)) for c in cols}
    for c, mw in max_widths.items():
        widths[c] = min(widths[c], mw)
    line = " | ".join(c.ljust(widths[c]) for c in cols)
    print(line)
    print("-+-".join("-" * widths[c] for c in cols))
    for r in rows:
        vals = []
        for c in cols:
            v = str(r.get(c, "") or "")
            if len(v) > widths[c]:
                v = v[: widths[c] - 1] + "…"
            vals.append(v.ljust(widths[c]))
        print(" | ".join(vals))


# ---------------------------------------------------------------------------
# Query implementations
# ---------------------------------------------------------------------------

def _status_date(header):
    """Status date with fallback to current_date, then today."""
    return (parse_dt(header.get("status_date"))
            or parse_dt(header.get("current_date"))
            or datetime.now())


def q_status(project, tasks, resources, assignments, args):
    header = project["header"]
    caps = project["capabilities"]
    status_date = _status_date(header)

    real_tasks = [t for t in tasks if not t["summary"]]
    done = [t for t in real_tasks if parse_num(t.get("percent_complete")) >= 100]
    inprog = [t for t in real_tasks if 0 < parse_num(t.get("percent_complete")) < 100]
    notyet = [t for t in real_tasks if parse_num(t.get("percent_complete")) == 0]

    # Prefer the roll-up that MS Project already stored on the project summary (ID=0)
    overall = parse_num(header.get("project_percent_complete"))
    work_overall = parse_num(header.get("project_percent_work_complete"))
    source = "project summary (roll-up by MS Project)"
    if overall == 0 and work_overall == 0:
        # Fall back to a weighted recalculation
        total_w = sum(parse_num(t.get("work_hours")) for t in real_tasks)
        done_w = sum(parse_num(t.get("work_hours")) * parse_num(t.get("percent_complete")) / 100 for t in real_tasks)
        overall = (done_w / total_w * 100) if total_w else 0.0
        work_overall = overall
        source = "weighted by work (recomputed)"

    print(f"Project:      {header.get('title') or header.get('name') or '(untitled)'}")
    print(f"Dates:        {fmt_date(header.get('start_date'))} → {fmt_date(header.get('finish_date'))}")
    print(f"Status date:  {fmt_date(header.get('status_date')) or '(not set — using current date ' + fmt_date(header.get('current_date')) + ')'}")
    print(f"Overall:      {overall:5.1f}% complete  /  {work_overall:5.1f}% work complete  [{source}]")
    print(f"              {len(done)} done, {len(inprog)} in-progress, {len(notyet)} not-started "
          f"(of {len(real_tasks)} non-summary tasks)")
    if header.get("project_cost") is not None:
        print(f"Cost:         actual {header.get('project_actual_cost')} / plan {header.get('project_cost')} "
              f"/ baseline {header.get('project_baseline_cost')}")
    if header.get("project_work_hours") is not None:
        print(f"Work (h):     actual {header.get('project_actual_work_hours')} / plan {header.get('project_work_hours')} "
              f"/ baseline {header.get('project_baseline_work_hours')}")

    if not caps["has_actuals"]:
        print("\n[!] No actuals recorded yet — progress metrics will be 0.")
        return

    # Tasks most behind schedule: finish < status_date and %complete < 100
    behind = [
        {
            "id": t["id"], "wbs": t["wbs"], "name": t["name"],
            "finish": fmt_date(t["finish"]),
            "%": f"{parse_num(t['percent_complete']):.0f}",
            "slack": t.get("total_slack"),
        }
        for t in real_tasks
        if parse_dt(t["finish"]) and parse_dt(t["finish"]) < status_date
           and parse_num(t["percent_complete"]) < 100
    ]
    behind.sort(key=lambda r: parse_dt(r["finish"]) or datetime.max)
    if behind:
        print(f"\nTasks behind schedule (finish before {status_date:%Y-%m-%d} and <100%):")
        table(behind, ["id", "wbs", "name", "finish", "%", "slack"],
              max_widths={"name": 50}, limit=args.limit)


def q_critical(project, tasks, resources, assignments, args):
    crit = [t for t in tasks if t.get("critical") and not t.get("summary")]
    crit.sort(key=lambda t: (parse_dt(t.get("start")) or datetime.max, t.get("id") or 0))
    rows = [
        {"id": t["id"], "wbs": t["wbs"], "name": t["name"],
         "duration": t.get("duration"), "start": fmt_date(t["start"]),
         "finish": fmt_date(t["finish"]),
         "slack": t.get("total_slack"),
         "%": parse_num(t.get("percent_complete"))}
        for t in crit
    ]
    print(f"Critical path: {len(rows)} tasks")
    table(rows, ["id", "wbs", "name", "duration", "start", "finish", "slack", "%"],
          max_widths={"name": 45}, limit=args.limit)


def q_network(project, tasks, resources, assignments, args):
    by_uid = {t["uid"]: t for t in tasks}
    rows = []
    for t in tasks:
        if t["summary"]:
            continue
        preds = t.get("predecessors") or []
        pred_str = ", ".join(
            f"{by_uid.get(p['predecessor_uid'], {}).get('id', '?')}{str(p['type']).replace('_','')}"
            + (f"+{p['lag']}" if p.get("lag") and p["lag"] != "0.0d" else "")
            for p in preds
        )
        rows.append({"id": t["id"], "wbs": t["wbs"], "name": t["name"],
                     "start": fmt_date(t["start"]), "finish": fmt_date(t["finish"]),
                     "predecessors": pred_str or "(none)"})
    table(rows, ["id", "wbs", "name", "start", "finish", "predecessors"],
          max_widths={"name": 40, "predecessors": 40}, limit=args.limit)


def q_overdue(project, tasks, resources, assignments, args):
    sd = parse_dt(project["header"].get("status_date")) or datetime.now()
    rows = []
    for t in tasks:
        if t["summary"]:
            continue
        finish = parse_dt(t["finish"])
        pc = parse_num(t.get("percent_complete"))
        if finish and finish < sd and pc < 100:
            rows.append({
                "id": t["id"], "wbs": t["wbs"], "name": t["name"],
                "finish": fmt_date(t["finish"]),
                "days_late": (sd - finish).days,
                "%": f"{pc:.0f}",
            })
    rows.sort(key=lambda r: -r["days_late"])
    print(f"Overdue tasks as of {sd:%Y-%m-%d}:")
    table(rows, ["id", "wbs", "name", "finish", "days_late", "%"],
          max_widths={"name": 45}, limit=args.limit)


def q_upcoming(project, tasks, resources, assignments, args):
    sd = parse_dt(project["header"].get("status_date")) or datetime.now()
    end = sd + timedelta(days=args.days)
    rows = []
    for t in tasks:
        if t["summary"]:
            continue
        s = parse_dt(t["start"])
        if s and sd <= s <= end:
            rows.append({"id": t["id"], "wbs": t["wbs"], "name": t["name"],
                         "start": fmt_date(t["start"]), "finish": fmt_date(t["finish"]),
                         "duration": t.get("duration"),
                         "%": parse_num(t.get("percent_complete"))})
    rows.sort(key=lambda r: parse_dt(r["start"]))
    print(f"Tasks starting in the next {args.days} days (from {sd:%Y-%m-%d}):")
    table(rows, ["id", "wbs", "name", "start", "finish", "duration", "%"],
          max_widths={"name": 45}, limit=args.limit)


def q_slack(project, tasks, resources, assignments, args):
    buckets = defaultdict(int)
    for t in tasks:
        if t["summary"]:
            continue
        s = str(t.get("total_slack") or "0.0d")
        try:
            d = float(s.rstrip("d").rstrip("h").rstrip("w"))
        except Exception:
            d = 0
        if "h" in s:
            d = d / 8
        elif "w" in s:
            d = d * 5
        if d == 0: key = "=0 (critical)"
        elif d <= 3: key = "1-3 days"
        elif d <= 7: key = "4-7 days"
        elif d <= 20: key = "8-20 days"
        else: key = ">20 days"
        buckets[key] += 1
    order = ["=0 (critical)", "1-3 days", "4-7 days", "8-20 days", ">20 days"]
    print("Total Slack distribution:")
    for k in order:
        print(f"  {k:20s}  {buckets.get(k, 0)}")


def _task_evm(t, status_date, bl_index):
    """Compute BCWS/BCWP/ACWP for one task, using the chosen baseline set and EV method.

    Returns (bac, bcws, bcwp, acwp) as floats. Uses linear accrual across baseline
    dates, which is the same approximation MS Project defaults to when the
    detailed earned-value fields aren't pre-computed on the file.
    """
    bl = (t.get("baseline_sets") or {}).get(str(bl_index)) or (t.get("baseline_sets") or {}).get(bl_index)
    if not bl:
        return 0.0, 0.0, 0.0, parse_cost(t.get("actual_cost"))

    bac = parse_cost(bl.get("cost"))
    bs = parse_dt(bl.get("start"))
    bf = parse_dt(bl.get("finish"))

    # Planned Value (BCWS): portion of baseline cost scheduled by status date
    if bac == 0 or not bs or not bf:
        bcws = 0.0
    elif status_date >= bf:
        bcws = bac
    elif status_date <= bs:
        bcws = 0.0
    else:
        span = (bf - bs).total_seconds()
        elapsed = (status_date - bs).total_seconds()
        bcws = bac * (elapsed / span) if span > 0 else 0.0

    # Earned Value (BCWP): baseline cost × completion % (depending on EV method)
    method = str(t.get("earned_value_method", "")).upper()
    if method == "PHYSICAL_PERCENT_COMPLETE":
        pc = parse_num(t.get("physical_percent_complete"))
    else:
        pc = parse_num(t.get("percent_complete"))
    bcwp = bac * pc / 100.0

    # Actual Cost: straight from the task
    acwp = parse_cost(t.get("actual_cost"))

    return bac, bcws, bcwp, acwp


def q_evm(project, tasks, resources, assignments, args):
    caps = project["capabilities"]
    header = project["header"]
    if not caps.get("has_baseline") or not caps.get("has_actuals"):
        print("[!] Earned value needs both a baseline and actuals.")
        print(f"    has_baseline = {caps.get('has_baseline')}")
        print(f"    has_actuals  = {caps.get('has_actuals')}")
        print("    → In MS Project: Project → Set Baseline, record %complete, re-export.")
        return

    status_date = _status_date(header)
    bl_index = header.get("baseline_for_earned_value") or 0

    # Compute leaf-level EVM
    leaf = [t for t in tasks if not t["summary"]]
    rows = []
    tot_bac = tot_bcws = tot_bcwp = tot_acwp = 0.0
    for t in leaf:
        bac, bcws, bcwp, acwp = _task_evm(t, status_date, bl_index)
        tot_bac  += bac; tot_bcws += bcws; tot_bcwp += bcwp; tot_acwp += acwp
        if bac == 0 and bcws == 0 and bcwp == 0 and acwp == 0:
            continue
        rows.append({
            "id": t["id"], "name": t["name"],
            "method": str(t.get("earned_value_method") or "").replace("_PERCENT_COMPLETE", ""),
            "%": f"{parse_num(t.get('percent_complete')):.0f}",
            "phys%": f"{parse_num(t.get('physical_percent_complete')):.0f}",
            "BAC": f"{bac:,.0f}",
            "BCWS": f"{bcws:,.0f}",
            "BCWP": f"{bcwp:,.0f}",
            "ACWP": f"{acwp:,.0f}",
            "CV": f"{bcwp - acwp:,.0f}",
            "SV": f"{bcwp - bcws:,.0f}",
        })

    cv  = tot_bcwp - tot_acwp
    sv  = tot_bcwp - tot_bcws
    cpi = (tot_bcwp / tot_acwp) if tot_acwp else None
    spi = (tot_bcwp / tot_bcws) if tot_bcws else None
    eac_cpi = (tot_bac / cpi) if cpi else None
    etc = (eac_cpi - tot_acwp) if eac_cpi is not None else None
    vac = (tot_bac - eac_cpi) if eac_cpi is not None else None
    rem_denom = (tot_bac - tot_acwp)
    tcpi = ((tot_bac - tot_bcwp) / rem_denom) if rem_denom else None
    cur = header.get("currency_symbol") or ""

    print(f"Baseline used:       Baseline{bl_index if bl_index else ''} "
          f"(file setting: BaselineForEarnedValue = {bl_index})")
    print(f"Status date:         {status_date:%Y-%m-%d} "
          f"{'(from file)' if header.get('status_date') else '(fallback: current_date)'}")
    print(f"Currency:            {cur} ({header.get('currency_code') or ''})")
    print(f"EV methods present:  " + ", ".join(sorted({str(t.get('earned_value_method') or 'PERCENT_COMPLETE') for t in leaf})))
    print()
    print(f"BAC   (Budget At Completion)         = {cur}{tot_bac:,.2f}")
    print(f"BCWS  (Planned Value / PV)           = {cur}{tot_bcws:,.2f}")
    print(f"BCWP  (Earned Value / EV)            = {cur}{tot_bcwp:,.2f}")
    print(f"ACWP  (Actual Cost / AC)             = {cur}{tot_acwp:,.2f}")
    print(f"CV    (Cost Variance = EV − AC)      = {cur}{cv:,.2f}")
    print(f"SV    (Schedule Variance = EV − PV)  = {cur}{sv:,.2f}")
    print(f"CPI   (Cost Perf. Index = EV / AC)   = {cpi:.3f}" if cpi is not None else "CPI   = n/a")
    print(f"SPI   (Sched. Perf. Index = EV / PV) = {spi:.3f}" if spi is not None else "SPI   = n/a")
    print(f"EAC   (Estimate At Completion, /CPI) = {cur}{eac_cpi:,.2f}" if eac_cpi is not None else "EAC   = n/a")
    print(f"ETC   (Estimate To Complete)         = {cur}{etc:,.2f}" if etc is not None else "ETC   = n/a")
    print(f"VAC   (Variance At Completion)       = {cur}{vac:,.2f}" if vac is not None else "VAC   = n/a")
    print(f"TCPI  (To-Complete Perf. Index)      = {tcpi:.3f}" if tcpi is not None else "TCPI  = n/a")

    if rows and args.limit and args.limit > 0:
        print(f"\nPer-task EVM breakdown (leaf tasks only, top {args.limit}):")
        rows.sort(key=lambda r: float(r["BAC"].replace(",", "")), reverse=True)
        table(rows, ["id", "name", "method", "%", "phys%", "BAC", "BCWS", "BCWP", "ACWP", "CV", "SV"],
              max_widths={"name": 30}, limit=args.limit)


def q_baseline(project, tasks, resources, assignments, args):
    caps = project["capabilities"]
    header = project["header"]
    if not caps["has_baseline"]:
        print("[!] No baseline saved on any task. Run 'Set Baseline' in MS Project first.")
        return

    # Which baseline sets are populated?
    sets_found = sorted({int(k) for t in tasks for k in (t.get("baseline_sets") or {}).keys()})
    bl_index = header.get("baseline_for_earned_value") or (sets_found[0] if sets_found else 0)
    print(f"Baselines in file: " + (", ".join(f"Baseline{k or ''}" for k in sets_found) or "none"))
    print(f"Using for variance: Baseline{bl_index if bl_index else ''}")
    print()

    rows = []
    for t in tasks:
        if t["summary"]:
            continue
        bl = (t.get("baseline_sets") or {}).get(str(bl_index)) or (t.get("baseline_sets") or {}).get(bl_index)
        if not bl:
            continue
        bs = parse_dt(bl.get("start"))
        bf = parse_dt(bl.get("finish"))
        s = parse_dt(t.get("start"))
        f = parse_dt(t.get("finish"))
        bc = parse_cost(bl.get("cost"))
        cc = parse_cost(t.get("cost"))
        rows.append({
            "id": t["id"], "name": t["name"],
            "bl_start":  fmt_date(bl.get("start")),
            "start":     fmt_date(t["start"]),
            "start_Δ":   (s - bs).days if s and bs else "",
            "bl_finish": fmt_date(bl.get("finish")),
            "finish":    fmt_date(t["finish"]),
            "finish_Δ":  (f - bf).days if f and bf else "",
            "bl_cost":   f"{bc:,.0f}" if bc else "",
            "cost":      f"{cc:,.0f}" if cc else "",
            "cost_Δ":    f"{cc - bc:,.0f}" if (cc or bc) else "",
        })
    rows.sort(key=lambda r: abs(r["finish_Δ"]) if isinstance(r["finish_Δ"], int) else 0, reverse=True)
    print("Variance vs baseline (Δ = current − baseline; days and currency):")
    table(rows,
          ["id", "name", "bl_start", "start", "start_Δ", "bl_finish", "finish", "finish_Δ",
           "bl_cost", "cost", "cost_Δ"],
          max_widths={"name": 35}, limit=args.limit)


def q_resources(project, tasks, resources, assignments, args):
    rows = []
    for r in resources:
        rows.append({
            "id": r["id"], "name": r["name"], "type": r["type"],
            "max": r.get("max_units"),
            "std_rate": r.get("standard_rate"),
            "work_h": r.get("work_hours"),
            "actual_h": r.get("actual_work_hours"),
            "cost": r.get("cost"),
            "peak": r.get("peak_units"),
            "over_alloc": "YES" if r.get("peak_units") and parse_num(r["peak_units"]) > parse_num(r.get("max_units") or "100") else "",
        })
    table(rows, ["id", "name", "type", "max", "std_rate", "work_h", "actual_h", "cost", "peak", "over_alloc"],
          max_widths={"name": 30}, limit=args.limit)


def q_customfields(project, tasks, resources, assignments, args):
    defs = project.get("custom_fields") or {}
    if defs:
        print("Aliased custom fields (defined):")
        for ft, info in defs.items():
            print(f"  {ft:20s} → '{info.get('alias')}'")
        print()
    else:
        print("(no aliased custom fields defined — showing raw field names below)\n")

    # Show populated custom fields per task
    populated = defaultdict(int)
    for t in tasks:
        for k in (t.get("custom_fields") or {}):
            populated[k] += 1
    if not populated:
        print("No custom fields are populated on any task.")
        return
    print("Populated task custom fields (field → count of tasks):")
    for k, n in sorted(populated.items(), key=lambda x: -x[1]):
        alias = defs.get(f"TASK_{k}", {}).get("alias", "")
        print(f"  {k:20s} {('(' + alias + ')') if alias else '':20s} {n} tasks")


def q_calendars(project, tasks, resources, assignments, args):
    for c in project["calendars"]:
        print(f"• {c['name']}  (UID={c['uid']}, base={c['parent']})")
        if c["exceptions"]:
            for e in c["exceptions"]:
                print(f"    — {e['name']}: {e['from']} → {e['to']} (working={e['working']})")


def q_find(project, tasks, resources, assignments, args):
    needle_name = (args.name or "").lower()
    needle_wbs = args.wbs or ""
    rows = []
    for t in tasks:
        if needle_name and needle_name not in (t.get("name") or "").lower():
            continue
        if needle_wbs and str(t.get("wbs") or "") != needle_wbs:
            continue
        rows.append({
            "id": t["id"], "wbs": t["wbs"], "name": t["name"],
            "start": fmt_date(t["start"]), "finish": fmt_date(t["finish"]),
            "duration": t.get("duration"),
            "%": parse_num(t.get("percent_complete")),
            "critical": t.get("critical"),
        })
    table(rows, ["id", "wbs", "name", "start", "finish", "duration", "%", "critical"],
          max_widths={"name": 50}, limit=args.limit)


def q_summary_tree(project, tasks, resources, assignments, args):
    for t in tasks:
        indent = "  " * (t.get("outline_level") or 0)
        pc = parse_num(t.get("percent_complete"))
        flag = "●" if t["summary"] else ("◆" if t["milestone"] else "○")
        print(f"{indent}{flag} {t['id']:>3}  {t.get('name')}  [{pc:.0f}%  {t.get('duration')}]")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

DISPATCH = {
    "status": q_status,
    "critical": q_critical,
    "network": q_network,
    "overdue": q_overdue,
    "upcoming": q_upcoming,
    "slack": q_slack,
    "evm": q_evm,
    "baseline": q_baseline,
    "resources": q_resources,
    "customfields": q_customfields,
    "calendars": q_calendars,
    "find": q_find,
    "summary-tree": q_summary_tree,
}


def main():
    ap = argparse.ArgumentParser(description="Query an extracted MS Project bundle.")
    ap.add_argument("bundle", help="Directory produced by extract_project.py")
    ap.add_argument("query", choices=list(DISPATCH))
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--days", type=int, default=14, help="(upcoming) window size")
    ap.add_argument("--name", default=None, help="(find) name substring")
    ap.add_argument("--wbs", default=None, help="(find) exact WBS")
    args = ap.parse_args()

    project, tasks, resources, assignments = load_bundle(args.bundle)
    DISPATCH[args.query](project, tasks, resources, assignments, args)


if __name__ == "__main__":
    main()
