#!/usr/bin/env python3
"""
build_project.py — Generate a Microsoft Project schedule from a JSON spec.

Default output is **Microsoft Project XML** (.xml), the official interchange
format that MS Project opens natively and can Save As .mpp.

Native .mpp is not writable by open-source tools — the format is closed.
Two options:
  1. Open the generated XML in MS Project → File → Save As → Project (.mpp).
  2. If you have an Aspose.Tasks license installed, pass `--format mpp`
     and the script will use Aspose to write native .mpp.

Usage:
    python3 build_project.py spec.json --out project.xml
    python3 build_project.py spec.json --out project.mpp --format mpp
    cat spec.json | python3 build_project.py - --out out.xml

JSON spec (all fields optional unless marked required):

    {
      "project": {
        "title": "Proyecto demo",           // required
        "author": "Mauricio",
        "manager": "Jefe",
        "company": "Projectical",
        "start_date": "2026-05-04",         // required OR give tasks with dates
        "default_calendar": "Standard",
        "currency_symbol": "$", "currency_code": "COP",
        "minutes_per_day": 480,             // 8h working day
        "minutes_per_week": 2400,           // Mon-Fri 8h
        "default_task_type": "FIXED_UNITS", // FIXED_DURATION | FIXED_WORK
        "default_task_ev_method": "PERCENT_COMPLETE",
        "status_date": "2026-05-15"
      },
      "calendars": [                         // optional, else a Standard calendar is generated
        {"name": "Standard",
         "working_days": ["MON","TUE","WED","THU","FRI"],
         "daily_hours": ["08:00-12:00","13:00-17:00"],
         "exceptions": [{"name":"Navidad","date":"2026-12-25","working": false}]}
      ],
      "resources": [
        {"id": 1, "name": "Ana",    "type": "WORK",     "max_units": 100, "standard_rate": "50/h"},
        {"id": 2, "name": "Cemento","type": "MATERIAL", "material_label": "m3", "standard_rate": 200},
        {"id": 3, "name": "Viaje",  "type": "COST"}
      ],
      "tasks": [
        {"id": 1, "name": "Fase 1", "outline_level": 1, "summary": true},
        {"id": 2, "name": "Diseño", "outline_level": 2,
         "duration": "5d",           // e.g. "5d", "40h", "2w", "30m"
         "start": "2026-05-04",       // optional; scheduler will compute if absent
         "type": "FIXED_WORK",
         "earned_value_method": "PERCENT_COMPLETE",
         "deadline": "2026-05-18",
         "percent_complete": 30,
         "notes": "Alcance preliminar",
         "predecessors": []},
        {"id": 3, "name": "Construcción", "outline_level": 2, "duration": "10d",
         "predecessors": [{"id": 2, "type": "FS", "lag": "0d"}]}
      ],
      "assignments": [
        {"task_id": 2, "resource_id": 1, "units": 100, "work": "40h"},
        {"task_id": 3, "resource_id": 2, "units": 1,   "work": "10 m3"}
      ],
      "options": {
        "save_baseline": true            // capture the generated values as Baseline
      }
    }

Exit 0 on success; non-zero on validation / write failure.
"""

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# JVM bootstrap (reuses the same MPXJ package the other scripts use)
# ---------------------------------------------------------------------------

def _start_jvm():
    try:
        import mpxj  # noqa: F401
    except ImportError:
        sys.stderr.write("[mpp-reader] Missing 'mpxj' Python package. pip install mpxj jpype1\n")
        sys.exit(2)
    import jpype
    if not jpype.isJVMStarted():
        import mpxj
        mpxj.startJVM()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DUR_RE = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)\s*(em|eh|ed|ew|emo|mo|min|m|h|d|w|y)?\s*$", re.I)
_DUR_UNITS = {
    "m": "MINUTES", "min": "MINUTES",
    "h": "HOURS",
    "d": "DAYS",
    "w": "WEEKS",
    "mo": "MONTHS",
    "y": "YEARS",
    "em": "ELAPSED_MINUTES",
    "eh": "ELAPSED_HOURS",
    "ed": "ELAPSED_DAYS",
    "ew": "ELAPSED_WEEKS",
    "emo": "ELAPSED_MONTHS",
}


def parse_duration(value, TimeUnit, Duration):
    """Parse a duration string/number into an MPXJ Duration."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return Duration.getInstance(float(value), TimeUnit.DAYS)  # assume days
    m = _DUR_RE.match(str(value))
    if not m:
        raise ValueError(f"Invalid duration '{value}'")
    n = float(m.group(1))
    unit_key = (m.group(2) or "d").lower()
    tu = getattr(TimeUnit, _DUR_UNITS[unit_key])
    return Duration.getInstance(n, tu)


def parse_rate(value, TimeUnit, Rate):
    """Parse '50/h', 100, '100/d', '200/w' etc into an MPXJ Rate."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return Rate(float(value), TimeUnit.HOURS)
    s = str(value).strip().replace("$", "").replace(",", "")
    if "/" in s:
        amt, per = s.split("/", 1)
        per = per.strip().lower()
        unit = {"h": TimeUnit.HOURS, "d": TimeUnit.DAYS, "w": TimeUnit.WEEKS,
                "mo": TimeUnit.MONTHS, "y": TimeUnit.YEARS, "min": TimeUnit.MINUTES}.get(per, TimeUnit.HOURS)
        return Rate(float(amt.strip()), unit)
    return Rate(float(s), TimeUnit.HOURS)


def parse_dt(value):
    """Parse ISO date or datetime string into java.time.LocalDateTime."""
    from java.time import LocalDateTime  # type: ignore
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return LocalDateTime.of(value.year, value.month, value.day, value.hour, value.minute)
    if isinstance(value, datetime.date):
        return LocalDateTime.of(value.year, value.month, value.day, 8, 0)  # default 08:00
    s = str(value)
    try:
        d = datetime.datetime.fromisoformat(s)
    except ValueError:
        # "YYYY-MM-DD HH:MM" variants
        for fmt in ("%Y-%m-%d %H:%M", "%Y/%m/%d", "%d/%m/%Y", "%Y-%m-%d"):
            try:
                d = datetime.datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"Invalid date '{value}' (use ISO 8601 or YYYY-MM-DD)")
    return LocalDateTime.of(d.year, d.month, d.day,
                            d.hour if hasattr(d, "hour") else 8,
                            d.minute if hasattr(d, "minute") else 0)


def parse_time_pair(s):
    """Parse '08:00-12:00' → (datetime.time(8,0), datetime.time(12,0))."""
    a, b = s.split("-")
    ah, am = a.strip().split(":")
    bh, bm = b.strip().split(":")
    return datetime.time(int(ah), int(am)), datetime.time(int(bh), int(bm))


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

DAY_NAMES = {"MON": "MONDAY", "TUE": "TUESDAY", "WED": "WEDNESDAY",
             "THU": "THURSDAY", "FRI": "FRIDAY", "SAT": "SATURDAY", "SUN": "SUNDAY"}


def build_project(spec: dict, out_path: Path, fmt: str = "xml"):
    _start_jvm()
    import jpype  # type: ignore
    from org.mpxj import (ProjectFile, TimeUnit, Duration, Rate,  # type: ignore
                          ResourceType, TaskType, RelationType,
                          EarnedValueMethod, Priority, ConstraintType,
                          LocalTimeRange, CostRateTableEntry, Relation)
    from org.mpxj.writer import UniversalProjectWriter, FileFormat  # type: ignore
    from java.time import LocalTime, DayOfWeek  # type: ignore
    from java.lang import Integer  # type: ignore
    from java.math import BigDecimal  # type: ignore
    RateArray = jpype.JArray(Rate)

    def _JI(x):
        """Wrap a Python int as java.lang.Integer (JPype needs boxing for setter overloads)."""
        return Integer(int(x)) if x is not None else None

    def _set_resource_rates(resource, std_rate=None, ot_rate=None, per_use=None):
        """Replace the resource's primary cost rate table (index 0) with one entry."""
        rates_list = []
        if std_rate is not None:
            rates_list.append(std_rate)
        if ot_rate is not None:
            # rates_list[0]=standard, [1]=overtime per MPXJ convention
            if not rates_list:
                rates_list.append(Rate(0, TimeUnit.HOURS))
            rates_list.append(ot_rate)
        if not rates_list:
            return
        entry = CostRateTableEntry(
            CostRateTableEntry.DEFAULT_ENTRY.getStartDate(),
            CostRateTableEntry.DEFAULT_ENTRY.getEndDate(),
            BigDecimal(float(per_use or 0)),
            RateArray(rates_list),
        )
        tbl = resource.getCostRateTable(0)
        tbl.clear()
        tbl.add(entry)

    project = ProjectFile()
    props = project.getProjectProperties()

    # -------- Project header --------
    P = spec.get("project", {})
    props.setProjectTitle(P.get("title") or P.get("name") or "Untitled")
    if P.get("name"):       props.setName(P["name"])
    if P.get("author"):     props.setAuthor(P["author"])
    if P.get("manager"):    props.setManager(P["manager"])
    if P.get("company"):    props.setCompany(P["company"])
    if P.get("subject"):    props.setSubject(P["subject"])
    if P.get("currency_symbol"): props.setCurrencySymbol(P["currency_symbol"])
    if P.get("currency_code"):   props.setCurrencyCode(P["currency_code"])
    if P.get("minutes_per_day") is not None:  props.setMinutesPerDay(_JI(P["minutes_per_day"]))
    if P.get("minutes_per_week") is not None: props.setMinutesPerWeek(_JI(P["minutes_per_week"]))
    if P.get("days_per_month") is not None:   props.setDaysPerMonth(_JI(P["days_per_month"]))

    if P.get("default_task_type"):
        props.setDefaultTaskType(getattr(TaskType, P["default_task_type"].upper()))
    if P.get("default_task_ev_method"):
        props.setDefaultTaskEarnedValueMethod(getattr(EarnedValueMethod, P["default_task_ev_method"].upper()))
    if P.get("start_date"):
        props.setStartDate(parse_dt(P["start_date"]))
    if P.get("finish_date"):
        props.setFinishDate(parse_dt(P["finish_date"]))
    if P.get("status_date"):
        props.setStatusDate(parse_dt(P["status_date"]))

    # -------- Calendars --------
    cal_map = {}
    cal_specs = spec.get("calendars") or []
    if not cal_specs:
        cal_specs = [{"name": P.get("default_calendar") or "Standard",
                      "working_days": ["MON","TUE","WED","THU","FRI"],
                      "daily_hours": ["08:00-12:00","13:00-17:00"]}]

    for cs in cal_specs:
        c = project.addCalendar()
        c.setName(cs["name"])
        working = {DAY_NAMES[d.upper()] for d in (cs.get("working_days") or [])}
        for dn in ("MONDAY","TUESDAY","WEDNESDAY","THURSDAY","FRIDAY","SATURDAY","SUNDAY"):
            dow = getattr(DayOfWeek, dn)
            is_work = dn in working
            c.setWorkingDay(dow, bool(is_work))
            if is_work:
                hours = c.addCalendarHours(dow)
                hour_specs = cs.get("daily_hours") or ["08:00-12:00", "13:00-17:00"]
                for hp in hour_specs:
                    a, b = parse_time_pair(hp)
                    hours.add(LocalTimeRange(
                        LocalTime.of(a.hour, a.minute),
                        LocalTime.of(b.hour, b.minute)
                    ))
        for e in (cs.get("exceptions") or []):
            from java.time import LocalDate  # type: ignore
            d = datetime.date.fromisoformat(e["date"]) if isinstance(e["date"], str) else e["date"]
            exc = c.addCalendarException(LocalDate.of(d.year, d.month, d.day))
            if e.get("name"): exc.setName(e["name"])
            if e.get("working"):
                # working exception: attach hours
                for hp in (e.get("daily_hours") or ["08:00-12:00", "13:00-17:00"]):
                    a, b = parse_time_pair(hp)
                    exc.add(LocalTimeRange(
                        LocalTime.of(a.hour, a.minute),
                        LocalTime.of(b.hour, b.minute)
                    ))
            # non-working exceptions need no hours — empty is what MS Project expects
        cal_map[cs["name"]] = c

    # Project default calendar (MPXJ: setDefaultCalendar(ProjectCalendar))
    default_name = P.get("default_calendar") or cal_specs[0]["name"]
    if default_name in cal_map:
        props.setDefaultCalendar(cal_map[default_name])

    # -------- Resources --------
    res_map = {}
    for rs in (spec.get("resources") or []):
        r = project.addResource()
        if rs.get("id") is not None: r.setUniqueID(_JI(rs["id"]))
        r.setName(rs["name"])
        rtype = (rs.get("type") or "WORK").upper()
        r.setType(getattr(ResourceType, rtype))
        if rs.get("initials"):        r.setInitials(rs["initials"])
        if rs.get("group"):           r.setGroup(rs["group"])
        if rs.get("email"):           r.setEmailAddress(rs["email"])
        if rs.get("code"):            r.setCode(rs["code"])
        if rs.get("material_label"):  r.setUnit(rs["material_label"])  # MPXJ 16 renamed MaterialLabel → Unit
        if rs.get("max_units") is not None and rtype == "WORK":
            # MPXJ 16 renamed setMaxUnits → setDefaultUnits for the "availability" concept
            r.setDefaultUnits(float(rs["max_units"]))
        # Rates and per-use go through the CostRateTable, not direct setters
        std = parse_rate(rs.get("standard_rate"), TimeUnit, Rate) if rs.get("standard_rate") is not None else None
        ot  = parse_rate(rs.get("overtime_rate"), TimeUnit, Rate) if rs.get("overtime_rate") is not None else None
        if std is not None or ot is not None or rs.get("cost_per_use") is not None:
            _set_resource_rates(r, std_rate=std, ot_rate=ot, per_use=rs.get("cost_per_use"))
        if rs.get("calendar") and rs["calendar"] in cal_map:
            r.setCalendar(cal_map[rs["calendar"]])
        res_map[rs.get("id", r.getUniqueID())] = r

    # -------- Tasks (two-pass for predecessors) --------
    task_specs = spec.get("tasks") or []
    task_map = {}
    # Pass 1: create tasks preserving outline hierarchy. We maintain a stack
    # of "current parent at level N". Level 1 means a top-level task.
    stack = [project]  # stack[0] = project (synthetic root)
    for ts in task_specs:
        level = int(ts.get("outline_level") or 1)
        while len(stack) < level:
            stack.append(None)
        parent = stack[level - 1]
        if parent is None:
            # no parent at that level; fall back to project root
            parent = project
        t = parent.addTask()
        task_map[ts.get("id", t.getUniqueID())] = t
        if ts.get("id") is not None:
            t.setUniqueID(_JI(ts["id"]))
            t.setID(_JI(ts["id"]))

        t.setName(ts["name"])
        if ts.get("notes"):           t.setNotes(ts["notes"])
        if ts.get("wbs"):             t.setWBS(str(ts["wbs"]))
        if ts.get("milestone"):       t.setMilestone(True)
        if ts.get("active") is False: t.setActive(False)

        if ts.get("duration") is not None:
            t.setDuration(parse_duration(ts["duration"], TimeUnit, Duration))
        if ts.get("start"):   t.setStart(parse_dt(ts["start"]))
        if ts.get("finish"):  t.setFinish(parse_dt(ts["finish"]))
        if ts.get("deadline"): t.setDeadline(parse_dt(ts["deadline"]))
        if ts.get("constraint_type"):
            t.setConstraintType(getattr(ConstraintType, ts["constraint_type"].upper()))
        if ts.get("constraint_date"):
            t.setConstraintDate(parse_dt(ts["constraint_date"]))
        if ts.get("type"):
            t.setType(getattr(TaskType, ts["type"].upper()))
        if ts.get("earned_value_method"):
            t.setEarnedValueMethod(getattr(EarnedValueMethod, ts["earned_value_method"].upper()))
        if ts.get("priority") is not None:
            t.setPriority(Priority.getInstance(int(ts["priority"])))
        if ts.get("percent_complete") is not None:
            t.setPercentageComplete(float(ts["percent_complete"]))
        if ts.get("physical_percent_complete") is not None:
            t.setPhysicalPercentComplete(_JI(ts["physical_percent_complete"]))
        if ts.get("fixed_cost") is not None:
            t.setFixedCost(float(ts["fixed_cost"]))
        if ts.get("actual_start"):  t.setActualStart(parse_dt(ts["actual_start"]))
        if ts.get("actual_finish"): t.setActualFinish(parse_dt(ts["actual_finish"]))
        if ts.get("calendar") and ts["calendar"] in cal_map:
            t.setCalendar(cal_map[ts["calendar"]])

        # Populate custom fields: {"TEXT1": "...", "NUMBER3": 42, ...}
        from org.mpxj import TaskField  # type: ignore
        for fname, fval in (ts.get("custom_fields") or {}).items():
            try:
                field = getattr(TaskField, fname.upper(), None)
                if field is not None:
                    t.set(field, fval)
            except Exception:
                pass

        # Register this task at its outline level for children to attach to
        while len(stack) <= level:
            stack.append(None)
        stack[level] = t
        # Invalidate deeper levels so a sibling at a deeper level doesn't mis-attach
        for i in range(level + 1, len(stack)):
            stack[i] = None

    # Pass 2: predecessors (accept FS/SS/FF/SF shorthand as well as long names)
    REL_ALIAS = {
        "FS": "FINISH_START", "SS": "START_START",
        "FF": "FINISH_FINISH", "SF": "START_FINISH",
        "FINISH_START": "FINISH_START", "START_START": "START_START",
        "FINISH_FINISH": "FINISH_FINISH", "START_FINISH": "START_FINISH",
    }
    for ts in task_specs:
        t = task_map.get(ts.get("id"))
        if not t:
            continue
        for pr in (ts.get("predecessors") or []):
            other = task_map.get(pr.get("id"))
            if not other:
                continue
            rel_name = REL_ALIAS.get((pr.get("type") or "FS").upper(), "FINISH_START")
            rel_type = getattr(RelationType, rel_name)
            lag = parse_duration(pr.get("lag") or "0d", TimeUnit, Duration)
            # MPXJ 16 switched to Builder pattern for Relation
            builder = (Relation.Builder()
                       .predecessorTask(other)
                       .successorTask(t)
                       .type(rel_type)
                       .lag(lag))
            t.addPredecessor(builder)

    # -------- Assignments --------
    for a in (spec.get("assignments") or []):
        tk = task_map.get(a["task_id"])
        rs = res_map.get(a["resource_id"])
        if not tk or not rs:
            continue
        assn = tk.addResourceAssignment(rs)
        if a.get("units") is not None: assn.setUnits(float(a["units"]))
        if a.get("work") is not None:
            # Material assignments use the resource's material_label unit; for work use hours
            try:
                assn.setWork(parse_duration(a["work"], TimeUnit, Duration))
            except Exception:
                # "10 m3" style for materials: leave to MPXJ default handling
                pass
        if a.get("start"):  assn.setStart(parse_dt(a["start"]))
        if a.get("finish"): assn.setFinish(parse_dt(a["finish"]))
        if a.get("percent_work_complete") is not None:
            assn.setPercentageWorkComplete(float(a["percent_work_complete"]))
        if a.get("cost_rate_table") is not None:
            assn.setCostRateTableIndex(_JI(a["cost_rate_table"]))

    # -------- Save baseline --------
    # Capture the current scheduled values as Baseline0 (the "main" baseline).
    # MPXJ doesn't have a one-liner for this in v16, so we copy per task + per assignment.
    if (spec.get("options") or {}).get("save_baseline"):
        for t in project.getTasks():
            try:
                if t.getStart() is not None:    t.setBaselineStart(t.getStart())
                if t.getFinish() is not None:   t.setBaselineFinish(t.getFinish())
                if t.getDuration() is not None: t.setBaselineDuration(t.getDuration())
                if t.getCost() is not None:     t.setBaselineCost(t.getCost())
                if t.getWork() is not None:     t.setBaselineWork(t.getWork())
            except Exception:
                pass
        for a in project.getResourceAssignments():
            try:
                if a.getStart() is not None:  a.setBaselineStart(a.getStart())
                if a.getFinish() is not None: a.setBaselineFinish(a.getFinish())
                if a.getWork() is not None:   a.setBaselineWork(a.getWork())
                if a.getCost() is not None:   a.setBaselineCost(a.getCost())
            except Exception:
                pass

    # -------- Write --------
    fmt = (fmt or "xml").lower()
    if fmt == "xml":
        out_path = out_path.with_suffix(".xml") if out_path.suffix.lower() != ".xml" else out_path
        writer = UniversalProjectWriter(FileFormat.MSPDI)
        writer.write(project, str(out_path))
        return out_path, "xml"
    elif fmt == "mpx":
        out_path = out_path.with_suffix(".mpx")
        writer = UniversalProjectWriter(FileFormat.MPX)
        writer.write(project, str(out_path))
        return out_path, "mpx"
    elif fmt == "mpp":
        # Native .mpp requires Aspose.Tasks (commercial). Optional plugin.
        try:
            return _write_mpp_with_aspose(project, out_path)
        except ImportError as e:
            sys.stderr.write(
                "[mpp-reader] Native .mpp output requires Aspose.Tasks (commercial).\n"
                f"Install: pip install aspose-tasks AND provide a license.\n"
                f"Details: {e}\n"
                "Tip: write XML instead (the default), open it in MS Project and Save As .mpp.\n"
            )
            sys.exit(3)
    else:
        raise ValueError(f"Unknown format '{fmt}'. Use xml, mpx, or mpp.")


def _write_mpp_with_aspose(mpxj_project, out_path: Path):
    """Optional: use Aspose.Tasks to persist a native .mpp via XML round-trip."""
    import tempfile
    from org.mpxj.writer import UniversalProjectWriter, FileFormat  # type: ignore
    import aspose.tasks as at  # type: ignore

    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tmp:
        UniversalProjectWriter(FileFormat.MSPDI).write(mpxj_project, tmp.name)
        p = at.Project(tmp.name)
        p.save(str(out_path), at.saving.SaveFileFormat.MPP)
    return out_path, "mpp"


# ---------------------------------------------------------------------------
# Phase-based spec → task-based spec (with strict sequencing rules)
# ---------------------------------------------------------------------------
#
# Input spec (phase-oriented, friendlier for LLM output):
# {
#   "project": { "title": "...", "start_date": "...", ... },
#   "calendars": [ ... ],
#   "resources": [
#       { "id": 1, "name": "Analyst", "type": "WORK", "standard_rate": "50000/h" }
#   ],
#   "phases": [
#     {
#       "id": "P1",
#       "name": "Análisis",
#       "description": "...",
#       "activities": [
#         { "id": "A1", "name": "Relevar requisitos",
#           "duration": "5d", "resource_id": 1, "units": 100,
#           "predecessor": null, "notes": "..." },
#         { "id": "A2", "name": "Especificar requisitos",
#           "duration": "3d", "resource_id": 1, "predecessor": "A1" }
#       ]
#     },
#     { ... next phase ... }
#   ],
#   "options": { "save_baseline": true, "milestone_name_prefix": "Hito · " }
# }
#
# Output (after expansion, passed to build_project()):
#
#   Project root (level 0, summary, NO relations)
#     Phase 1 (level 1, summary, NO relations)
#       Inicio · Phase 1 (level 2, milestone, NO predecessor if first phase of project)
#       Activity 1         (level 2, predecessor = phase start milestone)
#       Activity 2         (level 2, predecessor = Activity 1)
#       ...
#       Fin · Phase 1      (level 2, milestone, predecessor = last activity)
#     Phase 2 (level 1, summary, NO relations)
#       Inicio · Phase 2   (predecessor = Fin · Phase 1)
#       ...
#
# Sequencing invariants (validated after build):
#   • Summary tasks → NO predecessors, NO successors
#   • Every non-summary task has ≥1 predecessor EXCEPT the very first
#   • Every non-summary task has ≥1 successor   EXCEPT the very last
#   • Each phase summary wraps its children between a Start and End milestone

def build_project_from_phases(spec: dict, out_path: Path, fmt: str = "xml") -> tuple[Path, str]:
    """Expand a phase-centric spec into a task-centric spec and hand it to
    build_project() to emit XML/MPX/MPP. Enforces the sequencing rules above."""
    project_cfg   = dict(spec.get("project") or {})
    calendars_cfg = spec.get("calendars") or []
    resources_cfg = list(spec.get("resources") or [])
    phases_cfg    = spec.get("phases") or []
    options_cfg   = dict(spec.get("options") or {})
    start_prefix  = options_cfg.get("milestone_name_prefix_start") or "Inicio · "
    end_prefix    = options_cfg.get("milestone_name_prefix_end")   or "Fin · "

    if not phases_cfg:
        raise ValueError("Spec must have at least one phase.")

    # We use dense sequential integer IDs starting at 1 so MPXJ doesn't
    # complain and so inter-task references are simple.
    tasks: list[dict] = []
    assignments: list[dict] = []
    next_id = 1
    def new_id() -> int:
        nonlocal next_id
        i = next_id
        next_id += 1
        return i

    last_phase_end_id: Optional[int] = None
    activity_remap: dict[str, int] = {}  # original spec id → numeric id (scoped per phase but unique overall)

    for phase_idx, phase in enumerate(phases_cfg):
        phase_name = phase.get("name") or f"Fase {phase_idx + 1}"
        # ---- phase summary (level 1, no relations) ------------------------
        phase_summary_id = new_id()
        tasks.append({
            "id": phase_summary_id,
            "name": phase_name,
            "outline_level": 1,
            "summary": True,
            "notes": phase.get("description") or "",
        })

        # ---- start milestone (level 2) ------------------------------------
        start_id = new_id()
        start_preds = []
        if last_phase_end_id is not None:
            start_preds = [{"id": last_phase_end_id, "type": "FS", "lag": "0d"}]
        tasks.append({
            "id": start_id,
            "name": f"{start_prefix}{phase_name}",
            "outline_level": 2,
            "milestone": True,
            "duration": "0d",
            "predecessors": start_preds,
        })

        # ---- activities (level 2) chained FS by default -------------------
        phase_activity_ids: list[int] = []
        phase_remap: dict[str, int] = {}  # local map used to resolve predecessor refs inside the phase
        prev_id = start_id
        for act in (phase.get("activities") or []):
            act_id = new_id()
            phase_remap[act["id"]] = act_id
            activity_remap[act["id"]] = act_id

            # Resolve predecessor: explicit one inside the phase, else chain from previous
            pred_ref = act.get("predecessor")
            if pred_ref:
                pred_numeric = phase_remap.get(pred_ref) or activity_remap.get(pred_ref)
                if pred_numeric is None:
                    # Unknown predecessor reference — fall back to chain and warn
                    pred_numeric = prev_id
            else:
                pred_numeric = prev_id

            t = {
                "id": act_id,
                "name": act["name"],
                "outline_level": 2,
                "duration": act.get("duration") or "1d",
                "predecessors": [{"id": pred_numeric, "type": act.get("relationship_type") or "FS",
                                  "lag": act.get("lag") or "0d"}],
            }
            if act.get("notes"):
                t["notes"] = act["notes"]
            if act.get("constraint_type"):
                t["constraint_type"] = act["constraint_type"]
            if act.get("constraint_date"):
                t["constraint_date"] = act["constraint_date"]
            tasks.append(t)

            # Assignment
            if act.get("resource_id") is not None:
                assignments.append({
                    "task_id": act_id,
                    "resource_id": int(act["resource_id"]),
                    "units": float(act.get("units") or 100),
                    "work": act.get("work"),
                })

            phase_activity_ids.append(act_id)
            prev_id = act_id

        # ---- end milestone (level 2) --------------------------------------
        end_id = new_id()
        end_pred_numeric = phase_activity_ids[-1] if phase_activity_ids else start_id
        tasks.append({
            "id": end_id,
            "name": f"{end_prefix}{phase_name}",
            "outline_level": 2,
            "milestone": True,
            "duration": "0d",
            "predecessors": [{"id": end_pred_numeric, "type": "FS", "lag": "0d"}],
        })
        last_phase_end_id = end_id

    # ---- validation: non-summary tasks must have predecessors and successors ----
    # Summary tasks must not have any predecessors.
    by_id = {t["id"]: t for t in tasks}
    has_successor: set[int] = set()
    for t in tasks:
        for p in t.get("predecessors") or []:
            has_successor.add(int(p["id"]))

    warnings_list: list[str] = []
    non_summary = [t for t in tasks if not t.get("summary")]
    for i, t in enumerate(non_summary):
        # Predecessor rule: all except the very first non-summary task
        has_pred = bool(t.get("predecessors"))
        if i == 0:
            if has_pred:
                warnings_list.append(f"First task '{t['name']}' unexpectedly has a predecessor.")
        else:
            if not has_pred:
                warnings_list.append(f"Task '{t['name']}' (id={t['id']}) lacks a predecessor.")
        # Successor rule: all except the very last non-summary task
        if i == len(non_summary) - 1:
            if t["id"] in has_successor:
                warnings_list.append(f"Last task '{t['name']}' unexpectedly has a successor.")
        else:
            if t["id"] not in has_successor:
                warnings_list.append(f"Task '{t['name']}' (id={t['id']}) lacks a successor.")

    # Summary tasks: no relations allowed
    for t in tasks:
        if t.get("summary") and t.get("predecessors"):
            warnings_list.append(f"Summary '{t['name']}' has predecessors — removing.")
            t["predecessors"] = []

    # Stitch together the full spec expected by build_project()
    full_spec = {
        "project": project_cfg,
        "calendars": calendars_cfg,
        "resources": resources_cfg,
        "tasks": tasks,
        "assignments": assignments,
        "options": options_cfg,
    }
    # Print warnings for transparency (they go to stdout and are captured by callers)
    for w in warnings_list:
        sys.stderr.write(f"[build_project_from_phases] WARN: {w}\n")
    if warnings_list:
        sys.stderr.write(f"[build_project_from_phases] {len(warnings_list)} warning(s) found during sequencing validation.\n")

    return build_project(full_spec, out_path, fmt)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Generate a Microsoft Project schedule from a JSON spec.")
    ap.add_argument("spec", help="Path to JSON spec file, or '-' to read from stdin")
    ap.add_argument("--out", required=True, help="Output file path (.xml, .mpx or .mpp)")
    ap.add_argument("--format", choices=["xml", "mpx", "mpp"], default=None,
                    help="Output format. Defaults to the extension of --out (xml if absent).")
    ap.add_argument("--mode", choices=["tasks", "phases"], default="tasks",
                    help="'tasks' (default): spec has flat tasks[]. "
                         "'phases': spec has phases[] with activities; expanded with start/end milestones per phase.")
    args = ap.parse_args()

    if args.spec == "-":
        spec = json.load(sys.stdin)
    else:
        spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))

    fmt = args.format or Path(args.out).suffix.lstrip(".") or "xml"
    out = Path(args.out).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    if args.mode == "phases":
        written, final_fmt = build_project_from_phases(spec, out, fmt)
    else:
        written, final_fmt = build_project(spec, out, fmt)
    print(f"[mpp-reader] Wrote {written} ({final_fmt})")


if __name__ == "__main__":
    main()
