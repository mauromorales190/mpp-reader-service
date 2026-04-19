#!/usr/bin/env python3
"""
extract_project.py — Read any MS Project / Primavera schedule file and dump a
complete, query-friendly snapshot to JSON + CSV.

Supports: .mpp (MPP8/9/12/14), .mpx, .xml (MSP & P6), .mpd, .xer, .pmxml, etc.
Backed by MPXJ (https://www.mpxj.org/) via the `mpxj` PyPI package.

Usage:
    python3 extract_project.py <input-file> --out <workspace-dir>

Output bundle in <workspace-dir>:
    project.json       header, calendars, custom fields, capability flags
    tasks.json         all tasks with ~70 fields + predecessors
    resources.json     all resources with rates & totals
    assignments.json   task↔resource assignments
    tasks.csv / resources.csv / assignments.csv   flat views for pivoting

Exit code 0 on success; non-zero on failure.
"""

import argparse
import csv
import json
import os
import sys
from datetime import date, datetime, time
from pathlib import Path

# ---------------------------------------------------------------------------
# JPype / MPXJ bootstrap
# ---------------------------------------------------------------------------

def _start_jvm():
    try:
        import mpxj  # noqa: F401
    except ImportError:
        sys.stderr.write(
            "[mpp-reader] The 'mpxj' Python package is missing.\n"
            "Install with: pip install mpxj jpype1\n"
        )
        sys.exit(2)

    import jpype
    if not jpype.isJVMStarted():
        import mpxj
        mpxj.startJVM()
    return jpype


# ---------------------------------------------------------------------------
# Helpers: safe JSON conversion for Java objects
# ---------------------------------------------------------------------------

def j2s(val):
    """Best-effort conversion of a Java/MPXJ value to a JSON-serializable Python value."""
    if val is None:
        return None
    if isinstance(val, (bool, int, float, str)):
        return val
    if isinstance(val, (datetime, date, time)):
        return val.isoformat()
    # jpype primitives
    cls = type(val).__name__
    if cls in ("bool", "int", "float", "str"):
        return val
    # Try common MPXJ types
    s = str(val)
    if s in ("null", "None"):
        return None
    # Duration / Rate / Number types all have a sensible str()
    return s


_PROJECT_PROPS = None  # set at start of main() so duration conversions use real project defaults


def dur_minutes(d):
    """Return a Duration as minutes (float) or None, respecting the project's min/day + min/week."""
    if d is None:
        return None
    try:
        from org.mpxj import TimeUnit  # type: ignore
        # convertUnits needs a ProjectProperties so MPXJ knows how many minutes are in a "day" / "week"
        return float(d.convertUnits(TimeUnit.MINUTES, _PROJECT_PROPS).getDuration())
    except Exception:
        # Fallback: parse the string representation "8.0d" / "72.0h" / "30.0m" / "2.0w"
        try:
            s = str(d).strip().lower()
            num = float("".join(ch for ch in s if ch.isdigit() or ch in ".-"))
            if s.endswith("m"):   return num
            if s.endswith("h"):   return num * 60
            if s.endswith("d"):   return num * (float(_PROJECT_PROPS.getMinutesPerDay()) if _PROJECT_PROPS else 480)
            if s.endswith("w"):   return num * (float(_PROJECT_PROPS.getMinutesPerWeek()) if _PROJECT_PROPS else 2400)
            if s.endswith("mo"):  return num * 20 * 480
        except Exception:
            return None
    return None


def dur_hours(d):
    m = dur_minutes(d)
    return round(m / 60.0, 4) if m is not None else None


def dur_days(d):
    m = dur_minutes(d)
    if m is None:
        return None
    mpd = float(_PROJECT_PROPS.getMinutesPerDay()) if _PROJECT_PROPS else 480.0
    return round(m / mpd, 4) if mpd else None


def _call(obj, *names):
    """Call the first available method name on an object; return None if none work."""
    for n in names:
        fn = getattr(obj, n, None)
        if fn is None:
            continue
        try:
            return fn()
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Extractors
# ---------------------------------------------------------------------------

TASK_NUMBER_FIELDS = [
    *[f"TEXT{i}" for i in range(1, 31)],
    *[f"NUMBER{i}" for i in range(1, 21)],
    *[f"COST{i}" for i in range(1, 11)],
    *[f"DATE{i}" for i in range(1, 11)],
    *[f"FLAG{i}" for i in range(1, 21)],
    *[f"DURATION{i}" for i in range(1, 11)],
    *[f"START{i}" for i in range(1, 11)],
    *[f"FINISH{i}" for i in range(1, 11)],
    *[f"OUTLINE_CODE{i}" for i in range(1, 11)],
]

TASK_BASELINE_FIELDS = [
    "BASELINE_START", "BASELINE_FINISH", "BASELINE_DURATION",
    "BASELINE_COST", "BASELINE_WORK",
    *[f"BASELINE{i}_START" for i in range(1, 11)],
    *[f"BASELINE{i}_FINISH" for i in range(1, 11)],
    *[f"BASELINE{i}_DURATION" for i in range(1, 11)],
    *[f"BASELINE{i}_COST" for i in range(1, 11)],
    *[f"BASELINE{i}_WORK" for i in range(1, 11)],
]

RESOURCE_NUMBER_FIELDS = [
    *[f"TEXT{i}" for i in range(1, 31)],
    *[f"NUMBER{i}" for i in range(1, 21)],
    *[f"COST{i}" for i in range(1, 11)],
    *[f"DATE{i}" for i in range(1, 11)],
    *[f"FLAG{i}" for i in range(1, 21)],
    *[f"OUTLINE_CODE{i}" for i in range(1, 11)],
]


def read_field(obj, field_enum, field_name):
    """Read a field value by name from a Task/Resource/Assignment via TaskField/ResourceField."""
    try:
        f = getattr(field_enum, field_name, None)
        if f is None:
            return None
        return j2s(obj.get(f))
    except Exception:
        return None


def _relation_list(relations):
    out = []
    for rel in (relations or []):
        try:
            other_pred = _call(rel, "getPredecessorTask")
            other_succ = _call(rel, "getSuccessorTask")
            out.append({
                "predecessor_id":  _call(other_pred, "getID"),
                "predecessor_uid": _call(other_pred, "getUniqueID"),
                "successor_id":    _call(other_succ, "getID"),
                "successor_uid":   _call(other_succ, "getUniqueID"),
                "type": str(_call(rel, "getType") or ""),
                "lag":  j2s(_call(rel, "getLag")),
                "lag_hours": dur_hours(_call(rel, "getLag")),
            })
        except Exception:
            pass
    return out


def _task_baselines(t):
    """Return dict of populated baseline sets {0..10: {start, finish, duration, cost, work}}."""
    out = {}
    for i in range(0, 11):
        try:
            if i == 0:
                bs = _call(t, "getBaselineStart")
                bf = _call(t, "getBaselineFinish")
                bd = _call(t, "getBaselineDuration")
                bc = _call(t, "getBaselineCost")
                bw = _call(t, "getBaselineWork")
            else:
                bs = t.getBaselineStart(i)
                bf = t.getBaselineFinish(i)
                bd = t.getBaselineDuration(i)
                bc = t.getBaselineCost(i)
                bw = t.getBaselineWork(i)
            populated = (bs is not None) or (bc is not None and float(str(bc) or 0) != 0)
            if populated:
                out[i] = {
                    "start": j2s(bs), "finish": j2s(bf),
                    "duration": j2s(bd), "duration_hours": dur_hours(bd),
                    "cost": j2s(bc), "work": j2s(bw), "work_hours": dur_hours(bw),
                }
        except Exception:
            continue
    return out


def extract_task(t, TaskField):
    preds = _relation_list(_call(t, "getPredecessors"))
    succs = _relation_list(_call(t, "getSuccessors"))

    base = {
        "id": _call(t, "getID"),
        "uid": _call(t, "getUniqueID"),
        "name": _call(t, "getName"),
        "wbs": _call(t, "getWBS"),
        "outline_number": _call(t, "getOutlineNumber"),
        "outline_level": _call(t, "getOutlineLevel"),
        "summary": bool(_call(t, "getSummary")),
        "milestone": bool(_call(t, "getMilestone")),
        "critical": bool(_call(t, "getCritical")),
        "active": bool(_call(t, "getActive")) if _call(t, "getActive") is not None else True,
        "rollup": bool(_call(t, "getRollup")) if _call(t, "getRollup") is not None else None,
        "hide_bar": bool(_call(t, "getHideBar")) if _call(t, "getHideBar") is not None else None,
        "notes": _call(t, "getNotesObject") and str(_call(t, "getNotesObject")) or _call(t, "getNotes"),
        # Schedule
        "start": j2s(_call(t, "getStart")),
        "finish": j2s(_call(t, "getFinish")),
        "early_start": j2s(_call(t, "getEarlyStart")),
        "early_finish": j2s(_call(t, "getEarlyFinish")),
        "late_start": j2s(_call(t, "getLateStart")),
        "late_finish": j2s(_call(t, "getLateFinish")),
        "duration": j2s(_call(t, "getDuration")),
        "duration_hours": dur_hours(_call(t, "getDuration")),
        "duration_days": dur_days(_call(t, "getDuration")),
        "total_slack": j2s(_call(t, "getTotalSlack")),
        "total_slack_hours": dur_hours(_call(t, "getTotalSlack")),
        "free_slack": j2s(_call(t, "getFreeSlack")),
        "free_slack_hours": dur_hours(_call(t, "getFreeSlack")),
        "constraint_type": str(_call(t, "getConstraintType") or ""),
        "constraint_date": j2s(_call(t, "getConstraintDate")),
        "deadline": j2s(_call(t, "getDeadline")),
        # Progress
        "percent_complete": j2s(_call(t, "getPercentageComplete")),
        "percent_work_complete": j2s(_call(t, "getPercentageWorkComplete")),
        "physical_percent_complete": j2s(_call(t, "getPhysicalPercentComplete")),
        "actual_start": j2s(_call(t, "getActualStart")),
        "actual_finish": j2s(_call(t, "getActualFinish")),
        "actual_duration": j2s(_call(t, "getActualDuration")),
        "remaining_duration": j2s(_call(t, "getRemainingDuration")),
        # Work
        "work": j2s(_call(t, "getWork")),
        "work_hours": dur_hours(_call(t, "getWork")),
        "actual_work": j2s(_call(t, "getActualWork")),
        "actual_work_hours": dur_hours(_call(t, "getActualWork")),
        "remaining_work": j2s(_call(t, "getRemainingWork")),
        "remaining_work_hours": dur_hours(_call(t, "getRemainingWork")),
        "overtime_work": j2s(_call(t, "getOvertimeWork")),
        # Cost
        "cost": j2s(_call(t, "getCost")),
        "actual_cost": j2s(_call(t, "getActualCost")),
        "remaining_cost": j2s(_call(t, "getRemainingCost")),
        "fixed_cost": j2s(_call(t, "getFixedCost")),
        "fixed_cost_accrual": str(_call(t, "getFixedCostAccrual") or ""),
        # Earned value (task-level)
        "bcws": j2s(_call(t, "getBCWS")),
        "bcwp": j2s(_call(t, "getBCWP")),
        "acwp": j2s(_call(t, "getACWP")),
        "cv": j2s(_call(t, "getCV")),
        "sv": j2s(_call(t, "getSV")),
        "earned_value_method": str(_call(t, "getEarnedValueMethod") or ""),
        # Scheduling attributes
        "priority": j2s(_call(t, "getPriority")),
        "type": str(_call(t, "getType") or ""),
        "effort_driven": bool(_call(t, "getEffortDriven")) if _call(t, "getEffortDriven") is not None else None,
        "calendar": _call(_call(t, "getCalendar"), "getName") if _call(t, "getCalendar") else None,
        "resource_names": _call(t, "getResourceNames"),
        "resource_initials": _call(t, "getResourceInitials"),
        # Relations
        "predecessors": preds,
        "successors": succs,
        # Baseline sets {0..10: {start, finish, duration, cost, work}} — populated only
        "baseline_sets": _task_baselines(t),
        # Custom fields (only keep populated ones)
        "custom_fields": {f: read_field(t, TaskField, f) for f in TASK_NUMBER_FIELDS
                          if read_field(t, TaskField, f) not in (None, "", "0.0", "0.0h", "0.0d", "0", 0.0, 0, False)},
    }
    return base


def extract_resource(r, ResourceField):
    return {
        "id": _call(r, "getID"),
        "uid": _call(r, "getUniqueID"),
        "name": _call(r, "getName"),
        "initials": _call(r, "getInitials"),
        "type": str(_call(r, "getType") or ""),
        "group": _call(r, "getGroup"),
        "code": _call(r, "getCode"),
        "email": _call(r, "getEmailAddress"),
        "max_units": j2s(_call(r, "getMaxUnits")),
        "standard_rate": j2s(_call(r, "getStandardRate")),
        "overtime_rate": j2s(_call(r, "getOvertimeRate")),
        "cost_per_use": j2s(_call(r, "getCostPerUse")),
        "accrue_at": str(_call(r, "getAccrueAt") or ""),
        "calendar": _call(_call(r, "getCalendar"), "getName") if _call(r, "getCalendar") else None,
        "work": j2s(_call(r, "getWork")),
        "work_hours": dur_hours(_call(r, "getWork")),
        "actual_work": j2s(_call(r, "getActualWork")),
        "actual_work_hours": dur_hours(_call(r, "getActualWork")),
        "remaining_work": j2s(_call(r, "getRemainingWork")),
        "overtime_work": j2s(_call(r, "getOvertimeWork")),
        "peak_units": j2s(_call(r, "getPeakUnits")),
        "cost": j2s(_call(r, "getCost")),
        "actual_cost": j2s(_call(r, "getActualCost")),
        "remaining_cost": j2s(_call(r, "getRemainingCost")),
        "baseline_work": j2s(_call(r, "getBaselineWork")),
        "baseline_cost": j2s(_call(r, "getBaselineCost")),
        "bcws": j2s(_call(r, "getBCWS")),
        "bcwp": j2s(_call(r, "getBCWP")),
        "acwp": j2s(_call(r, "getACWP")),
        "custom_fields": {f: read_field(r, ResourceField, f) for f in RESOURCE_NUMBER_FIELDS
                          if read_field(r, ResourceField, f) not in (None, "", "0.0", "0.0h", "0", 0.0, 0, False)},
    }


def extract_assignment(a):
    t = _call(a, "getTask")
    r = _call(a, "getResource")
    return {
        "uid": _call(a, "getUniqueID"),
        "task_id": _call(t, "getID") if t else None,
        "task_uid": _call(t, "getUniqueID") if t else None,
        "task_name": _call(t, "getName") if t else None,
        "resource_id": _call(r, "getID") if r else None,
        "resource_uid": _call(r, "getUniqueID") if r else None,
        "resource_name": _call(r, "getName") if r else None,
        "units": j2s(_call(a, "getUnits")),
        "work": j2s(_call(a, "getWork")),
        "work_hours": dur_hours(_call(a, "getWork")),
        "actual_work": j2s(_call(a, "getActualWork")),
        "actual_work_hours": dur_hours(_call(a, "getActualWork")),
        "remaining_work": j2s(_call(a, "getRemainingWork")),
        "start": j2s(_call(a, "getStart")),
        "finish": j2s(_call(a, "getFinish")),
        "actual_start": j2s(_call(a, "getActualStart")),
        "actual_finish": j2s(_call(a, "getActualFinish")),
        "percent_work_complete": j2s(_call(a, "getPercentageWorkComplete")),
        "cost": j2s(_call(a, "getCost")),
        "actual_cost": j2s(_call(a, "getActualCost")),
        "baseline_work": j2s(_call(a, "getBaselineWork")),
        "baseline_cost": j2s(_call(a, "getBaselineCost")),
        "baseline_start": j2s(_call(a, "getBaselineStart")),
        "baseline_finish": j2s(_call(a, "getBaselineFinish")),
        "cost_rate_table": j2s(_call(a, "getCostRateTableIndex")),
    }


def extract_calendar(c):
    out = {
        "uid": _call(c, "getUniqueID"),
        "name": _call(c, "getName"),
        "parent": _call(_call(c, "getParent"), "getName") if _call(c, "getParent") else None,
        "exceptions": [],
        "work_weeks": [],
    }
    try:
        for e in (c.getCalendarExceptions() or []):
            out["exceptions"].append({
                "name": _call(e, "getName"),
                "from": j2s(_call(e, "getFromDate")),
                "to": j2s(_call(e, "getToDate")),
                "working": bool(_call(e, "getWorking")),
            })
    except Exception:
        pass
    try:
        for w in (c.getWorkWeeks() or []):
            out["work_weeks"].append({
                "name": _call(w, "getName"),
                "from": j2s(_call(w, "getDateRange") and _call(w.getDateRange(), "getStart")),
                "to": j2s(_call(w, "getDateRange") and _call(w.getDateRange(), "getEnd")),
            })
    except Exception:
        pass
    return out


def extract_custom_field_defs(project):
    """Return {field_name: {alias, lookup_table}} for any aliased field."""
    out = {}
    try:
        for cf in project.getCustomFields():
            alias = _call(cf, "getAlias")
            ft = _call(cf, "getFieldType")
            if alias and ft:
                out[str(ft)] = {"alias": str(alias)}
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Capability detection — what can the file actually be asked about?
# ---------------------------------------------------------------------------

def _to_float(v):
    try:
        return float(v)
    except Exception:
        try:
            return float(str(v).split()[0].rstrip("h").rstrip("d").rstrip("$").replace(",", ""))
        except Exception:
            return 0.0


def compute_capabilities(tasks, resources, assignments, header):
    leaf = [t for t in tasks if not t["summary"]]
    has_baseline = any(t["baseline_sets"] for t in tasks)
    has_actuals = (
        any(_to_float(t.get("percent_complete")) > 0 for t in tasks)
        or any(t.get("actual_start") for t in tasks)
        or any(_to_float(t.get("actual_work_hours")) > 0 for t in tasks)
        or any(_to_float(t.get("actual_cost")) > 0 for t in tasks)
    )
    has_status_date = bool(header.get("status_date"))
    # Earned value can still be computed without a status date by using current_date
    has_evm_inputs = has_baseline and has_actuals
    has_costs = (
        any(r.get("standard_rate") and r["standard_rate"] not in ("0.0/h", "$0.00/h")
            for r in resources)
        or any(_to_float(t.get("fixed_cost")) > 0 for t in tasks)
        or any(_to_float(t.get("cost")) > 0 for t in leaf)
    )
    has_custom_fields = any(t["custom_fields"] for t in tasks) or any(r["custom_fields"] for r in resources)
    has_predecessors = any(t["predecessors"] for t in tasks)
    has_deadlines = any(t.get("deadline") for t in tasks)
    uses_physical_pc = any(str(t.get("earned_value_method", "")).upper() == "PHYSICAL_PERCENT_COMPLETE"
                           for t in tasks)
    return {
        "has_baseline": has_baseline,
        "has_actuals": has_actuals,
        "has_status_date": has_status_date,
        "has_evm_inputs": has_evm_inputs,
        "has_costs": has_costs,
        "has_custom_fields": has_custom_fields,
        "has_predecessors": has_predecessors,
        "has_deadlines": has_deadlines,
        "uses_physical_percent_complete": uses_physical_pc,
    }


# ---------------------------------------------------------------------------
# CSV flattening
# ---------------------------------------------------------------------------

CSV_TASK_COLS = [
    "id", "uid", "wbs", "outline_level", "name", "summary", "milestone", "critical",
    "duration", "duration_hours", "start", "finish", "early_start", "late_finish",
    "total_slack", "free_slack",
    "percent_complete", "percent_work_complete", "actual_start", "actual_finish",
    "work_hours", "actual_work_hours", "remaining_work_hours",
    "cost", "actual_cost", "fixed_cost",
    "bcws", "bcwp", "acwp", "cv", "sv",
    "resource_names", "deadline", "constraint_type", "constraint_date",
]

CSV_RES_COLS = [
    "id", "uid", "name", "initials", "type", "group", "email",
    "max_units", "standard_rate", "overtime_rate", "cost_per_use",
    "work_hours", "actual_work_hours", "cost", "actual_cost", "peak_units",
]

CSV_ASSN_COLS = [
    "uid", "task_id", "task_name", "resource_id", "resource_name", "units",
    "work_hours", "actual_work_hours", "start", "finish",
    "baseline_work", "baseline_cost", "cost", "actual_cost",
]


def write_csv(path, rows, cols):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c) for c in cols])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Extract an MS Project / Primavera file to a query-friendly JSON+CSV bundle.")
    ap.add_argument("input", help="Path to .mpp/.mpx/.xml/.mpd/.xer/.pmxml/...")
    ap.add_argument("--out", required=True, help="Output workspace directory (created if missing)")
    args = ap.parse_args()

    in_path = Path(args.input).expanduser().resolve()
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if not in_path.exists():
        print(f"[ERROR] file not found: {in_path}", file=sys.stderr)
        sys.exit(1)

    _start_jvm()
    from org.mpxj.reader import UniversalProjectReader  # type: ignore
    from org.mpxj import TaskField, ResourceField  # type: ignore

    print(f"[mpp-reader] Reading {in_path.name} ({in_path.stat().st_size:,} bytes)...")
    reader = UniversalProjectReader()
    project = reader.read(str(in_path))

    # ---- header ----
    props = project.getProjectProperties()
    global _PROJECT_PROPS
    _PROJECT_PROPS = props

    # Project-level summary task (ID=0) gives us the roll-up %complete straight from MS Project
    root_task = None
    for t in project.getTasks():
        if _call(t, "getID") == 0:
            root_task = t
            break

    header = {
        "source_file": str(in_path),
        "file_type": _call(props, "getFileType"),
        "mpp_file_type": _call(props, "getMppFileType"),
        "application": _call(props, "getFileApplication"),
        "application_version": _call(props, "getApplicationVersion"),
        "name": _call(props, "getName"),
        "title": _call(props, "getProjectTitle"),
        "author": _call(props, "getAuthor"),
        "manager": _call(props, "getManager"),
        "company": _call(props, "getCompany"),
        "subject": _call(props, "getSubject"),
        "start_date": j2s(_call(props, "getStartDate")),
        "finish_date": j2s(_call(props, "getFinishDate")),
        "status_date": j2s(_call(props, "getStatusDate")),
        "current_date": j2s(_call(props, "getCurrentDate")),
        "default_calendar": _call(props, "getDefaultCalendarName"),
        "default_task_type": str(_call(props, "getDefaultTaskType") or ""),
        "default_task_ev_method": str(_call(props, "getDefaultTaskEarnedValueMethod") or ""),
        "baseline_for_earned_value": _call(props, "getBaselineForEarnedValue"),
        "minutes_per_day": _call(props, "getMinutesPerDay"),
        "minutes_per_week": _call(props, "getMinutesPerWeek"),
        "days_per_month": _call(props, "getDaysPerMonth"),
        "currency_symbol": _call(props, "getCurrencySymbol"),
        "currency_code": _call(props, "getCurrencyCode"),
        "critical_slack_limit": j2s(_call(props, "getCriticalSlackLimit")),
        "multiple_critical_paths": bool(_call(props, "getMultipleCriticalPaths")) if _call(props, "getMultipleCriticalPaths") is not None else None,
        "honor_constraints": bool(_call(props, "getHonorConstraints")) if _call(props, "getHonorConstraints") is not None else None,
        "fiscal_year_start_month": _call(props, "getFiscalYearStartMonth"),
        "revision": _call(props, "getRevision"),
        # Roll-up values MS Project already computed on the project summary task:
        "project_percent_complete": j2s(_call(root_task, "getPercentageComplete")) if root_task else None,
        "project_percent_work_complete": j2s(_call(root_task, "getPercentageWorkComplete")) if root_task else None,
        "project_work_hours": dur_hours(_call(root_task, "getWork")) if root_task else None,
        "project_actual_work_hours": dur_hours(_call(root_task, "getActualWork")) if root_task else None,
        "project_cost": j2s(_call(root_task, "getCost")) if root_task else None,
        "project_actual_cost": j2s(_call(root_task, "getActualCost")) if root_task else None,
        "project_baseline_cost": j2s(_call(root_task, "getBaselineCost")) if root_task else None,
        "project_baseline_work_hours": dur_hours(_call(root_task, "getBaselineWork")) if root_task else None,
    }

    # ---- entities ----
    tasks = [extract_task(t, TaskField) for t in project.getTasks() if _call(t, "getName") is not None or _call(t, "getID") == 0]
    resources = [extract_resource(r, ResourceField) for r in project.getResources() if _call(r, "getName") is not None]
    assignments = [extract_assignment(a) for a in project.getResourceAssignments()]
    calendars = [extract_calendar(c) for c in project.getCalendars()]
    custom_fields = extract_custom_field_defs(project)

    counts = {
        "tasks": len(tasks),
        "tasks_summary": sum(1 for t in tasks if t["summary"]),
        "tasks_milestone": sum(1 for t in tasks if t["milestone"]),
        "tasks_critical": sum(1 for t in tasks if t["critical"]),
        "resources": len(resources),
        "resources_work": sum(1 for r in resources if r["type"] == "WORK"),
        "resources_material": sum(1 for r in resources if r["type"] == "MATERIAL"),
        "resources_cost": sum(1 for r in resources if r["type"] == "COST"),
        "assignments": len(assignments),
        "calendars": len(calendars),
    }

    capabilities = compute_capabilities(tasks, resources, assignments, header)

    project_bundle = {
        "header": header,
        "counts": counts,
        "capabilities": capabilities,
        "calendars": calendars,
        "custom_fields": custom_fields,
    }

    # ---- write outputs ----
    (out_dir / "project.json").write_text(json.dumps(project_bundle, indent=2, default=str, ensure_ascii=False))
    (out_dir / "tasks.json").write_text(json.dumps(tasks, indent=2, default=str, ensure_ascii=False))
    (out_dir / "resources.json").write_text(json.dumps(resources, indent=2, default=str, ensure_ascii=False))
    (out_dir / "assignments.json").write_text(json.dumps(assignments, indent=2, default=str, ensure_ascii=False))

    write_csv(out_dir / "tasks.csv", tasks, CSV_TASK_COLS)
    write_csv(out_dir / "resources.csv", resources, CSV_RES_COLS)
    write_csv(out_dir / "assignments.csv", assignments, CSV_ASSN_COLS)

    # ---- console summary ----
    print(f"\n=== {in_path.name} ===")
    print(f"Title:   {header.get('title')!r}")
    print(f"Author:  {header.get('author')!r}")
    print(f"Dates:   {header.get('start_date')} → {header.get('finish_date')}")
    print(f"Status:  {header.get('status_date') or '(no status date set)'}")
    print(f"Counts:  {counts['tasks']} tasks ({counts['tasks_summary']} summary, "
          f"{counts['tasks_milestone']} milestone, {counts['tasks_critical']} critical), "
          f"{counts['resources']} resources, {counts['assignments']} assignments, {counts['calendars']} calendars")
    print(f"Flags:   " + ", ".join(f"{k}={v}" for k, v in capabilities.items()))
    print(f"\nWrote bundle to {out_dir}")

    # shutdown JVM cleanly
    try:
        import mpxj
        mpxj.shutdownJVM()
    except Exception:
        pass


if __name__ == "__main__":
    main()
