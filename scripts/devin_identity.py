#!/usr/bin/env python3
"""Devin account identity (name / email) for the collector.

Source of truth: `devin auth status`, which prints the signed-in Devin
account (name, email, user id). The CLI is optional: when it is missing, not
installed, logged out, or the machine is offline, the identity is null and the
collector keeps working (fail-open).

Platform notes:
- macOS/Linux: the CLI is a symlink at ~/.local/bin/devin pointing into
  ~/.local/share/devin/cli/_versions/<version>/bin/devin.
- Windows: the installer drops devin.exe in %LOCALAPPDATA%\\devin\\cli\\bin and
  adds that directory to the user PATH, so PATH lookup normally succeeds;
  the known install directories are probed as a fallback.

`devin auth status` has no machine-readable output and needs the network to
resolve the account, so the result is cached in the data dir and refreshed
only when it goes stale.
"""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

PLUGIN_NAME = "tools-call-collector"

# Explicit override for the devin executable (empty string disables the lookup).
DEVIN_BIN_ENV = "TOOL_CALL_COLLECTOR_DEVIN_BIN"
# Cache lifetime in seconds; "6h" / "30m" / "90" are all accepted.
IDENTITY_TTL_ENV = "TOOL_CALL_COLLECTOR_IDENTITY_TTL"

DEFAULT_TTL = 6 * 3600
# A failed lookup is cached for much shorter, so a temporary outage does not
# cost one subprocess per tool call, but a fresh login is picked up quickly.
FAILURE_TTL = 60
COMMAND_TIMEOUT = 5.0

FIELDS = ("name", "email", "user_id")


def _ttl():
    raw = os.environ.get(IDENTITY_TTL_ENV)
    if not raw:
        return DEFAULT_TTL
    raw = raw.strip().lower()
    try:
        if raw.endswith("h"):
            return max(0, int(float(raw[:-1]) * 3600))
        if raw.endswith("m"):
            return max(0, int(float(raw[:-1]) * 60))
        return max(0, int(float(raw)))
    except ValueError:
        return DEFAULT_TTL


def _windows_candidates():
    """Known install locations of the Devin CLI on native Windows."""
    out = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        base = Path(local_app_data) / "devin" / "cli"
        out.append(base / "bin" / "devin.exe")
        out.append(base / "bin" / "devin.cmd")
        out.append(Path(local_app_data) / "devin" / "bin" / "devin.exe")
    app_data = os.environ.get("APPDATA")
    if app_data:
        out.append(Path(app_data) / "devin" / "bin" / "devin.exe")
    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        out.append(Path(user_profile) / ".devin" / "bin" / "devin.exe")
    return out


def _unix_candidates():
    """Known install locations of the Devin CLI on macOS/Linux."""
    out = []
    try:
        home = Path.home()
    except (OSError, RuntimeError):
        home = None
    if home is not None:
        out.append(home / ".local" / "bin" / "devin")
        out.append(home / ".local" / "share" / "devin" / "cli" / "_versions" / "current" / "bin" / "devin")
        out.append(home / ".devin" / "bin" / "devin")
        versions = home / ".local" / "share" / "devin" / "cli" / "_versions"
        try:
            for version_dir in sorted(versions.glob("*/bin/devin"), reverse=True):
                out.append(version_dir)
        except OSError:
            pass
    out.append(Path("/usr/local/bin/devin"))
    out.append(Path("/usr/bin/devin"))
    return out


def find_devin():
    """Return the path of the devin executable, or None when not installed."""
    override = os.environ.get(DEVIN_BIN_ENV)
    if override is not None:
        value = override.strip()
        if not value or value.lower() in ("off", "none", "disabled", "0"):
            # Explicitly disabled. Note: an empty value is how POSIX shells
            # spell "off"; on Windows `set VAR=` deletes the variable instead,
            # so the explicit words are accepted as well.
            return None
        candidate = Path(value).expanduser()
        if candidate.is_file():
            return str(candidate)
        found = shutil.which(value)
        return found
    which = shutil.which("devin")
    if which:
        return which
    candidates = _windows_candidates() if os.name == "nt" else _unix_candidates()
    for candidate in candidates:
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        except OSError:
            continue
    return None


def parse_auth_status(text):
    """Extract name / email / user id from `devin auth status` output.

    The output is human-readable; the account lives in a "User:" section:

        User:
          Name:              Ian Yin
          Email:             ian_yin@example.com
          User ID:           waad|AzureAD|...

    When the server cannot be reached the section is replaced by
    "User / team info:" with a failure note and no account lines, which yields
    an empty identity (the caller then falls back).
    """
    name = email = user_id = None
    in_user = False
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        if not raw_line[:1].isspace():
            # A section header at column 0 ends the account section.
            in_user = raw_line.strip() == "User:"
            continue
        if not in_user:
            continue
        key, sep, value = raw_line.strip().partition(":")
        if not sep:
            continue
        value = value.strip()
        if not value:
            continue
        key = key.strip().lower()
        if key == "name":
            name = value
        elif key == "email":
            email = value
        elif key == "user id":
            user_id = value
    return {"name": name, "email": email, "user_id": user_id}


def _run_auth_status(binary):
    """Run `devin auth status`; return its stdout (empty string on failure).

    A non-zero exit code is deliberately ignored: the CLI can print the account
    block and still exit non-zero (a warning on stderr, a partially failed
    team-settings lookup). Whether the identity was actually resolved is
    decided by the parsed content, not by the exit status.
    """
    argv = [binary, "auth", "status"]
    if os.name == "nt" and binary.lower().endswith((".cmd", ".bat")):
        # CreateProcess cannot execute a batch wrapper directly.
        argv = ["cmd", "/c", binary, "auth", "status"]
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=COMMAND_TIMEOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout or ""


def _cache_path(data_dir):
    return Path(data_dir) / "state" / "devin_identity.json"


def _load_cached(path, ttl):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    fetched_at = payload.get("fetched_at")
    if not isinstance(fetched_at, (int, float)):
        return None
    limit = ttl if payload.get("ok") else FAILURE_TTL
    if time.time() - fetched_at > limit:
        return None
    return payload


def _store(path, payload):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def get_identity(data_dir):
    """Return the signed-in Devin account: {name, email, user_id, source, error}.

    `source` is "devin-cli" when the account came from `devin auth status`,
    otherwise None. `error` carries the reason the lookup failed
    ("no-devin-cli", "not-logged-in", "no-account", "lookup-failed").

    Never raises and never blocks for long: the result is cached in the data
    dir, so the common path costs one small file read.
    """
    path = _cache_path(data_dir)
    ttl = _ttl()
    cached = _load_cached(path, ttl)
    if cached is not None:
        return _identity_from(cached)

    binary = find_devin()
    if not binary:
        return _remember_failure(path, "no-devin-cli")

    output = _run_auth_status(binary)
    parsed = parse_auth_status(output)
    if parsed.get("email"):
        payload = dict(parsed)
        payload["ok"] = True
        payload["fetched_at"] = time.time()
        _store(path, payload)
        result = _identity_from(payload)
        result["source"] = "devin-cli"
        return result

    reason = "not-logged-in" if "not logged in" in output.lower() else "no-account"
    return _remember_failure(path, reason)


def _identity_from(payload):
    result = {field: payload.get(field) for field in FIELDS}
    result["source"] = "devin-cli" if payload.get("ok") else None
    result["error"] = None if payload.get("ok") else payload.get("error") or "lookup-failed"
    return result


def _remember_failure(path, reason):
    payload = {field: None for field in FIELDS}
    payload["ok"] = False
    payload["error"] = reason
    payload["fetched_at"] = time.time()
    _store(path, payload)
    result = _identity_from(payload)
    result["source"] = None
    return result

