# tools-call-collector 设计文档

版本：1.8.0 ｜ 日期：2026-10-15 ｜ 状态：本地采集 + 直发上传（hook 内即写即传，熔断保护）+ N clients → 1 server 已实现

配套文档：`README.md`（安装与使用）、`plugin.json`（清单）、`hooks.json`（hook 注册）
英文版：[DESIGN_EN.md](DESIGN_EN.md)

---

## 1. 任务目标

统计 agent 在会话中使用**工具（含 skill）**的数据：

1. **采集**：通过 hook 在每次工具调用后自动收集，无需人工干预；
2. **本地保存**：数据先落盘本地，格式对"将来上传"友好；
3. **上传（未确定）**：后期可能上传到服务器做聚合分析。上传的目标端点、协议、
   认证方式与触发时机均未确定，当前只做**格式与游标预留**，不实现上传逻辑。

设计约束：

| 约束 | 说明 |
| --- | --- |
| 不阻塞会话 | 采集器任何失败都以 exit 0 结束（fail-open），绝不影响 agent 正常工作 |
| 隐私安全 | 只记录元数据（工具名、状态、参数字段名、skill 名等标识符），不记录参数值、工具输出、错误文本 |
| 规范符合 | 插件打包符合 [Agent Plugins Specification 1.0.0](https://github.com/agentplugins/agent-plugins-spec) |
| 零依赖 | 脚本仅用 Python 3 标准库 |

---

## 2. 数据收集方法：hook

### 2.1 原理

Devin CLI 提供生命周期 hook：在 agent 生命周期事件上执行注册的 shell 命令，
事件数据以 JSON 从 **stdin** 传入。本插件注册 `PostToolUse` 事件（matcher 为空，
匹配所有工具）——**每次工具调用结束后**触发一次采集脚本。

```text
agent 调用工具（exec / edit / read / skill / mcp__* / …）
   │  工具执行结束
   ▼
PostToolUse hook 触发（hooks.json 注册）
   │  事件 JSON ──► stdin
   ▼
scripts/collector.py
   ├─► 解析事件，提取元数据
   ├─► 按 $TOOL_CALL_COLLECTOR_CATEGORIES 决定是否写入、写入哪个文件：
   │     配置了该调用的类别 → events/<类别>-calls.jsonl
   │     否则配置了 all    → events/tool-calls.jsonl
   │     否则（含未配置）   → 丢弃，不生成任何文件
   └─► skill 类别的记录额外携带 skill 名（skill-calls.jsonl）
```

### 2.2 注册方式

hook 不是 Agent Plugins v1 规范化的组件类型（v1 只标准化 `skills/` 与 `mcp.json`），
属于规范 §8 的**客户端扩展**机制。本插件按 Devin CLI 的插件 hook 约定，
在插件根目录 `hooks.json` 注册：

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

- `matcher: ""` 匹配所有工具名（含 `mcp__<server>__<tool>` 形式的 MCP 工具）；
- `${DEVIN_PLUGIN_ROOT}` 由 CLI 注入，指向插件安装后的内容目录（cache 路径），
  以此定位插件内脚本；`$CLAUDE_PLUGIN_ROOT` 作为兼容回退；
- hook 遵循 Devin 插件 hook 的 fail-open 语义。

### 2.3 采集器的关键设计

| 设计点 | 实现 | 原因 |
| --- | --- | --- |
| fail-open | 全部逻辑包在 try/except 中，任何异常打印 stderr 后 `exit 0` | 采集是旁路功能，绝不能干扰 agent 主流程 |
| 按类别开关（分层配置） | env > 数据目录 config.json > 插件自带 config.default.json > 内置关闭 | 插件安装无法写环境变量，靠自带默认文件实现零配置生效；用户仍可逐层覆盖 |
| 元数据 only | 只取 `tool_name`、`tool_response.success/error`（归纳为 `status`）、`tool_input` 的**键名列表** | 参数值可能含文件内容、命令等敏感数据；键名足以做使用模式分析 |
| 并发安全 | `open(O_APPEND)` + `fcntl.flock(LOCK_EX)` 后单行写入 | 并行工具调用时多个 hook 进程同时追加，保证行完整性 |
| 幂等低开销 | 单进程、单文件追加、无网络调用 | hook 在每次工具调用后同步执行，必须快 |
| 直发低延迟 | 写入后立即发送一批（每 server 一批）；短超时 3s + 按 server 熔断（失败冷却 60s） | 即时性优先；宕机服务器只损失一个超时窗口，之后冷却期内零开销（见 §5） |

### 2.4 分类配置（三层解析）

插件安装无法替使用者写入环境变量，因此类别配置按**分层解析、首个命中生效**：

1. `$TOOL_CALL_COLLECTOR_CATEGORIES`（环境变量；**设置但为空 = 显式关闭**，可不卸载而禁用）
2. `<数据目录>/config.json`（用户配置文件，`{"categories": [...]}`；随数据目录持久化，插件更新不丢）
3. `<插件根>/config.default.json`（插件自带的出厂默认，当前 `["all", "skill"]`——**使用者零修改即生效**）
4. 内置兜底：关闭（不生成任何文件）

配置值为逗号/空格分隔的类别列表；每配置一个类别，生成一个对应的事件文件：

| 类别 | 包含的 tool_name | 事件文件 |
| --- | --- | --- |
| `file` | `read` `write` `edit` `apply_patch` `notebook_read` `notebook_edit` | `file-calls.jsonl` |
| `search` | `grep` `glob` | `search-calls.jsonl` |
| `shell` | `exec` `get_output` `write_to_process` `kill_shell` | `shell-calls.jsonl` |
| `web` | `webfetch` | `web-calls.jsonl` |
| `planning` | `todo_write` `exit_plan_mode` | `planning-calls.jsonl` |
| `skill` | `skill` | `skill-calls.jsonl`（含 skill 名） |
| `subagent` | `run_subagent` `read_subagent` | `subagent-calls.jsonl` |
| `permission` | `request_scope` | `permission-calls.jsonl` |
| `mcp-management` | `mcp_list_servers` `mcp_list_tools` `mcp_call_tool` `mcp_read_resource` | `mcp-management-calls.jsonl` |
| `mcp` | `mcp__<server>__<tool>`（前缀匹配） | `mcp-calls.jsonl` |
| `other` | 其他未归类工具（如 `ask_user_question` 等新增工具） | `other-calls.jsonl` |
| `all` | 所有工具 | `tool-calls.jsonl`（通用文件，仅元数据） |

写入规则（保证一条调用只进一个文件，统计可安全跨文件求和）：

1. 该调用的类别被配置 → 写该类别文件（具体类别优先于 `all`）；
2. 否则配置了 `all` → 写 `tool-calls.jsonl`；
3. 否则丢弃。
配置中的未知类别名被忽略（不报错、不阻塞）。
环境变量需在启动 devin 的 shell 中导出（hook 进程继承会话环境）。

### 2.5 性能开销（实测）

`PostToolUse` hook 在每次工具调用后**同步**执行，开销按次计。实测
（Linux，Python 3.12，各场景 20 次取统计量，预热后计时）：

| 场景                                  | min (ms) | mean (ms) | p50 (ms) | max (ms) |
| ------------------------------------- | -------- | --------- | -------- | -------- |
| shell 进程创建（`/bin/true`，下限）    | 0.6      | 1.1       | 0.8      | 2.0      |
| `python3 -c pass`（解释器启动）        | 28.0     | 30.8      | 31.1     | 33.0     |
| `python3 -S -E -c pass`                | 9.4      | 11.1      | 10.9     | 13.3     |
| collector 完整执行（写文件路径）        | 35.3     | 38.6      | 38.6     | 42.0     |
| collector `-S -E`（写文件路径）         | 30.2     | 33.7      | 34.6     | 36.2     |
| collector 早退（配置关闭）              | 34.4     | 37.3      | 37.1     | 40.4     |

结论：

- **collector 自身逻辑仅 ~8 ms**（stdin 解析 + 分层配置解析 + 类别匹配 + `flock` 追加）；
  大头是 Python 解释器启动（~31 ms）。因此 hook 命令使用 `python3 -S -E`
  （跳过 `site` 初始化与 `PYTHON*` 环境处理，纯标准库脚本安全），每次再省 ~5 ms。
- 写文件路径只比早退路径多 ~1.3 ms，`flock` 追加开销可忽略。
- 对照：~34 ms 占 hook 10 s 超时预算的 0.3%；相对典型工具调用往返
  （shell 命令通常 100 ms～数秒）占比很小。并行调用时按次付费，
  `flock` 只在 ~1 ms 的写入窗口内串行化。

### 2.6 skill 调用的识别

在 agent 侧，skill 的调用本身就是一次 `skill` 工具调用（输入含 `command`:
invoke/list/search 与 `skill`: 名称等字段）。配置 `skill` 类别后：

- 仅**真实调用**写入 `skill-calls.jsonl`（`type: "skill_invoke"`）：
  `command == "invoke"` 或能识别出 skill 名；`list`/`search` 等浏览操作不记录；
  记录额外携带 **skill 名**（`skill`）、**操作模式**（`skill_command`）及 `status`；
- 若只配置 `all`：skill 调用进 `tool-calls.jsonl`，与其他工具一样仅元数据（无名称）。

skill 名是标识符而非内容，记录它不违反"仅元数据"约束。

---

## 3. 数据存储与格式

### 3.1 位置

```text
~/.devin/tools-call-collector/          # 默认；可用 $TOOL_CALL_COLLECTOR_DATA_DIR 覆盖
├── config.json                         # （可选）用户类别配置，优先级低于环境变量
├── events/
│   ├── tool-calls.jsonl                # 仅配置 all 时生成：所有工具调用（type: tool_call）
│   ├── skill-calls.jsonl               # 仅配置 skill 时生成：skill 调用（type: skill_invoke，含 skill 名）
│   └── <类别>-calls.jsonl              # 其余类别按配置动态生成
└── state/                              # 上传游标 cursors.json + upload.lock
```

事件文件按解析后的类别配置动态生成：未配置的类别没有对应文件，配置解析为空时
`events/` 目录本身也不会创建（插件自带的 `config.default.json` 使默认配置为
`all,skill`）。

选 `~/.devin/` 的原因：用户级全局目录，跨项目汇总，不污染代码仓库；
按插件名建子目录避免与其他工具混淆。

### 3.2 记录格式（schema_version: 3）

JSONL（NDJSON）：**一行一条自包含 JSON 记录**。公共字段：
`schema_version`、`timestamp`（UTC ISO 8601 微秒）、`client_id`、`user`、
`session_id`、`prompt_id`、`project_dir`、`host`、`collector{name,version}`。

`client_id` 是**安装级稳定标识**：首次运行时生成一次 UUID，持久化在
`<数据目录>/state/client_id`，此后所有记录携带。N 个 client 向同一服务器上传时，
服务器以 `client_id` 区分数据归属（`host` 仅作人类可读提示——主机名会撞名、
会话名跨机会重复，都不是可靠主键）；服务端按 `(client_id, session_id)` 即可全局唯一定位一次会话。

`user` 是会话所属的 **Linux 账号名**（`getpass.getuser()`，取不到时为 null），
便于服务端按使用者聚合；它是描述性字段而非可靠主键（可改名、可撞名）。

`tool_call` 记录独有字段：`tool_name`、`status`（success/error/unknown）、
`input_keys`（参数键名数组）。

`skill_invoke` 记录独有字段：`skill`（名称）、`skill_command`（invoke/list/search 等）、
`status`。

示例：

```json
{"schema_version":3,"type":"tool_call","timestamp":"2026-10-14T07:59:35.309678Z","client_id":"2d1dcb0d-…","user":"ian_yin","session_id":"superb-quality","prompt_id":"c8222e07-…","project_dir":"/home/ian_yin/devin-tools","host":"devbox","tool_name":"edit","status":"success","input_keys":["file_path","new_string","old_string"],"collector":{"name":"tools-call-collector","version":"1.9.0"}}
{"schema_version":3,"type":"skill_invoke","timestamp":"2026-10-14T08:08:07.585190Z","client_id":"2d1dcb0d-…","user":"ian_yin","session_id":"superb-quality","prompt_id":"8aa1ce27-…","project_dir":"/home/ian_yin/devin-tools","host":"devbox","skill":"devin-cli","skill_command":"invoke","collector":{"name":"tools-call-collector","version":"1.9.0"}}
```

### 3.3 为什么选 JSONL（面向上传的设计）

| 特性 | 对上传的价值 |
| --- | --- |
| 追加即写入，无读改写 | 崩溃/断电最多丢最后一行，文件不会损坏；天然构成"待上传队列" |
| 一行 = 一条自包含记录 | 上传时按行批量 POST（NDJSON 是流式摄取标准格式），服务端可逐行解析入库 |
| `schema_version` 内嵌于每行 | 将来字段演进时，服务端可按版本兼容解析新旧混合数据 |
| 只增不改 | 上传进度可用"字节游标"表达，见 §5 |

---

## 4. 本地统计

`scripts/stats.py`（只读）聚合 `events/` 下**所有** `*-calls.jsonl` 文件
（无论配置了哪些类别；`skill_invoke` 记录在工具汇总中计入 `skill`）：

```bash
python3 scripts/stats.py              # 按工具汇总（total/success/error/unknown）
python3 scripts/stats.py --by skill   # skill 调用统计（按 skill 名分组）
python3 scripts/stats.py --by session # 按会话分解
python3 scripts/stats.py --by day     # 按天分解
python3 scripts/stats.py --days 7     # 时间窗过滤
python3 scripts/stats.py --json       # 机器可读输出（含 tools/skills/days/sessions）
python3 scripts/stats.py --info       # 当前配置 + 各事件文件的大小/行数（上传游标参考）
```

---

## 5. 上传到服务器（v1.3.0 引入，v1.7.0 改为直发模型）

**协议**：HTTPS（localhost 允许 HTTP）+ NDJSON 批量 POST——请求体即原始 JSONL 行
（`Content-Type: application/x-ndjson`），服务端逐行摄取；客户端仅在 2xx 后推进游标，
失败自然重试。无认证。

**触发——hook 内直发**（无后台进程、无 SessionEnd hook）：

1. 每次工具调用后，collector **先落盘**（保证持久），随后**直接向每台配置的 server
   发送一批**——正常情况下就是本条目；此前有发送失败时，顺带补一批积压
   （每请求最多 500 行 / 256 KB）；
2. **熔断器**：某 server 发送失败 → 该 server 进入 60s 冷却（按 server 独立）；
   冷却期内完全跳过、数据留在文件里，冷却后下一次工具调用自动重试并补积压；
   健康的 server 不受影响；
3. **短超时**：每请求 3s（`TOOL_CALL_COLLECTOR_SEND_TIMEOUT` 可覆盖）；连接被拒
   即时失败（宕机 server 额外成本 ~0），黑洞地址每个冷却窗口最多损失一次超时；
4. `scripts/uploader.py` 保留为**手动全量补传**工具（`python3 scripts/uploader.py`，
   可挂 cron）；与 hook 直发共享同一把锁、同一份游标和熔断器。

**延迟（实测）**：直发使 hook 每次净增 ~60 ms（2 台 localhost server，各 ~30 ms）；
WAN server 预计 ~100–300 ms/次——这是每条目即时性的自觉代价。文件先写后发，
持久性永不受威胁。

**拓扑：N clients → 1 server**——client 对每个事件文件维护独立字节游标
（`state/cursors.json`，schema v3 扁平格式，旧 v1/v2 自动迁移）：server 短暂不可达时
静默落后，恢复后从自己的游标完整补齐；游标仅在 2xx 后原子推进；
文件不存在（轮转后）的游标条目会被清理。多 client 的数据归属由 `client_id` 区分。

**服务器并发**：server 端 SQLite 开启 WAL + `busy_timeout=10s`——N 个请求同时到达
时排队原子入库（实测 20 并发请求 × 5 条 = 100 条全部入库、零错误、621ms）。

**服务器配置**（三层解析，首个命中生效）：

1. `$TOOL_CALL_COLLECTOR_SERVER_URL`（环境变量，临时覆盖）
2. `<数据目录>/config.json` → `{"server": "https://…/v1/events"}`
   （单机持久覆盖；兼容旧 `"servers": […]` 列表，取第一个）
3. `<插件根>/config.default.json` → `{"server": "https://…/v1/events"}`
   （**插件自带的出厂默认**：部署自有插件时填入真实地址，安装后零配置生效）

凭据（token/密码）绝不打进插件包（规范要求）；server 地址不是凭据，自有插件
部署时可以自带。未配置服务器时不发送，数据只在本地累积。

**参考后端**：[`../tools-call-server`](../tools-call-server)——FastAPI + SQLite：
`POST /v1/events`（摄取）、`GET /v1/stats`（聚合）、`GET /dashboard`（HTML 图表仪表盘：
skill/工具调用次数、每日趋势，Chart.js 本地托管无需外网）、`GET /health`。
畸形行在服务端隔离保存（raw 保留），客户端游标总能安全推进。

---

## 6. 插件打包与生效机制

### 6.1 打包（Agent Plugins 1.0.0）

```text
tools-call-collector/
├── plugin.json     # 规范清单（$schema 指向 1.0.0，封闭字段集，已通过官方 schema 校验）
├── hooks.json      # 客户端扩展：Devin CLI 插件 hook 约定位置
├── scripts/
│   ├── collector.py
│   └── stats.py
├── LICENSE
└── README.md / DESIGN.md
```

### 6.2 安装与生效（注册制，非目录制）

```text
devin plugins install --local <源目录>
  └─► 写入注册表 ~/.local/share/devin/cli/plugins/lock.json（user 作用域，仅本机）
        └─► 会话/轮次启动时读取注册表
              └─► 从 cache（硬链接到源目录）加载 manifest/hooks
                    └─► 激活 PostToolUse hook
```

- 插件源码可在任意路径，生效条件是注册表中有记录，不要求位于"标准目录"；
- `--local` 安装通过**硬链接**缓存内容，修改源文件即时反映到 cache
  （hook 实际以 cache 路径作为 `DEVIN_PLUGIN_ROOT` 执行）；
- 实测生效时机：会话中途安装的插件，从**下一个用户轮次**开始生效
  （会话启动时插件尚不存在则该轮次不生效）。

### 6.3 已验证事项

- manifest 通过官方 `plugin.schema.json` 校验；`devin plugins info` 正确列出 hook；
- 单元级：合法/畸形输入、20 路并发追加均正确落盘；配置解析 6 种场景
  （env 覆盖/env 显式关闭/用户 config.json/出厂默认/全部缺失/坏文件跳层）全部通过；
- 真实端到端：真实会话中 hook 触发、`DEVIN_PLUGIN_ROOT` 解析、数据落盘全链路通过；
  skill 调用被单独记录到 `skill-calls.jsonl`（含 skill 名）；
- 性能：hook 全链路 ~34 ms/次（`-S -E` 优化后），自身逻辑仅 ~8 ms，
  其余为 Python 解释器启动（详见 §2.5）；
- 上传全链路：collector 写入 → 派生后台上传 → FastAPI 入库 → 游标推进，
  真实会话端到端验证通过；5 并发上传恰好一次入库（锁生效）；
  错误 token 被拒（401）时游标不推进、数据留待重试；
- client_id：同数据目录稳定、跨数据目录唯一（schema v2）；服务端旧库自动迁移
  （ALTER TABLE 加列），旧记录归入 null 组、新记录按 client 聚合区分。
