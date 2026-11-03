#!/usr/bin/env python3
"""tools-call-collector: PostToolUse hook collector.

Reads one hook event (JSON) from stdin and appends a metadata-only record to
a per-category append-only JSONL event log.

Design rules:
- Opt-in by category: nothing is written unless $TOOL_CALL_COLLECTOR_CATEGORIES
  lists at least one category (unset or empty -> no files are created at all).
- Metadata only: never stores tool_input values, tool output, or error text.
- Fail-open: any error exits 0 so the agent session is never blocked.
- Concurrency-safe: appends under an exclusive flock.

Identity fields:
- `user`: the local account name (getpass.getuser()) — who ran the session.
- `name` / `email`: the Devin account, read from `devin auth status` (see
  scripts/devin_identity.py). The Devin CLI is optional: without it, or when
  the machine is offline, both fields are null and the record is still
  written. The lookup is cached in <data dir>/state/devin_identity.json so the
  hook does not pay the CLI startup cost on every tool call.

Data location (first match wins):
1. $TOOL_CALL_COLLECTOR_DATA_DIR
2. ~/.devin/tools-call-collector

Upload: right after writing a record, collector directly sends one batch of
pending lines to the configured server (normally just the record itself;
after failures it also catches up backlog one batch at a time). A short
timeout plus a circuit breaker (60s cooldown after a failure) keep the hook
fast and fail-open. scripts/uploader.py remains available for manual
full-drain backfill. Topology: N clients -> 1 server.

Category config resolution (first match wins):
1. $TOOL_CALL_COLLECTOR_CATEGORIES (environment variable; set-but-empty = explicitly off)
2. <data dir>/config.json  -> {"categories": [...]}   (user file, survives plugin updates)
3. <plugin root>/config.default.json -> {"categories": [...]}   (shipped factory default)
4. Built-in fallback: off (no files are created)

Category config ($TOOL_CALL_COLLECTOR_CATEGORIES or the config files, comma/whitespace separated):
  file, search, shell, web, planning, skill, subagent, permission,
  mcp-management, mcp, other
      -> one file per category: events/<category>-calls.jsonl
  all -> generic file: events/tool-calls.jsonl (every tool call)

Each tool call is written to at most one file: its own category file when that
category is configured, otherwise the generic file when "all" is configured,
otherwise it is discarded. Unknown category names in the config are ignored.

Skill records (events/skill-calls.jsonl) additionally carry the skill name
(`skill`), the operation mode (`skill_command`), and `status`; only real
invocations are recorded — list/search browsing is skipped. Every other file
stays metadata-only.
"""

import getpass
import json
import os
import socket
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # non-POSIX platform: append without locking
    fcntl = None

PLUGIN_NAME = "tools-call-collector"
PLUGIN_VERSION = "1.11.0"
SCHEMA_VERSION = 4

CATEGORIES_ENV = "TOOL_CALL_COLLECTOR_CATEGORIES"


def _prefix(p):
    return lambda name: name.startswith(p)


# All writable categories, in match order (first match wins; "other" must stay last).
# A category maps either to a set of exact tool names or to a prefix matcher.
CATEGORY_MATCHERS = {
    "file": {"read", "write", "edit", "apply_patch", "notebook_read", "notebook_edit"},
    "search": {"grep", "glob"},
    "shell": {"exec", "get_output", "write_to_process", "kill_shell"},
    "web": {"webfetch"},
    "planning": {"todo_write", "exit_plan_mode"},
    "skill": {"skill"},
    "subagent": {"run_subagent", "read_subagent"},
    "permission": {"request_scope"},
    "mcp-management": {"mcp_list_servers", "mcp_list_tools", "mcp_call_tool", "mcp_read_resource"},
    "mcp": _prefix("mcp__"),      # MCP server tools: mcp__<server>__<tool>
    "other": lambda name: True,   # fallback: anything unclassified
}


def data_dir():
    override = os.environ.get("TOOL_CALL_COLLECTOR_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".devin" / PLUGIN_NAME


def plugin_root():
    # collector.py lives in <plugin root>/scripts/
    return Path(__file__).resolve().parent.parent


def send_pending():
    """Direct send: one batch per configured server (fail-open, never raises)."""
    try:
        import uploader  # same scripts/ directory
        uploader.run_upload(max_rounds=1)
    except Exception as exc:  # fail-open
        print(f"{PLUGIN_NAME}: send failed: {exc}", file=sys.stderr)


def read_categories_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    categories = config.get("categories") if isinstance(config, dict) else None
    if not isinstance(categories, list):
        return None
    return ",".join(c for c in categories if isinstance(c, str))


def resolve_categories(ddir=None, proot=None):
    """Return (categories, source) following the layered resolution order."""
    ddir = ddir or data_dir()
    proot = proot or plugin_root()
    env = os.environ.get(CATEGORIES_ENV)
    if env is not None:
        return parse_categories(env), "env"
    user_file = read_categories_file(Path(ddir) / "config.json")
    if user_file is not None:
        return parse_categories(user_file), "config.json"
    default_file = read_categories_file(Path(proot) / "config.default.json")
    if default_file is not None:
        return parse_categories(default_file), "config.default.json"
    return set(), "off"


def parse_categories(raw):
    if not raw:
        return set()
    return {part.strip().lower() for part in raw.replace(",", " ").split() if part.strip()}


def categorize(tool_name):
    for category, matcher in CATEGORY_MATCHERS.items():
        if matcher(tool_name) if callable(matcher) else tool_name in matcher:
            return category
    return "other"  # safety net; "other" in CATEGORY_MATCHERS already catches all


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def derive_status(tool_response):
    if not isinstance(tool_response, dict):
        return "unknown"
    if tool_response.get("success") is True:
        return "success"
    if tool_response.get("success") is False or tool_response.get("error") is not None:
        return "error"
    return "unknown"


def client_id():
    """Stable per-installation ID (generated once, persisted in the data dir)."""
    path = data_dir() / "state" / "client_id"
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    new_id = str(uuid.uuid4())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")  # unique per process: no rename races
        tmp.write_text(new_id, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:  # fail-open
        print(f"{PLUGIN_NAME}: client_id generation failed: {exc}", file=sys.stderr)
    try:  # return the persisted winner so racing first-writes agree on one id
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    return new_id


def current_user():
    """Linux/Unix account name; never raises (fail-open)."""
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER") or os.environ.get("LOGNAME")


def devin_account():
    """Devin account identity (name/email) from `devin auth status`.

    Returns {"name", "email", "user_id", "source", "error"}. `error` is set
    when the account could not be resolved (CLI missing, logged out, offline).
    """
    try:
        import devin_identity  # same scripts/ directory

        return devin_identity.get_identity(data_dir())
    except Exception as exc:  # fail-open: never block the agent
        return {"name": None, "email": None, "user_id": None,
                "source": None, "error": f"lookup-failed ({exc})"}


def base_fields(event, account):
    return {
        "schema_version": SCHEMA_VERSION,
        "timestamp": utc_now_iso(),
        "client_id": client_id(),
        "user": current_user(),
        "name": account.get("name"),
        "email": account.get("email"),
        "session_id": event.get("session_id"),
        "prompt_id": event.get("prompt_id"),
        "project_dir": os.environ.get("DEVIN_PROJECT_DIR") or os.environ.get("CLAUDE_PROJECT_DIR"),
        "host": socket.gethostname(),
        "collector": {"name": PLUGIN_NAME, "version": PLUGIN_VERSION},
    }


def build_record(event, account):
    tool_input = event.get("tool_input")
    record = base_fields(event, account)
    record.update({
        "type": "tool_call",
        "tool_name": event.get("tool_name"),
        "status": derive_status(event.get("tool_response")),
        "input_keys": sorted(tool_input.keys()) if isinstance(tool_input, dict) else [],
    })
    return record


def skill_info(event):
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return {"skill": None, "skill_command": None}
    command = tool_input.get("command")
    command = command if isinstance(command, str) else None
    name = tool_input.get("skill")
    name = name if isinstance(name, str) and name else None
    return {"skill": name, "skill_command": command}


def build_skill_record(event, info, account):
    record = base_fields(event, account)
    record.update({"type": "skill_invoke", "status": derive_status(event.get("tool_response"))})
    record.update(info)
    return record


def append_record(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.write(line)
        f.flush()
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


IDENTITY_REASONS = {
    "no-devin-cli": ("Devin CLI not found",
                     "install the Devin CLI (Linux/macOS: "
                     "`curl -fsSL https://cli.devin.ai/install.sh | bash`; "
                     "Windows PowerShell: "
                     "`irm https://static.devin.ai/cli/setup.ps1 | iex`) "
                     "and run `devin auth login`"),
    "not-logged-in": ("not logged in to Devin", "run `devin auth login`"),
    "no-account": ("no Devin account in `devin auth status`",
                   "run `devin auth login`"),
}


def identity_error_message(reason):
    """One-line explanation for the user.

    ASCII only on purpose: the message travels through a Windows console
    (OEM code page) as well as through the harness, and a non-ASCII
    character would be mangled there.
    """
    detail, hint = IDENTITY_REASONS.get(reason, (reason or "unknown reason", ""))
    return (f"{PLUGIN_NAME}: {detail} - this plugin cannot be used without a "
            f"Devin account; nothing was collected or uploaded. To fix it: {hint}.")


def identity_error_short(reason):
    """The essential part only - this is the line the harness shows."""
    detail = IDENTITY_REASONS.get(reason, (reason or "unknown reason", ""))[0]
    return (f"{PLUGIN_NAME}: {detail} - this plugin cannot be used without a "
            "Devin account; nothing was collected or uploaded.")


def warning_marker_path():
    return data_dir() / "state" / "identity_warning.json"


def clear_identity_warning():
    """Forget the "already warned" marker so a later failure warns again."""
    try:
        warning_marker_path().unlink()
    except OSError:
        pass


def warn_missing_identity(event, account):
    """Report the missing Devin account to the user, once per session.

    Two channels, because the two harnesses surface things differently:

    - stdout JSON: `systemMessage` is shown to the user as a warning in Claude
      Code; `hookSpecificOutput.additionalContext` puts the same sentence in
      the agent's context in both harnesses (the agent can then mention it).
    - stderr: the plain one-line message, for logs and for harnesses that
      surface hook stderr.

    The hook still exits 0: the hook command is an interpreter-fallback chain
    (`python3 ... || python ...`), and a non-zero exit would make the shell
    try every remaining branch — re-running the collector several times per
    tool call. Exit 0 stops the chain at the first working interpreter.
    """
    reason = account.get("error") or "lookup-failed"
    session_id = event.get("session_id")
    marker = warning_marker_path()
    seen = None
    try:
        seen = json.loads(marker.read_text(encoding="utf-8")).get("session_id")
    except (OSError, ValueError):
        seen = None
    if session_id is not None and seen == session_id:
        return  # already reported for this session
    message = identity_error_message(reason)
    payload = {
        "systemMessage": message,
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": message,
        },
    }
    try:
        # stderr carries the short form: the first line is what Claude Code
        # puts into its hook-error notice.
        print(identity_error_short(reason), file=sys.stderr)
        print(json.dumps(payload, ensure_ascii=False), flush=True)
    except Exception:
        # Cannot talk to the user -> stay silent and let the caller exit.
        return
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        tmp = marker.with_name(f"{marker.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"session_id": session_id, "warned_at": utc_now_iso()}),
                       encoding="utf-8")
        os.replace(tmp, marker)
    except OSError:
        pass


def main():
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
        if not isinstance(event, dict):
            raise ValueError("hook payload is not a JSON object")
        tool_name = event.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            return 0  # not a tool event; nothing to count
        categories, _source = resolve_categories()
        if not categories:
            return 0  # collection disabled: no files are created
        account = devin_account()
        if not account.get("email"):
            # No Devin account -> the record cannot be attributed to a person.
            # Do not write and do not upload; tell the user instead.
            warn_missing_identity(event, account)
            return 0
        clear_identity_warning()  # the account is back: warn again if it breaks
        category = categorize(tool_name)
        events_dir = data_dir() / "events"
        wrote = False
        if category in categories:
            if category == "skill":
                info = skill_info(event)
                if info["skill_command"] == "invoke" or info["skill"]:
                    # real invocation only; list/search browsing is not recorded
                    append_record(events_dir / "skill-calls.jsonl", build_skill_record(event, info, account))
                    wrote = True
            else:
                append_record(events_dir / f"{category}-calls.jsonl", build_record(event, account))
                wrote = True
        elif "all" in categories:
            append_record(events_dir / "tool-calls.jsonl", build_record(event, account))
            wrote = True
        if wrote:
            send_pending()
    except Exception as exc:  # fail-open: never block the agent
        print(f"{PLUGIN_NAME}: collector error: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
