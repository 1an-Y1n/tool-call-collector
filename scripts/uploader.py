#!/usr/bin/env python3
"""tools-call-collector: uploader.

Sends pending JSONL event lines to one or more configured servers as NDJSON
batches, then atomically advances each server's upload cursor.

Two ways it runs:
- Called from collector.py right after a record is written (direct send):
  one batch per server, short timeout, circuit breaker to protect the hook
  from a down server.
- Standalone (`python3 uploader.py`, optionally `--spawn` for detached runs):
  full drain of all pending data — manual backfill / cron.

Topology: N clients -> 1 server. Each client keeps its own byte cursor per
event file, so a temporarily down/unreachable server simply falls behind and
catches up (full backfill) on the next successful send.

Design rules:
- Fail-open: any error exits 0; the cursor is only advanced after a 2xx
  response, so failed data stays pending.
- Single-flight: a non-blocking flock on state/upload.lock makes concurrent
  senders skip; the running one drains the pending data.
- Batches: up to 500 lines / 256 KB per request.

Server configuration (first match wins):
1. $TOOL_CALL_COLLECTOR_SERVER_URL
2. <data dir>/config.json -> {"server": "..."} (legacy "servers": [...] takes the first)
3. <plugin root>/config.default.json -> {"server": "..."} (shipped factory default:
   fill in the real address when deploying, so installed clients need zero config)

Data location: same as collector.py ($TOOL_CALL_COLLECTOR_DATA_DIR or
~/.devin/tools-call-collector). Cursor file: state/cursors.json.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # non-POSIX platform: run without locking
    fcntl = None

PLUGIN_NAME = "tools-call-collector"
PLUGIN_VERSION = "1.10.0"

SERVER_URL_ENV = "TOOL_CALL_COLLECTOR_SERVER_URL"
DEBUG_ENV = "TOOL_CALL_COLLECTOR_DEBUG"
SEND_TIMEOUT_ENV = "TOOL_CALL_COLLECTOR_SEND_TIMEOUT"

BATCH_MAX_LINES = 500
BATCH_MAX_BYTES = 256 * 1024
DEFAULT_SEND_TIMEOUT = 3.0
BREAKER_COOLDOWN_SECONDS = 60
FULL_DRAIN_MAX_ROUNDS = 20


def data_dir():
    override = os.environ.get("TOOL_CALL_COLLECTOR_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".devin" / PLUGIN_NAME


def send_timeout():
    try:
        return float(os.environ.get(SEND_TIMEOUT_ENV, DEFAULT_SEND_TIMEOUT))
    except ValueError:
        return DEFAULT_SEND_TIMEOUT


def log(message):
    if os.environ.get(DEBUG_ENV):
        state = data_dir() / "state"
        state.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(state / "uploader.log", "a", encoding="utf-8") as f:
            f.write(f"{ts} {message}\n")


def read_server_field(path):
    """Return the raw "server"/"servers" value from a config file, or None."""
    try:
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(config, dict):
        return config.get("server") or config.get("servers")
    return None


def resolve_server():
    """Return the single configured server URL (env > user config > shipped default) or None."""
    raw = os.environ.get(SERVER_URL_ENV)
    if not raw:
        raw = read_server_field(data_dir() / "config.json")
    if not raw:
        raw = read_server_field(Path(__file__).resolve().parent.parent / "config.default.json")
    if isinstance(raw, list):
        raw = raw[0] if raw else None  # legacy list form: take the first
    if isinstance(raw, str):
        return raw.split(",")[0].strip() or None
    return None


def load_cursors(path):
    """Return {event_file: {byte_offset, line_count, uploaded_at}}."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    if isinstance(data.get("cursors"), dict):
        return data["cursors"]  # v1/v3 flat form
    if isinstance(data.get("servers"), dict):  # v2 multi-server form: flatten
        per_server = list(data["servers"].values())
        return per_server[0] if per_server else {}
    return {}


def save_cursors(path, cursors):
    payload = {
        "schema_version": 3,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cursors": cursors,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def breaker_path():
    return data_dir() / "state" / "send-state.json"


def load_breaker():
    try:
        with open(breaker_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_breaker(state):
    try:
        breaker_path().parent.mkdir(parents=True, exist_ok=True)
        tmp = breaker_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, breaker_path())
    except OSError as exc:
        log(f"breaker save failed: {exc}")


def breaker_open():
    return time.time() < load_breaker().get("cooldown_until", 0)


def trip_breaker():
    state = load_breaker()
    state["cooldown_until"] = time.time() + BREAKER_COOLDOWN_SECONDS
    save_breaker(state)
    log(f"breaker open ({BREAKER_COOLDOWN_SECONDS}s)")


def clear_breaker():
    state = load_breaker()
    if "cooldown_until" in state:
        del state["cooldown_until"]
        save_breaker(state)


def read_batch(path, offset):
    """Return (bytes_consumed, [lines]) for the next batch, or (0, []) at EOF."""
    with open(path, "rb") as f:
        f.seek(offset)
        chunk = f.read(BATCH_MAX_BYTES)
    if not chunk:
        return 0, []
    cut = chunk.rfind(b"\n")
    if cut == -1:  # no complete line yet
        return 0, []
    chunk = chunk[: cut + 1]
    lines = [line for line in chunk.decode("utf-8", errors="replace").splitlines() if line.strip()]
    return len(chunk), lines


def upload_batch(url, payload):
    request = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/x-ndjson"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=send_timeout()) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as exc:
        log(f"upload to {url} failed: HTTP {exc.code} {exc.reason}")
        return False
    except Exception as exc:
        log(f"upload to {url} failed: {exc}")
        return False


def drain(ddir, url, cursors, persist, max_rounds):
    """Drain pending lines of the server; cursors is its file->cursor map."""
    events_dir = ddir / "events"
    if not events_dir.is_dir():
        return
    for path in sorted(events_dir.glob("*-calls.jsonl")):
        entry = cursors.get(path.name, {})
        offset = entry.get("byte_offset", 0)
        line_count = entry.get("line_count", 0)
        rounds = 0
        while rounds < max_rounds:
            consumed, lines = read_batch(path, offset)
            if not consumed or not lines:
                break
            if not upload_batch(url, ("\n".join(lines) + "\n").encode("utf-8")):
                trip_breaker()
                break  # fail-open: data stays pending
            clear_breaker()
            offset += consumed
            line_count += len(lines)
            cursors[path.name] = {
                "byte_offset": offset,
                "line_count": line_count,
                "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            }
            persist()
            log(f"uploaded {len(lines)} line(s) from {path.name} (offset -> {offset})")
            rounds += 1
    for name in list(cursors):  # drop cursors for files gone (e.g. rotation)
        if not (events_dir / name).is_file():
            del cursors[name]
    persist()


def run_upload(max_rounds=FULL_DRAIN_MAX_ROUNDS):
    """One upload pass to the configured server. Returns True if a lock was held."""
    url = resolve_server()
    if not url:
        return True  # nothing to do; caller need not care
    ddir = data_dir()
    lock_path = ddir / "state" / "upload.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock:
        if fcntl is not None:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False  # another sender is running; it drains everything
        if breaker_open():
            log("skip: breaker open")
            return True
        cursors_path = ddir / "state" / "cursors.json"
        cursors = load_cursors(cursors_path)

        def persist():
            save_cursors(cursors_path, cursors)

        drain(ddir, url, cursors, persist, max_rounds)
    return True


def main():
    if "--spawn" in sys.argv:  # re-launch detached, for hooks that must return fast
        subprocess.Popen(
            [sys.executable, "-S", "-E", os.path.abspath(__file__)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return 0
    run_upload()
    return 0


if __name__ == "__main__":
    sys.exit(main())
