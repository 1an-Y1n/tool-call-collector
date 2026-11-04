# tools-call-collector

Collect devin tool calling.

A plugin conforming to the [Agent Plugins Specification 1.0.0](https://github.com/agentplugins/agent-plugins-spec):
it collects how often the agent uses each tool via lifecycle hooks, stores the data
locally first (append-only JSONL event logs), and the data format is designed for
being uploaded to a server later.

Design details: [DESIGN_EN.md](DESIGN_EN.md) (English) / [DESIGN.md](DESIGN.md) (Chinese).

## How it works

```text
agent calls a tool ──► PostToolUse hook ──► scripts/collector.py ──┬─► events/<category>-calls.jsonl (per category)
                                                                   │
                                 $TOOL_CALL_COLLECTOR_CATEGORIES ┘  (nothing is written when unset)
                                                   │
                                                   └── any failure exits 0 (fail-open, never blocks the session)
```

- The hook event (`PostToolUse`, empty matcher = all tools) is passed to the collector
  as JSON on stdin.
- The collector extracts **metadata only** (tool name, success/failure, input field
  names) — it never records parameter values, tool output, or error text, so sensitive
  data (file contents, commands, etc.) never lands on disk.
- **Off by default**: when `TOOL_CALL_COLLECTOR_CATEGORIES` is unset or empty, no data
  files are generated; each configured category gets its own event file (see
  [Configuration](#configuration)).
- Every call **appends one line** to a JSONL file (`flock` for concurrency safety) —
  no read-modify-write, no full-file rewrites.

## Layout

```text
tools-call-collector/
├── plugin.json                      # Agent Plugins 1.0.0 manifest (Devin CLI)
├── hooks.json                       # lifecycle hook registration for the Devin CLI (client extension, see "Spec conformance")
├── .claude-plugin/
│   ├── marketplace.json             # Claude Code marketplace manifest
│   └── plugin.json                  # Claude Code plugin manifest
├── hooks/
│   └── hooks.json                   # Claude Code lifecycle hook registration
├── config.default.json              # factory-default category config (shipped with the plugin)
├── scripts/
│   ├── collector.py                 # PostToolUse collector (stdlib only)
│   ├── devin_identity.py            # Devin account identity via `devin auth status` (stdlib only)
│   ├── uploader.py                  # background NDJSON uploader (stdlib only)
│   └── stats.py                     # local aggregation (read-only)
├── LICENSE
└── README.md
```

The plugin carries two sets of manifests because the two harnesses disagree on
where they belong: the Devin CLI reads `plugin.json` and `hooks.json` from the
plugin root, while Claude Code reads `.claude-plugin/plugin.json` and
`hooks/hooks.json`. The collector itself is shared and works in both.

## Requirements

The plugin runs inside the Devin CLI and takes the person from that CLI's
signed-in account, so all of this must hold **both when you install it and
later, in the shell that runs the hooks**:

| Requirement | Why it is needed | Check |
| --- | --- | --- |
| **Devin CLI** on `PATH` | it installs and updates the plugin itself (`devin plugins install`) **and** attributes every event to a person via `devin auth status` | `devin --version` |
| **Signed in to Devin** | the account supplies `name` / `email`; with no account the collector writes nothing | `devin auth status` → a `User:` block containing an `Email:` line |
| **Python 3.9+** as `python` or `python3` | the hook starts the collector as a subprocess on every tool call | `python3 --version` |

No `pip install`, no system packages, no build step.

### The CLI is checked at runtime, not at install time

The Agent Plugins format has **no install-time hook**: a plugin cannot run a
command when it is installed — [spec §7](https://github.com/agentplugins/agent-plugins-spec)
defines exactly two component types, skills and MCP servers, and everything
else is out of the portable format. So the CLI and the login **cannot** be
verified during installation, and these situations still leave the plugin
without an account afterwards:

- the CLI was removed, or replaced by another installation (`PATH` changed)
- the shell that runs the hook sees a different `PATH` than the login shell
- **Devin Desktop** bundles its own CLI that is not on `PATH` — explicitly out
  of scope, the collector will not find it
- the CLI is present but logged out, or the account is no longer valid

In every one of those cases the collector **fails open**: it writes nothing,
uploads nothing, and prints one warning per session so the user can fix it —
`Devin CLI not found` (carrying the install command for Linux/macOS and
Windows) or `not logged in to Devin`. Full text and both output channels:
[Devin identity](#devin-identity-name--email).

## Installation

### Claude Code

The Claude Code layout lives on the `claudecode` branch of this repository.

```bash
# 1. Register the repository as a marketplace, then install the plugin from it
claude plugin marketplace add git@github.com:1an-Y1n/tool-call-collector.git
claude plugin install tool-call-collector@tool-call-collector

# 2. Verify the plugin is registered
claude plugin list

# 3. Verify attribution, after one tool call in a session: the cached identity
#    must carry an email, otherwise no event can be attributed to a person
cat ~/.devin/tools-call-collector/state/devin_identity.json
```

Claude Code reports a failed tool call through a separate `PostToolUseFailure`
event rather than through `PostToolUse`, so `hooks/hooks.json` registers both;
otherwise failed calls would never be counted.

### Devin CLI

```bash
# 0. Prerequisites — see Requirements above
devin --version
devin auth status          # must show the User: block with an Email: line
python3 --version

# 1. Install (Local development links this folder; edits take effect immediately)
devin plugins install --local ./tools-call-collector

# 2. Verify the plugin is registered
devin plugins list
devin plugins info tools-call-collector

# 3. Verify attribution, after one tool call in a session: the cached identity
#    must carry an email, otherwise no event can be attributed to a person
cat ~/.devin/tools-call-collector/state/devin_identity.json
#    (Windows: %USERPROFILE%\.devin\tools-call-collector\state\devin_identity.json)
```

## Configuration

### `TOOL_CALL_COLLECTOR_CATEGORIES` (collection switch)

Plugin installation cannot write environment variables, so the category config is
resolved in layers — **first match wins**:

1. `$TOOL_CALL_COLLECTOR_CATEGORIES` — environment variable; **set-but-empty means
   explicitly off** (a way to disable without uninstalling)
2. `<data dir>/config.json` — user config file, `{"categories": [...]}`; lives in the
   persistent data directory, so it survives plugin updates
3. `<plugin root>/config.default.json` — factory default shipped with the plugin
   (currently `["all", "skill"]`), so collection works **with zero user configuration**
4. Built-in fallback: off (no files are generated)

The resolved value is a comma- or whitespace-separated category list; each configured
category generates its own event file:

| Category           | tool_name included                                                              | Event file                                         |
| ------------------ | ------------------------------------------------------------------------------- | -------------------------------------------------- |
| `file`           | `read` `write` `edit` `apply_patch` `notebook_read` `notebook_edit` | `file-calls.jsonl`                               |
| `search`         | `grep` `glob`                                                               | `search-calls.jsonl`                             |
| `shell`          | `exec` `get_output` `write_to_process` `kill_shell`                     | `shell-calls.jsonl`                              |
| `web`            | `webfetch`                                                                    | `web-calls.jsonl`                                |
| `planning`       | `todo_write` `exit_plan_mode`                                               | `planning-calls.jsonl`                           |
| `skill`          | `skill`                                                                       | `skill-calls.jsonl` (with skill name, below)     |
| `subagent`       | `run_subagent` `read_subagent`                                              | `subagent-calls.jsonl`                           |
| `permission`     | `request_scope`                                                               | `permission-calls.jsonl`                         |
| `mcp-management` | `mcp_list_servers` `mcp_list_tools` `mcp_call_tool` `mcp_read_resource` | `mcp-management-calls.jsonl`                     |
| `mcp`            | `mcp__<server>__<tool>` (MCP server tools)                                    | `mcp-calls.jsonl`                                |
| `other`          | anything unclassified (e.g. newer tools like `ask_user_question`)             | `other-calls.jsonl`                              |
| `all`            | every tool                                                                      | `tool-calls.jsonl` (generic file, metadata only) |

Rules:

- A call is **written to at most one file**: if its specific category is configured →
  that category's file; otherwise if `all` is configured → `tool-calls.jsonl`;
  otherwise it is discarded. (With `all,skill`, skill calls go only to
  `skill-calls.jsonl` and everything else to `tool-calls.jsonl`.)
- Unknown category names in the config are ignored (no error, no blocking).
- The environment variable must be exported in the **shell that launches devin** (hook
  processes inherit the session environment); to change the default persistently
  instead, edit `<data dir>/config.json`:

```bash
# Environment variable (top layer, per-shell):
export TOOL_CALL_COLLECTOR_CATEGORIES=skill,shell

# Persistent user config (second layer): ~/.devin/tools-call-collector/config.json
# {"schema_version": 1, "categories": ["skill", "shell"]}

# Factory default (third layer, shipped as config.default.json):
# {"schema_version": 1, "categories": ["all", "skill"]}
```

### `TOOL_CALL_COLLECTOR_DATA_DIR` (data directory override, optional)

Defaults to `~/.devin/tools-call-collector`; setting it overrides the default.

## Data format

### Location

First match wins:

1. `$TOOL_CALL_COLLECTOR_DATA_DIR` (environment variable override)
2. `~/.devin/tools-call-collector` (default)

```text
~/.devin/tools-call-collector/
├── events/
│   ├── tool-calls.jsonl          # generated only when "all" is configured: every tool call (metadata only)
│   ├── skill-calls.jsonl         # generated only when "skill" is configured: skill calls (with skill name)
│   ├── shell-calls.jsonl         # generated only when "shell" is configured: … one file per category
│   └── …                         # <category>-calls.jsonl, generated per config
└── state/                        # upload cursors (cursors.json) + upload.lock
                                  # + client_id + cached Devin identity (devin_identity.json)
```

Event files are **generated dynamically from the config**: unconfigured categories have
no file, and with an empty config the `events/` directory itself is never created.

### Record schema (`schema_version: 4`)

One self-contained JSON object per line (NDJSON), with fields:

| Field              | Type           | Description                                                                                                                 |
| ------------------ | -------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `schema_version` | int            | Record format version, currently `4`; bump it when fields evolve                                                          |
| `type`           | string         | Event type, currently `"tool_call"`                                                                                       |
| `timestamp`      | string         | UTC ISO 8601, microsecond precision, `Z` suffix                                                                           |
| `client_id`      | string \| null | Stable per-installation ID (UUID, generated once in the data dir) — distinguishes different machines/clients on the server |
| `user`           | string \| null | Linux account name of the session owner (`getpass.getuser()`)                                                             |
| `name`           | string \| null | Devin account display name (e.g. `Ian Yin`), from `devin auth status`                                                      |
| `email`          | string \| null | Devin account email (e.g. `ian_yin@example.com`), from `devin auth status`                                                 |
| `session_id`     | string \| null | Session ID (shared by all hook events within a session)                                                                     |
| `prompt_id`      | string \| null | Turn ID (rotated on every user prompt)                                                                                      |
| `project_dir`    | string \| null | Project root of the session that triggered the call                                                                         |
| `host`           | string         | Machine name (human-readable hint; `client_id` is the reliable key)                                                       |
| `tool_name`      | string         | Tool name, e.g. `exec`, `edit`, `mcp__github__create_issue`                                                           |
| `status`         | string         | `success` / `error` / `unknown`                                                                                       |
| `input_keys`     | string[]       | Field names of `tool_input` (key names only, never values)                                                                |
| `collector`      | object         | `{ "name": "tools-call-collector", "version": "1.12.0" }`                                                                 |

Example (one line):

```json
{"schema_version":4,"type":"tool_call","timestamp":"2026-10-14T07:31:22.123456Z","client_id":"2d1dcb0d-6bcf-494a-9a8e-e02faaa4b8f7","user":"ian_yin","name":"Ian Yin","email":"ian_yin@example.com","session_id":"3f8d1c2a-…","prompt_id":"b71e9d40-…","project_dir":"/home/ian_yin/devin-tools","host":"devbox","tool_name":"exec","status":"success","input_keys":["command","timeout"],"collector":{"name":"tools-call-collector","version":"1.12.0"}}
```

### Devin identity (`name` / `email`)

`name` and `email` come from the Devin CLI: the collector runs
`devin auth status` and reads the account block of its output.

**Supported setup: the Devin CLI on Linux, macOS or Windows, signed in to
Devin.** (Devin Desktop is out of scope — it bundles a CLI of its own that is
not on the `PATH`, so the collector does not find it.)

**The Devin account is required.** Without it the collector cannot attribute a
record to a person, so it **writes nothing and uploads nothing** and tells the
user instead. Two distinct messages:

```
tools-call-collector: Devin CLI not found - this plugin cannot be used without a
Devin account; nothing was collected or uploaded.
```
```
tools-call-collector: not logged in to Devin - this plugin cannot be used without a
Devin account; nothing was collected or uploaded.
```

The message goes out on two channels:

| Channel | Where it shows up |
| ------- | ----------------- |
| stderr | the harness's hook log / console — the **short** form, because that is the first line Claude Code puts into its hook-error notice |
| stdout JSON | `systemMessage` → a warning shown to the user in Claude Code; `hookSpecificOutput.additionalContext` → the same sentence in the agent's context in both harnesses. Carries the **full** text, including how to fix it: the install command for Linux/macOS and Windows, or `devin auth login` |

If neither channel works (e.g. stdout is closed), the collector simply exits —
silently, and still without collecting anything.

The report happens **once per session** (`<data dir>/state/identity_warning.json`
remembers the session id), and the marker is cleared as soon as the account is
resolved again, so a later logout warns once more. The hook exits 0 in this
state: the hook command is an interpreter-fallback chain
(`python3 … || python …`), and a non-zero exit would make the shell try every
remaining branch — re-running the collector several times per tool call.

Details of the lookup:

- **Cached.** The CLI needs ~0.9 s to answer and the hook fires on *every*
  tool call, so the result is cached in `<data dir>/state/devin_identity.json`
  and refreshed only when it goes stale (6 h by default). A *failed* lookup is
  cached for only 60 s, so a fresh `devin auth login` is picked up quickly
  without costing a CLI call per tool call during an outage.
- **The exit code does not decide.** `devin auth status` can print the account
  block *and* exit non-zero (a warning on stderr, a partially failed team
  lookup). The identity is therefore taken from the parsed output, never from
  the process status.
- **No offline path.** The account is resolved by the server
  (`server.codeium.com`); the CLI stores no local copy of it (its credential
  file holds a session token whose payload carries only a session id). Offline,
  the CLI prints `User / team info: Failed to fetch from server` and no
  account — so an offline machine collects nothing.

| Environment variable | Default | Meaning |
| -------------------- | ------- | ------- |
| `TOOL_CALL_COLLECTOR_DEVIN_BIN` | (auto-detected) | Path to the `devin` executable. Set it to `off` / `none` / `disabled` (or to an empty string on POSIX shells) to disable the lookup — the collector then behaves as if no account were available |
| `TOOL_CALL_COLLECTOR_IDENTITY_TTL` | `6h` | How long a cached identity stays valid (`6h`, `30m`, or plain seconds) |

> On Windows, `set VAR=` *deletes* the variable instead of setting it empty, so
> the explicit word `off` is the reliable way to disable the lookup there.

Where the executable is looked for, in order:

1. `TOOL_CALL_COLLECTOR_DEVIN_BIN` (or `devin` on the `PATH`)
2. Windows: `%LOCALAPPDATA%\devin\cli\bin\devin.exe`,
   `%LOCALAPPDATA%\devin\bin\devin.exe`, `%APPDATA%\devin\bin\devin.exe`,
   `%USERPROFILE%\.devin\bin\devin.exe`
3. macOS/Linux: `~/.local/bin/devin`,
   `~/.local/share/devin/cli/_versions/<version>/bin/devin`,
   `~/.devin/bin/devin`, `/usr/local/bin/devin`, `/usr/bin/devin`

`shutil.which` is used for the `PATH` lookup, so a `devin.cmd` / `devin.bat`
wrapper is found as well (a `.cmd` is executed through `cmd /c`).

`devin auth status` is available on native Windows as well (same CLI, same
subcommand); only the *sandbox* is WSL-only, which this collector does not
use.

### Skill invocation records (`events/skill-calls.jsonl`, `type: "skill_invoke"`)

When the `skill` category is configured, **only real invocations** are written to this
file (`command == "invoke"` or an identifiable skill name); `list`/`search` browsing is
not recorded. Compared with other files, skill records additionally carry the skill
name and the operation mode (both are identifiers, so this does not violate the
metadata-only constraint):

| Field             | Type           | Description                                                                                                                                |
| ----------------- | -------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| (common fields)   | —             | Same as above: `schema_version`/`timestamp`/`client_id`/`user`/`name`/`email`/`session_id`/`prompt_id`/`project_dir`/`host`/`collector` |
| `type`          | string         | Always `"skill_invoke"`                                                                                                                  |
| `status`        | string         | `success` / `error` / `unknown`                                                                                                      |
| `skill`         | string \| null | The invoked skill name (e.g. `tools-call-collector:tool-call-stats`); null only when an invocation carries no identifiable name          |
| `skill_command` | string \| null | Operation mode of the skill tool: `invoke` / `list` / `search` etc.                                                                  |

In statistics (`stats.py --by skill`) records are grouped by `skill` name; an
invocation with no identifiable name shows up as `<unidentified>`.

Example (one line):

```json
{"schema_version":4,"type":"skill_invoke","timestamp":"2026-10-14T07:31:22.123456Z","client_id":"2d1dcb0d-6bcf-494a-9a8e-e02faaa4b8f7","user":"ian_yin","name":"Ian Yin","email":"ian_yin@example.com","session_id":"3f8d1c2a-…","prompt_id":"b71e9d40-…","project_dir":"/home/ian_yin/devin-tools","host":"devbox","skill":"tools-call-collector:tool-call-stats","skill_command":"invoke","collector":{"name":"tools-call-collector","version":"1.12.0"}}
```

### Why JSONL (upload-oriented design)

- **Append-only writes**: the hook only does a single-line `O_APPEND` write; a crash or
  power loss loses at most the last line and never corrupts existing data.
- **One line = one self-contained record**: uploads batch lines and POST them as NDJSON
  (the standard streaming-ingest format); the server can parse and ingest line by line
  without parsing a whole package.
- **`schema_version` embedded**: when fields evolve, the server parses mixed old/new
  data version-compatibly.
- **Never rewritten in place**: the local file is naturally an "outbound queue";
  upload progress is expressed with a cursor file (below).

### Uploading to a server (direct send, since v1.7.0)

**Protocol**: HTTPS (or HTTP for localhost) + NDJSON batch `POST` — the request body is
the raw JSONL lines, `Content-Type: application/x-ndjson`; the server ingests line by
line. The client advances its cursor only after a 2xx response, so failures retry
naturally. No authentication.

**Trigger — direct send in the hook** (no background processes, no SessionEnd hook):

1. After each recorded call, `collector.py` **writes the record to the file first**
   (durable), then **directly sends one batch per configured server** — normally just
   this record; if earlier sends failed, it also catches up backlog one batch per call
   (500 lines / 256 KB per request).
2. **Circuit breaker**: a failed send puts that server on a 60 s cooldown (per server);
   during cooldown it is skipped entirely, its data stays pending, and the next call
   after cooldown retries and catches up. Healthy servers are never affected.
3. **Short timeout**: 3 s per request (`TOOL_CALL_COLLECTOR_SEND_TIMEOUT` to override);
   a refused connection fails instantly, so a down server costs ~0 extra time, while a
   black-holed host costs at most one timeout per cooldown window.
4. `scripts/uploader.py` remains for **manual full-drain backfill** (`python3 scripts/uploader.py`, or cron); it shares the same lock, cursors, and breaker.

**Latency** (measured): direct send adds ~60 ms per call for 2 localhost servers
(~30 ms each); over a WAN expect ~100–300 ms per call — the deliberate price of
per-entry immediacy. The file is written *before* sending, so durability is never at
risk.

**Cursor** — `state/cursors.json`, one byte offset per event file:

```json
{
  "schema_version": 3,
  "updated_at": "2026-10-15T06:10:00Z",
  "cursors": {
    "tool-calls.jsonl": {"byte_offset": 37942, "line_count": 106, "uploaded_at": "2026-10-15T06:10:00Z"}
  }
}
```

Semantics: every byte before `byte_offset` in that file has been uploaded. A
temporarily unreachable server falls behind silently and catches up (full backfill
from its own cursor) on the next successful send. The cursor is advanced atomically
(temp file + `rename`) after each accepted batch; entries for files that no longer
exist (e.g. after rotation) are dropped. Older cursor formats (v1 flat, v2
multi-server) are migrated automatically.

**Server configuration** — the topology is **N clients → 1 server** (each client is
distinguished by its `client_id`). Resolved in layers, first match wins:

1. `$TOOL_CALL_COLLECTOR_SERVER_URL` — environment variable (temporary override)
2. `<data dir>/config.json` → `{"server": "https://…/v1/events"}` (per-machine
   persistent override; a legacy `"servers": […]` list takes its first entry)
3. `<plugin root>/config.default.json` → `{"server": "https://…/v1/events"}` —
   **shipped factory default**: fill in the real address when deploying your own
   plugin, so installed clients need zero configuration

Credentials are never shipped inside the plugin package (spec requirement) — the
server URL is not a credential and may be shipped for own-plugin deployments. No
authentication: the reference backend is unauthenticated — bind it to localhost or
put it behind your own gateway/firewall. With no server configured anywhere, nothing
is sent and data simply accumulates locally.

**Reference backend**: [`../tools-call-server`](../tools-call-server) — FastAPI +
SQLite (`POST /v1/events` ingest, `GET /v1/stats` aggregation, `GET /health`).
Malformed lines are quarantined server-side (raw preserved), so the client cursor can
always advance safely.

## Viewing statistics

Run directly (read-only, never modifies data). `stats.py` aggregates **all**
`*-calls.jsonl` files under `events/` (regardless of which categories are configured):

```bash
python3 <plugin dir>/scripts/stats.py              # per-tool totals (across all event files)
python3 <plugin dir>/scripts/stats.py --days 7     # last 7 days
python3 <plugin dir>/scripts/stats.py --by skill   # skill invocation stats (grouped by skill name)
python3 <plugin dir>/scripts/stats.py --by session # per-session breakdown
python3 <plugin dir>/scripts/stats.py --by day     # per-day breakdown
python3 <plugin dir>/scripts/stats.py --json       # machine-readable output
python3 <plugin dir>/scripts/stats.py --info       # current config + size/line count of each event file
```

## Platform support (Linux / macOS / Windows)

The collector is pure-stdlib Python 3 and runs on all three platforms. What differs
between them is **how the hook command is executed** and **what the Python interpreter
is called**.

### The hook command

`hooks.json` runs one shell command per tool call. It is written as a chain of
alternatives so that it works in every shell the two harnesses use:

```
python3 -S -E "${CLAUDE_PLUGIN_ROOT}/scripts/collector.py" || python -S -E "%CLAUDE_PLUGIN_ROOT%/scripts/collector.py" || …
```

The chain is ordered by **which shell is most likely to run it** *and* by
**what a failed branch costs**:

| Shell | Winning branch | Cost of the branches before it |
| ----- | -------------- | ------------------------------ |
| `sh -c` (Devin CLI and Claude Code on Linux/macOS) | `${CLAUDE_PLUGIN_ROOT}` + `python3` | none — first branch |
| Git Bash (Claude Code on Windows with Git for Windows) | `${CLAUDE_PLUGIN_ROOT}` + `python3` | none — first branch |
| `cmd.exe`, harness **substitutes** the path textually | `${CLAUDE_PLUGIN_ROOT}` + `python3` | none — first branch |
| `cmd.exe`, harness **exports** the variable | `%CLAUDE_PLUGIN_ROOT%` + `python` | one wasted interpreter start |
| `cmd.exe`, only `PLUGIN_ROOT` exported | `%PLUGIN_ROOT%` + `python` | up to three wasted starts |

Three details make this work:

- **Interpreter name.** `python3` is the Linux/macOS name; the Windows python.org
  installer provides `python.exe` and the `py` launcher, but **not** `python3.exe`.
  Both spellings are tried.
- **Path syntax.** POSIX shells expand `${VAR}`; `cmd.exe` expands `%VAR%` — and
  *only* `%VAR%` (a `${VAR}` in `cmd.exe` stays literal text, verified). The
  plugin root is therefore referenced through several spellings: `${CLAUDE_PLUGIN_ROOT}`,
  `%CLAUDE_PLUGIN_ROOT%`, `${PLUGIN_ROOT}`, `%PLUGIN_ROOT%`. Both harnesses know
  `CLAUDE_PLUGIN_ROOT` and `PLUGIN_ROOT`; the Devin CLI has **no** `DEVIN_PLUGIN_ROOT`
  (its binary only mentions `CLAUDE_PROJECT_DIR`, `CLAUDE_PLUGIN_ROOT`, `PLUGIN_ROOT`,
  `PLUGIN_DATA`), so a `${DEVIN_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT}}` branch is kept
  last, purely for compatibility with anything that does set it.
- **Failure cost.** "command not found" is a shell-level error and nearly free;
  "can't open file" requires starting the interpreter first. The branch order
  therefore puts the *likely* interpreter first, so a wrong guess costs a cheap
  shell error rather than a full interpreter start.

Measured on a Windows 11 host (`cmd.exe`, Python 3.13 at `C:\Python313`):

| Scenario | Time |
| -------- | ---- |
| collector, identity cached | 0.27 s |
| collector, cold (identity lookup + upload attempt) | 2.4 s — the upload to an unreachable server, not the identity lookup; the circuit breaker suppresses it for the next 60 s |

### Known limitation: PowerShell 5.1

Claude Code runs shell-form hooks in **PowerShell** on Windows when Git for Windows
is *not* installed. Windows PowerShell 5.1 cannot parse `||` (`ParserError:
InvalidEndOfLine`, verified), so the chain above fails to parse there and no data
is collected (the session itself is unaffected — the hook simply errors out).

Fixes, in order of preference:

1. **Install Git for Windows** — the hook then runs in Git Bash and the first
   branch works. Note that Claude Code looks for Git Bash in the standard
   locations; if Git is installed elsewhere (e.g. `C:\Git`), point
   `CLAUDE_CODE_GIT_BASH_PATH` at `bash.exe`.
2. Use PowerShell 7+ (`pwsh`), which supports `||`.
3. Override the hook in your own settings with a PowerShell-compatible command.

### Python on Windows

Python 3.9+ must be on `PATH` as `python` (or `python3`). The collector itself is
stdlib-only, so any Python 3 works; `python -S -E` skips `site` initialisation and
`PYTHON*` environment processing.

The Devin CLI itself is optional for collection — without it the record is still
written, with `name` and `email` set to `null`.

## Performance

The `PostToolUse` hook runs synchronously after every tool call, so its cost is paid
per call. Measured on this machine (20 runs each, after warmup; Linux, Python 3.12):

| Scenario                               | min (ms) | mean (ms) | p50 (ms) | max (ms) |
| -------------------------------------- | -------- | --------- | -------- | -------- |
| shell process spawn (`/bin/true`)    | 0.6      | 1.1       | 0.8      | 2.0      |
| `python3 -c pass` (interpreter boot) | 28.0     | 30.8      | 31.1     | 33.0     |
| `python3 -S -E -c pass`              | 9.4      | 11.1      | 10.9     | 13.3     |
| collector full run (write path)        | 35.3     | 38.6      | 38.6     | 42.0     |
| collector with `-S -E` (write path)  | 30.2     | 33.7      | 34.6     | 36.2     |
| collector early-exit (config off)      | 34.4     | 37.3      | 37.1     | 40.4     |

Analysis:

- **The collector's own logic costs ~8 ms** (stdin parse + layered config resolution +
  category match + `flock` append). The dominant cost is Python interpreter startup
  (~31 ms), which is why the hook command uses `python3 -S -E` (skip `site` init and
  `PYTHON*` env processing — safe for a stdlib-only script), saving ~5 ms per call.
- The write path adds only ~1.3 ms over the early-exit path — appending with `flock` is
  negligible.
- Context: ~34 ms per tool call is well within the hook's 10 s timeout budget (0.3%),
  and small relative to a typical tool round-trip (shell commands alone usually take
  100 ms to seconds). Sessions with heavy parallel tool calls pay the cost per call,
  but the appends are serialized by `flock` only for the ~1 ms write window.
- Upload adds a **direct-send cost in the hook**: ~60 ms per call for 2 localhost
  servers (v1.7.0 direct-send model); a down server costs ~0 (instant refusal) or at
  most one 3 s timeout per 60 s cooldown window (circuit breaker).

## Spec conformance notes

- `plugin.json` strictly conforms to the Agent Plugins 1.0.0 manifest schema (closed
  field set: `$schema`/`name`/`version`/`description`/`author`/`license`/`keywords`).
- Spec v1 standardizes only two component types (`skills/`, `mcp.json`); **hooks are
  not a v1 component type** — they fall under the "client extension" mechanism (§8 of
  the spec, client-specific behavior). This plugin delivers its hooks via Devin CLI's
  plugin hook convention (`hooks.json` at the plugin root); the hook process locates
  bundled scripts through `DEVIN_PLUGIN_ROOT` and follows Devin's plugin-hook
  "fail-open" semantics.
- The format has **no install-time hook**, so the Devin CLI and the Devin login
  cannot be verified while installing; both are requirements the user must meet (see
  [Requirements](#requirements)). The collector detects either one at runtime, writes
  nothing without an account, and reports how to fix it.
- The data directory does not use the spec's `PLUGIN_DATA` (per §9.1 that variable is
  only guaranteed for stdio MCP subprocesses, not hook processes); an equivalent
  user-level persistent directory plus an environment-variable override is used
  instead.

## Privacy

By default only metadata is recorded (tool name, status, input field names). Tool input
values, output, and error text are never recorded. If finer-grained data is needed in
the future, prefer enabling it on the server/upload side rather than widening what is
persisted at the collector.

