#!/usr/bin/env python3
"""tools-call-collector: local statistics over the collected event logs.

Aggregates every events/*-calls.jsonl file written by collector.py (one file
per configured category, plus the generic tool-calls.jsonl for "all").
Read-only: it never modifies the data.

Usage:
  python3 stats.py [--data-dir DIR] [--days N] [--by tool|session|day|skill]
                   [--json] [--info]
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

PLUGIN_NAME = "tools-call-collector"
CATEGORIES_ENV = "TOOL_CALL_COLLECTOR_CATEGORIES"


def data_dir(explicit=None):
    if explicit:
        return Path(explicit).expanduser()
    override = os.environ.get("TOOL_CALL_COLLECTOR_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".devin" / PLUGIN_NAME


def event_files(ddir):
    events = ddir / "events"
    return sorted(events.glob("*-calls.jsonl")) if events.is_dir() else []


def parse_ts(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_records(ddir, days=None):
    cutoff = None
    if days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    records, bad_lines, files_seen = [], 0, []
    for path in event_files(ddir):
        files_seen.append(path.name)
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    bad_lines += 1
                    continue
                if not isinstance(record, dict) or record.get("type") not in ("tool_call", "skill_invoke"):
                    continue
                ts = parse_ts(record.get("timestamp"))
                if cutoff is not None and (ts is None or ts < cutoff):
                    continue
                records.append(record)
    return records, bad_lines, files_seen


def tool_of(record):
    # skill invocations are skill-tool calls; count them under "skill"
    return record["tool_name"] if record.get("type") == "tool_call" else "skill"


def summarize_tools(records):
    tools = defaultdict(Counter)
    sessions = defaultdict(lambda: {"counts": Counter(), "project_dir": None, "day": None})
    days = defaultdict(Counter)
    first = last = None
    for rec in records:
        tool = tool_of(rec) or "<unknown>"
        status = rec.get("status") if rec.get("status") in ("success", "error", "unknown") else "unknown"
        tools[tool]["total"] += 1
        tools[tool][status] += 1
        ts = parse_ts(rec.get("timestamp"))
        if ts is not None:
            day = ts.strftime("%Y-%m-%d")
            days[day][tool] += 1
            first = ts if first is None or ts < first else first
            last = ts if last is None or ts > last else last
        sid = rec.get("session_id") or "<unknown>"
        entry = sessions[sid]
        entry["counts"][tool] += 1
        if entry["project_dir"] is None:
            entry["project_dir"] = rec.get("project_dir")
        if entry["day"] is None and ts is not None:
            entry["day"] = ts.strftime("%Y-%m-%d")
    return tools, sessions, days, first, last


def summarize_skills(records):
    skills = Counter()
    first = last = None
    for rec in records:
        name = rec.get("skill")
        if not name:
            name = f"<{rec['skill_command']}>" if rec.get("skill_command") else "<unidentified>"
        skills[name] += 1
        ts = parse_ts(rec.get("timestamp"))
        if ts is not None:
            first = ts if first is None or ts < first else first
            last = ts if last is None or ts > last else last
    return skills, first, last


def fmt_ts(ts):
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ") if ts else "-"


def print_tool_table(tools):
    rows = sorted(tools.items(), key=lambda kv: (-kv[1]["total"], kv[0]))
    print(f"  {'tool':<30}{'total':>7}{'success':>9}{'error':>8}{'unknown':>9}")
    for tool, c in rows:
        print(f"  {tool:<30}{c['total']:>7}{c['success']:>9}{c['error']:>8}{c['unknown']:>9}")


def print_sessions(sessions):
    rows = sorted(sessions.items(), key=lambda kv: kv[1]["day"] or "")
    for sid, entry in rows:
        short = sid[:8] + "…" if isinstance(sid, str) and len(sid) > 8 else sid
        parts = [f"{tool} {n}" for tool, n in entry["counts"].most_common()]
        print(f"  session {short}  {entry['day'] or '-'}  {entry['project_dir'] or '-'}")
        print(f"    {', '.join(parts)}")


def print_days(days):
    for day in sorted(days):
        counts = days[day].most_common()
        print(f"  {day}: " + ", ".join(f"{tool} {n}" for tool, n in counts))


def print_skills(skills):
    print(f"  {'skill':<44}{'total':>7}")
    for name, n in skills.most_common():
        print(f"  {name:<44}{n:>7}")


def print_info(ddir):
    try:
        import collector
        categories, source = collector.resolve_categories(ddir=ddir)
        label = ",".join(sorted(categories)) if categories else "(off)"
        print(f"categories     : {label}  (source: {source})")
    except Exception:
        config = os.environ.get(CATEGORIES_ENV)
        print(f"categories     : {config if config else '(unset or empty -> collection disabled)'}")
    files = event_files(ddir)
    if not files:
        print("event files    : (none yet)")
    for f in files:
        lines = sum(1 for _ in open(f, encoding="utf-8"))
        print(f"{f.name:<20}: {f.stat().st_size} bytes, {lines} line(s)")
    print(f"{'upload cursor':<20}: byte_offset=0 (nothing uploaded yet; see README 'Uploading later')")


def main():
    parser = argparse.ArgumentParser(description="Aggregate tools-call-collector event data.")
    parser.add_argument("--data-dir", help="Override the data directory.")
    parser.add_argument("--days", type=int, help="Only include events from the last N days (UTC).")
    parser.add_argument("--by", choices=("tool", "session", "day", "skill"), default="tool",
                        help="Breakdown mode; 'skill' uses skill_invoke records.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument("--info", action="store_true", help="Show config and event-file info.")
    args = parser.parse_args()

    ddir = data_dir(args.data_dir)

    if args.info:
        print_info(ddir)
        return 0

    records, bad_lines, files_seen = load_records(ddir, args.days)
    if not files_seen:
        print(f"No data yet. Expected event logs under: {ddir / 'events'}", file=sys.stderr)
        return 1

    if args.json:
        tools, sessions, days, first, last = summarize_tools(records)
        skills, _, _ = summarize_skills([r for r in records if r.get("type") == "skill_invoke"])
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "data_dir": str(ddir),
            "files": files_seen,
            "total_records": len(records),
            "first_timestamp": fmt_ts(first),
            "last_timestamp": fmt_ts(last),
            "tools": {t: dict(c) for t, c in sorted(tools.items(), key=lambda kv: -kv[1]["total"])},
            "skills": dict(skills.most_common()),
            "days": {d: dict(c) for d, c in sorted(days.items())},
            "sessions": {
                sid: {"day": e["day"], "project_dir": e["project_dir"], "counts": dict(e["counts"])}
                for sid, e in sessions.items()
            },
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    scope = f"last {args.days} days" if args.days is not None else "all time"
    if args.by == "skill":
        skills, first, last = summarize_skills([r for r in records if r.get("type") == "skill_invoke"])
        print(f"Skill invocations ({scope}) — {sum(skills.values())} event(s)")
        print(f"  window: {fmt_ts(first)} .. {fmt_ts(last)}")
        print()
        print_skills(skills)
    else:
        tools, sessions, days, first, last = summarize_tools(records)
        print(f"Tool usage ({scope}) — {len(records)} event(s)")
        print(f"  window: {fmt_ts(first)} .. {fmt_ts(last)}")
        print()
        if args.by == "tool":
            print_tool_table(tools)
        elif args.by == "session":
            print_sessions(sessions)
        else:
            print_days(days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
