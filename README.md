
# tool-call-collector

Collect devin tool calling.
===========================

# tools-call-collector

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
├── plugin.json                      # Agent Plugins 1.0.0 manifest
├── hooks.json                       # lifecycle hook registration (client extension, see "Spec conformance")
├── config.default.json              # factory-default category config (shipped with the plugin)
├── scripts/
│   ├── collector.py                 # PostToolUse collector (stdlib only)
│   ├── uploader.py                  # background NDJSON uploader (stdlib only)
│   └── stats.py                     # local aggregation (read-only)
├── LICENSE
└── README.md
```

## Installation

```bash
# Local development (linked to this folder; edits take effect immediately)
devin plugins install --local ./tools-call-collector

# Verify
devin plugins list
devin plugins info tools-call-collector
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
```

Event files are **generated dynamically from the config**: unconfigured categories have
no file, and with an empty config the `events/` directory itself is never created.

### Record schema (`schema_version: 3`)

One self-contained JSON object per line (NDJSON), with fields:

| Field              | Type           | Description                                                                                                                 |
| ------------------ | -------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `schema_version` | int            | Record format version, currently `3`; bump it when fields evolve                                                          |
| `type`           | string         | Event type, currently `"tool_call"`                                                                                       |
| `timestamp`      | string         | UTC ISO 8601, microsecond precision, `Z` suffix                                                                           |
| `client_id`      | string \| null | Stable per-installation ID (UUID, generated once in the data dir) — distinguishes different machines/clients on the server |
| `user`           | string \| null | Linux account name of the session owner (`getpass.getuser()`)                                                             |
| `session_id`     | string \| null | Session ID (shared by all hook events within a session)                                                                     |
| `prompt_id`      | string \| null | Turn ID (rotated on every user prompt)                                                                                      |
| `project_dir`    | string \| null | Project root of the session that triggered the call                                                                         |
| `host`           | string         | Machine name (human-readable hint; `client_id` is the reliable key)                                                       |
| `tool_name`      | string         | Tool name, e.g. `exec`, `edit`, `mcp__github__create_issue`                                                           |
| `status`         | string         | `success` / `error` / `unknown`                                                                                       |
| `input_keys`     | string[]       | Field names of `tool_input` (key names only, never values)                                                                |
| `collector`      | object         | `{ "name": "tools-call-collector", "version": "1.6.0" }`                                                                  |

Example (one line):

```json
{"schema_version":3,"type":"tool_call","timestamp":"2026-10-14T07:31:22.123456Z","client_id":"2d1dcb0d-6bcf-494a-9a8e-e02faaa4b8f7","user":"ian_yin","session_id":"3f8d1c2a-…","prompt_id":"b71e9d40-…","project_dir":"/home/ian_yin/devin-tools","host":"devbox","tool_name":"exec","status":"success","input_keys":["command","timeout"],"collector":{"name":"tools-call-collector","version":"1.6.0"}}
```

### Skill invocation records (`events/skill-calls.jsonl`, `type: "skill_invoke"`)

When the `skill` category is configured, **only real invocations** are written to this
file (`command == "invoke"` or an identifiable skill name); `list`/`search` browsing is
not recorded. Compared with other files, skill records additionally carry the skill
name and the operation mode (both are identifiers, so this does not violate the
metadata-only constraint):

| Field             | Type           | Description                                                                                                                                |
| ----------------- | -------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| (common fields)   | —             | Same as above: `schema_version`/`timestamp`/`client_id`/`user`/`session_id`/`prompt_id`/`project_dir`/`host`/`collector` |
| `type`          | string         | Always `"skill_invoke"`                                                                                                                  |
| `status`        | string         | `success` / `error` / `unknown`                                                                                                      |
| `skill`         | string \| null | The invoked skill name (e.g. `tools-call-collector:tool-call-stats`); null only when an invocation carries no identifiable name          |
| `skill_command` | string \| null | Operation mode of the skill tool: `invoke` / `list` / `search` etc.                                                                  |

In statistics (`stats.py --by skill`) records are grouped by `skill` name; an
invocation with no identifiable name shows up as `<unidentified>`.

Example (one line):

```json
{"schema_version":3,"type":"skill_invoke","timestamp":"2026-10-14T07:31:22.123456Z","client_id":"2d1dcb0d-6bcf-494a-9a8e-e02faaa4b8f7","user":"ian_yin","session_id":"3f8d1c2a-…","prompt_id":"b71e9d40-…","project_dir":"/home/ian_yin/devin-tools","host":"devbox","skill":"tools-call-collector:tool-call-stats","skill_command":"invoke","collector":{"name":"tools-call-collector","version":"1.9.0"}}
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
- The data directory does not use the spec's `PLUGIN_DATA` (per §9.1 that variable is
  only guaranteed for stdio MCP subprocesses, not hook processes); an equivalent
  user-level persistent directory plus an environment-variable override is used
  instead.

## Privacy

By default only metadata is recorded (tool name, status, input field names). Tool input
values, output, and error text are never recorded. If finer-grained data is needed in
the future, prefer enabling it on the server/upload side rather than widening what is
persisted at the collector.

>>>>>>> 989fc49 (Tool call collecor v1.0)
>>>>>>>
>>>>>>
>>>>>
>>>>
>>>
>>
