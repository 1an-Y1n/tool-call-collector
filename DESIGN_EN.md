# tools-call-collector Design Document

Version: 1.8.0  |  Date: 2026-10-15  |  Status: local collection + direct-send upload (write-and-send in the hook, circuit-breaker protected) + N clients → 1 server implemented

Related documents: `README.md` (installation & usage), `plugin.json` (manifest), `hooks.json` (hook registration)
Chinese version: [DESIGN.md](DESIGN.md)

---

## 1. Goal

Collect data on how the agent uses **tools (including skills)** during sessions:

1. **Collection**: automatic, via a hook that fires after every tool call — no manual steps;
2. **Local storage**: data is written locally first, in a format designed to be upload-friendly;
3. **Upload (undetermined)**: the data may later be uploaded to a server for aggregation and
   analysis. The target endpoint, protocol, authentication, and trigger timing are all
   undetermined; for now we only **reserve the format and an upload cursor** — no upload
   logic is implemented.

Design constraints:

| Constraint | Description |
| --- | --- |
| Never block the session | The collector always exits with code 0 on any failure (fail-open); it must never interfere with the agent |
| Privacy | Metadata only (tool name, status, input field names, skill name — identifiers only); never parameter values, tool output, or error text |
| Spec conformance | The plugin package conforms to the [Agent Plugins Specification 1.0.0](https://github.com/agentplugins/agent-plugins-spec) |
| Zero dependencies | Scripts use only the Python 3 standard library |

---

## 2. Data collection method: hooks

### 2.1 How it works

Devin CLI provides lifecycle hooks: shell commands registered against agent lifecycle
events, with event data passed as JSON on **stdin**. This plugin registers the
`PostToolUse` event (empty matcher, i.e. all tools) — it fires **once after every tool
call finishes**.

```text
agent calls a tool (exec / edit / read / skill / mcp__* / …)
   │  tool execution finishes
   ▼
PostToolUse hook fires (registered in hooks.json)
   │  event JSON ──► stdin
   ▼
scripts/collector.py
   ├─► parses the event, extracts metadata
   ├─► decides whether/where to write based on $TOOL_CALL_COLLECTOR_CATEGORIES:
   │     the call's category is configured  → events/<category>-calls.jsonl
   │     otherwise "all" is configured      → events/tool-calls.jsonl
   │     otherwise (including unset)        → discarded, no files are created
   └─► skill-category records additionally carry the skill name (skill-calls.jsonl)
```

### 2.2 Registration

Hooks are not a component type standardized by Agent Plugins v1 (v1 standardizes only
`skills/` and `mcp.json`); they fall under the **client extension** mechanism (§8 of the
spec). Following Devin CLI's plugin hook convention, this plugin registers its hook in
`hooks.json` at the plugin root:

```json
{
  "PostToolUse": [
    {
      "matcher": "",
      "hooks": [
        {
          "type": "command",
          "command": "python3 \"${DEVIN_PLUGIN_ROOT:-$CLAUDE_PLUGIN_ROOT}/scripts/collector.py\"",
          "timeout": 10
        }
      ]
    }
  ]
}
```

- `matcher: ""` matches every tool name (including MCP tools in the
  `mcp__<server>__<tool>` form);
- `${DEVIN_PLUGIN_ROOT}` is injected by the CLI and points to the plugin's installed
  content directory (the cache path), which is how the hook locates the bundled script;
  `$CLAUDE_PLUGIN_ROOT` is kept as a compatibility fallback;
- the hook follows Devin's fail-open semantics for plugin hooks.

### 2.3 Key collector design decisions

| Decision | Implementation | Rationale |
| --- | --- | --- |
| Fail-open | All logic wrapped in try/except; any exception prints to stderr and exits 0 | Collection is a side-channel; it must never disturb the agent's main flow |
| Per-category switch (layered config) | env > data-dir config.json > shipped config.default.json > built-in off | Plugin installation cannot write environment variables; the shipped default file makes the plugin work with zero user configuration, while users can still override layer by layer |
| Metadata only | Extracts only `tool_name`, `tool_response.success/error` (normalized to `status`), and the **key names** of `tool_input` | Parameter values may contain file contents, commands, and other sensitive data; key names are enough for usage-pattern analysis |
| Concurrency-safe | `open(O_APPEND)` + `fcntl.flock(LOCK_EX)` around each single-line write | Parallel tool calls spawn concurrent hook processes; locking keeps lines intact |
| Cheap and stateless | Single process, single file append, no network calls | The hook runs synchronously after every tool call, so it must be fast |
| Direct send, bounded latency | After writing, sends one batch per server immediately; 3 s timeout + per-server circuit breaker (60 s cooldown after a failure) | Immediacy first; a down server costs one timeout window at most, then zero cost during cooldown (see §5) |

### 2.4 Category configuration (three-layer resolution)

Plugin installation cannot write environment variables on the user's behalf, so the
category config is resolved in layers — **first match wins**:

1. `$TOOL_CALL_COLLECTOR_CATEGORIES` — environment variable; **set-but-empty means
   explicitly off** (disable without uninstalling)
2. `<data dir>/config.json` — user config file, `{"categories": [...]}`; lives in the
   persistent data directory, so it survives plugin updates
3. `<plugin root>/config.default.json` — factory default shipped with the plugin
   (currently `["all", "skill"]`) — **works with zero user configuration**
4. Built-in fallback: off (no files are generated)

The resolved value is a comma/whitespace-separated category list; each configured
category gets its own event file:

| Category | tool_name included | Event file |
| --- | --- | --- |
| `file` | `read` `write` `edit` `apply_patch` `notebook_read` `notebook_edit` | `file-calls.jsonl` |
| `search` | `grep` `glob` | `search-calls.jsonl` |
| `shell` | `exec` `get_output` `write_to_process` `kill_shell` | `shell-calls.jsonl` |
| `web` | `webfetch` | `web-calls.jsonl` |
| `planning` | `todo_write` `exit_plan_mode` | `planning-calls.jsonl` |
| `skill` | `skill` | `skill-calls.jsonl` (with skill name) |
| `subagent` | `run_subagent` `read_subagent` | `subagent-calls.jsonl` |
| `permission` | `request_scope` | `permission-calls.jsonl` |
| `mcp-management` | `mcp_list_servers` `mcp_list_tools` `mcp_call_tool` `mcp_read_resource` | `mcp-management-calls.jsonl` |
| `mcp` | `mcp__<server>__<tool>` (prefix match) | `mcp-calls.jsonl` |
| `other` | anything unclassified (e.g. `ask_user_question` and other newer tools) | `other-calls.jsonl` |
| `all` | every tool | `tool-calls.jsonl` (generic file, metadata only) |

Write rules (each call lands in at most one file, so statistics can safely sum
across files):

1. The call's category is configured → its category file (specific categories
   win over `all`);
2. Otherwise `all` is configured → `tool-calls.jsonl`;
3. Otherwise the call is discarded.
Unknown category names in the config are ignored (no error, no blocking).
The environment variable must be exported in the shell that launches devin (hook
processes inherit the session environment).

### 2.5 Performance overhead (measured)

The `PostToolUse` hook runs **synchronously** after every tool call, so the cost is
paid per call. Measured on this machine (Linux, Python 3.12; 20 runs per scenario,
timed after warmup):

| Scenario                              | min (ms) | mean (ms) | p50 (ms) | max (ms) |
| ------------------------------------- | -------- | --------- | -------- | -------- |
| shell process spawn (`/bin/true`)     | 0.6      | 1.1       | 0.8      | 2.0      |
| `python3 -c pass` (interpreter boot)  | 28.0     | 30.8      | 31.1     | 33.0     |
| `python3 -S -E -c pass`               | 9.4      | 11.1      | 10.9     | 13.3     |
| collector full run (write path)       | 35.3     | 38.6      | 38.6     | 42.0     |
| collector with `-S -E` (write path)   | 30.2     | 33.7      | 34.6     | 36.2     |
| collector early-exit (config off)     | 34.4     | 37.3      | 37.1     | 40.4     |

Conclusions:

- **The collector's own logic costs ~8 ms** (stdin parse + layered config resolution +
  category match + `flock` append); the dominant cost is Python interpreter startup
  (~31 ms). The hook command therefore uses `python3 -S -E` (skip `site` init and
  `PYTHON*` env processing — safe for a stdlib-only script), saving another ~5 ms per
  call.
- The write path adds only ~1.3 ms over the early-exit path; the `flock` append is
  negligible.
- Context: ~34 ms is 0.3% of the hook's 10 s timeout budget and small relative to a
  typical tool round-trip (shell commands alone usually take 100 ms to seconds). With
  parallel tool calls the cost is paid per call; `flock` serializes only the ~1 ms
  write window.

### 2.6 Recognizing skill invocations

On the agent side, invoking a skill **is** a `skill` tool call (its input carries
`command`: invoke/list/search and `skill`: the name). With the `skill` category
configured:

- only **real invocations** are written to `skill-calls.jsonl`
  (`type: "skill_invoke"`): `command == "invoke"` or an identifiable skill name;
  list/search browsing is not recorded. These records additionally carry the
  **skill name** (`skill`), the **operation mode** (`skill_command`), and `status`;
- with only `all` configured: skill calls land in `tool-calls.jsonl` like any
  other tool — metadata only, no name.

A skill name is an identifier, not content, so recording it does not violate the
metadata-only constraint.

---

## 3. Data storage and format

### 3.1 Location

```text
~/.devin/tools-call-collector/          # default; override with $TOOL_CALL_COLLECTOR_DATA_DIR
├── config.json                         # (optional) user category config, below the env var in priority
├── events/
│   ├── tool-calls.jsonl                # generated only when "all" is configured: every tool call (type: tool_call)
│   ├── skill-calls.jsonl               # generated only when "skill" is configured: skill calls (type: skill_invoke, with name)
│   └── <category>-calls.jsonl          # other categories, generated per config
└── state/                              # upload cursors (cursors.json) + upload.lock
```

Event files are generated dynamically from the resolved category config:
unconfigured categories have no file, and when the config resolves to empty the
`events/` directory itself is never created (the shipped `config.default.json`
makes the default `all,skill`).

Why `~/.devin/`: a user-level global directory that aggregates across projects without
polluting any code repository; the plugin-name subdirectory avoids clashing with other
tools.

### 3.2 Record format (schema_version: 3)

JSONL (NDJSON): **one self-contained JSON record per line**. Common fields:
`schema_version`, `timestamp` (UTC ISO 8601, microsecond precision), `client_id`,
`user`, `session_id`, `prompt_id`, `project_dir`, `host`, `collector{name,version}`.

`client_id` is a **stable per-installation identifier**: a UUID generated once on
first run and persisted in `<data dir>/state/client_id`; every record carries it
afterwards. When N clients upload to the same server, the server distinguishes data
ownership by `client_id` (`host` is only a human-readable hint — hostnames collide
and session names repeat across machines, so neither is a reliable key);
`(client_id, session_id)` uniquely identifies one session globally.

`user` is the **Linux account name** owning the session (`getpass.getuser()`, null
when unavailable), handy for per-user aggregation server-side; it is descriptive
metadata, not a reliable key (renameable, collision-prone).

`tool_call`-only fields: `tool_name`, `status` (success/error/unknown), `input_keys`
(array of input key names).

`skill_invoke`-only fields: `skill` (the name), `skill_command` (invoke/list/search
etc.), `status`.

Examples:

```json
{"schema_version":3,"type":"tool_call","timestamp":"2026-10-14T07:59:35.309678Z","client_id":"2d1dcb0d-…","user":"ian_yin","session_id":"superb-quality","prompt_id":"c8222e07-…","project_dir":"/home/ian_yin/devin-tools","host":"devbox","tool_name":"edit","status":"success","input_keys":["file_path","new_string","old_string"],"collector":{"name":"tools-call-collector","version":"1.9.0"}}
{"schema_version":3,"type":"skill_invoke","timestamp":"2026-10-14T08:08:07.585190Z","client_id":"2d1dcb0d-…","user":"ian_yin","session_id":"superb-quality","prompt_id":"8aa1ce27-…","project_dir":"/home/ian_yin/devin-tools","host":"devbox","skill":"devin-cli","skill_command":"invoke","collector":{"name":"tools-call-collector","version":"1.9.0"}}
```

### 3.3 Why JSONL (upload-oriented design)

| Property | Value for uploading |
| --- | --- |
| Append-only, no read-modify-write | A crash loses at most the last line; the file is never corrupted; it naturally forms an "outbound queue" |
| One line = one self-contained record | Uploads can batch lines and POST them as NDJSON (the standard streaming-ingest format); the server parses and ingests line by line |
| `schema_version` embedded in every line | When fields evolve, the server can parse mixed old/new data version-compatibly |
| Never rewritten in place | Upload progress can be expressed as a byte cursor — see §5 |

---

## 4. Local statistics

`scripts/stats.py` (read-only) aggregates **every** `*-calls.jsonl` file under
`events/` (regardless of which categories are configured; `skill_invoke`
records are counted as tool `skill` in the tool summary):

```bash
python3 scripts/stats.py              # per-tool totals (total/success/error/unknown)
python3 scripts/stats.py --by skill   # skill invocation stats (grouped by skill name)
python3 scripts/stats.py --by session # per-session breakdown
python3 scripts/stats.py --by day     # per-day breakdown
python3 scripts/stats.py --days 7     # time-window filter
python3 scripts/stats.py --json       # machine-readable output (tools/skills/days/sessions)
python3 scripts/stats.py --info       # current config + size/line count of each event file
```

---

## 5. Uploading to a server (introduced in v1.3.0, direct-send model since v1.7.0)

**Protocol**: HTTPS (HTTP allowed for localhost) + NDJSON batch `POST` — the request
body is the raw JSONL lines (`Content-Type: application/x-ndjson`); the server ingests
line by line. The client advances its cursor only after a 2xx response, so failures
retry naturally. No authentication.

**Trigger — direct send in the hook** (no background processes, no SessionEnd hook):

1. After each recorded call, `collector.py` **writes the record to the file first**
   (durable), then **directly sends one batch to each configured server** — normally
   just this record; if earlier sends failed, it also catches up backlog one batch per
   call (500 lines / 256 KB per request);
2. **Circuit breaker**: a failed send puts that server on a 60 s cooldown (per
   server); during cooldown it is skipped entirely, its data stays pending, and the
   next call after cooldown retries and catches up; healthy servers are unaffected;
3. **Short timeout**: 3 s per request (`TOOL_CALL_COLLECTOR_SEND_TIMEOUT` to
   override); a refused connection fails instantly (a down server costs ~0 extra), a
   black-holed host costs at most one timeout per cooldown window;
4. `scripts/uploader.py` remains as the **manual full-drain backfill** tool
   (`python3 scripts/uploader.py`, cron-able); it shares the same lock, cursors, and
   breaker with the hook's direct send.

**Latency (measured)**: direct send adds ~60 ms per call (2 localhost servers, ~30 ms
each); WAN servers are expected at ~100–300 ms per call — the deliberate price of
per-entry immediacy. The file is written *before* sending, so durability is never at
risk.

**Topology: N clients → 1 server** — the client keeps its own byte cursor per event
file (`state/cursors.json`, schema v3 flat form, older v1/v2 formats migrate
automatically): a temporarily unreachable server falls behind silently and catches up
from its own cursor after recovery; cursors advance only after a 2xx; entries for
files that no longer exist (e.g. after rotation) are dropped. Data ownership across
clients is distinguished by `client_id`.

**Server concurrency**: the server runs SQLite with WAL + `busy_timeout=10s` — N
simultaneous requests queue and commit atomically (measured: 20 concurrent requests ×
5 records = 100 records ingested, zero errors, 621 ms).

**Server configuration** (resolved in layers, first match wins):

1. `$TOOL_CALL_COLLECTOR_SERVER_URL` — environment variable (temporary override)
2. `<data dir>/config.json` → `{"server": "https://…/v1/events"}`
   (per-machine persistent override; a legacy `"servers": […]` list takes its first entry)
3. `<plugin root>/config.default.json` → `{"server": "https://…/v1/events"}` —
   **shipped factory default**: fill in the real address when deploying your own
   plugin, so installed clients need zero configuration

Credentials are never shipped inside the plugin package (spec requirement); the
server URL is not a credential and may be shipped for own-plugin deployments. With no
server configured anywhere, nothing is sent and data simply accumulates locally.

**Reference backend**: [`../tools-call-server`](../tools-call-server) — FastAPI +
SQLite: `POST /v1/events` (ingest), `GET /v1/stats` (aggregation), `GET /dashboard`
(HTML chart dashboard: skill/tool usage counts and daily trend, Chart.js served
locally — no internet needed at view time), `GET /health`.
Malformed lines are quarantined server-side (raw preserved), so the client cursor can
always advance safely.

---

## 6. Packaging and activation

### 6.1 Package layout (Agent Plugins 1.0.0)

```text
tools-call-collector/
├── plugin.json     # spec manifest ($schema points to 1.0.0; closed field set; validated against the official schema)
├── hooks.json      # client extension: Devin CLI plugin hook convention location
├── scripts/
│   ├── collector.py
│   └── stats.py
├── LICENSE
└── README.md / DESIGN.md
```

### 6.2 Installation and activation (registry-based, not directory-based)

```text
devin plugins install --local <source dir>
  └─► writes an entry into ~/.local/share/devin/cli/plugins/lock.json (user scope, this machine only)
        └─► read at session/turn start
              └─► load manifest/hooks from the cache (hardlinked to the source dir)
                    └─► activate the PostToolUse hook
```

- The plugin source can live at any path; activation requires a registry entry, not a
  "standard directory" location;
- `--local` installs hardlink the content into the cache, so source edits take effect
  immediately (the hook actually runs with the cache path as `DEVIN_PLUGIN_ROOT`);
- Observed activation timing: a plugin installed mid-session takes effect from the
  **next user turn** (a turn that started before the plugin existed is not covered).

### 6.3 What has been verified

- The manifest validates against the official `plugin.schema.json`; `devin plugins info`
  lists the hook correctly;
- Unit level: valid/malformed input and 20-way concurrent appends all land correctly;
  all 6 config-resolution scenarios pass (env override / env explicit-off / user
  config.json / factory default / everything missing / malformed file skips the layer);
- Real end-to-end: in a live session the hook fires, `DEVIN_PLUGIN_ROOT` resolves, and
  data lands on disk — full chain verified; skill invocations are recorded separately
  in `skill-calls.jsonl` (with the skill name);
- Performance: the full hook chain costs ~34 ms per call (after the `-S -E`
  optimization), of which the collector's own logic is only ~8 ms — the rest is Python
  interpreter startup (see §2.5);
- Upload full chain: collector write → detached background upload → FastAPI ingest →
  cursor advance, verified end-to-end in a real session; 5 concurrent uploaders land
  exactly-once (lock works); a rejected token (401) leaves the cursor untouched and
  the data pending for retry;
- client_id: stable within a data dir, unique across data dirs (schema v2); the
  server migrates old databases automatically (ALTER TABLE adds the column), old
  records group under null and new records aggregate per client.
