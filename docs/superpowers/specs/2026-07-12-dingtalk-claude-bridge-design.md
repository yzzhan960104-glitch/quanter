# 钉钉远程驱动 Claude 旁路桥设计

> 日期：2026-07-12
> 背景：希望在离开本机时，用手机钉钉远程驱动本项目的 `claude`（Claude Code CLI）——发消息启动/续接会话、接收回答，实现"人在外、claude 在家干活"的旁路交互。
> 范围：新增独立守护进程包 `bridge/` + 入口脚本 `scripts/dingtalk_claude_bridge.py`，与现有 FastAPI 交易后端**完全解耦**。不改动 `server/`、`trading/`、`caisen/` 任何现有代码（仅在 `.env.example` 追加配置项）。

---

## 1. 核心决策（brainstorming 已确认）

| 决策点 | 选定方案 | 理由 |
|---|---|---|
| 钉钉接入方式 | **企业内部应用 + Stream 长连接**（`dingtalk-stream` SDK） | 本地主动 WebSocket 连出，**无需公网 IP / 内网穿透**，天然双向，契合 Windows 本地旁路。现有群机器人 Webhook 是单向出站，收不到用户消息，弃用。 |
| claude 驱动模型 | **常驻交互进程 + stream-json 双流**；每会话一个进程的进程池 | 省每轮启动开销；`stream-json` 是 claude CLI 官方给"程序驱动"的接口（REPL 无法被子进程驱动）。 |
| 权限档 | **等同终端·全放行**（`--permission-mode bypassPermissions`，不收敛 `--allowed-tools`） | 用户明确要求"和 PyCharm 终端里敲 claude 一致"。能力/上下文/配置/MCP/工具集与终端完全相同。代价见 §8 安全契约。 |
| 运行形态 | **独立守护进程**（`python -m bridge` 或 `scripts/dingtalk_claude_bridge.py`） | 旁路就该是旁路：claude 子进程崩溃/被杀不影响交易 API；可独立重启；交易后端挂了仍能用钉钉让 claude 诊断。 |
| 会话粒度 | 钉钉 `conversationId`（同群=同会话；群 vs 单聊天然隔离） | 复用 claude `--resume` 续上下文，进程会死但 session_id 不丢。 |
| 指令 | `/new`（重置会话）、`/status`（池状态）、`/help`（帮助） | 保持通用，不掺业务指令。 |

### 凭证处理（红线）
- 用户已提供企业内部应用凭证：App ID、Client ID(AppKey)、Client Secret(AppSecret)、群机器人 webhook。
- **AppKey/AppSecret 仅写入本地 `.env`（已在 `.gitignore`）**，代码与 spec 一律用环境变量名 `DINGTALK_APP_KEY` / `DINGTALK_APP_SECRET` 占位，**绝不硬编码**（遵循项目 `core/notifier.py` 惯例）。
- 群机器人 webhook（access_token 模式）与本桥的 Stream 接入是两套体系；webhook 可作为"主动推群消息"的备用出站通道，但主回复走 Stream `chatbot.Reply`。

---

## 2. 总体架构与数据流

```
你(手机钉钉) @机器人 "w_bottom.py 的颈线怎么拟合的？"
   │
   ▼  [钉钉云端]
   │  WebSocket 长连接（本地主动连出，无需公网IP）
   ▼
┌─────────────────────────────────────────────────────────┐
│  bridge 守护进程 (scripts/dingtalk_claude_bridge.py)     │
│                                                          │
│  ① Stream 接入层   dingtalk-stream ChatbotHandler        │
│        ├─ 收到 ChatbotMessage                            │
│        ├─ 鉴权闸: staffId ∈ 白名单?  @机器人?             │
│        └─ 立即 ACK（防钉钉重投）                          │
│                                                          │
│  ② 派发层   asyncio.Task（异步，不阻塞 Stream 主循环）    │
│        ├─ 取/建 该 conversationId 的常驻 claude 子进程    │
│        │     ├─ 首次: spawn claude                        │
│        │     │     --input-format stream-json            │
│        │     │     --output-format stream-json --verbose │
│        │     │     --permission-mode bypassPermissions   │
│        │     │     --cwd <项目根>                         │
│        │     │     [--resume <session_id>]   # 崩溃重建  │
│        │     ├─ stdin 写一行 {"type":"user",...}          │
│        │     └─ stdout 逐行读事件 → 聚合文本 → result     │
│        └─ 同会话消息串行排队；不同会话并行                 │
│                                                          │
│  ③ 回复层   聚合文本 chatbot.Reply @回复原消息            │
│        └─ 长输出分段（≤1800 字/条）                       │
│                                                          │
│  ④ 可观测   全程落 logs/dingtalk_bridge.log              │
│             + 全量审计 logs/dingtalk_bridge_audit.jsonl  │
│             + 高危工具调用实时告警（复用 core/notifier）  │
└─────────────────────────────────────────────────────────┘
```

**复用 vs 新增**：
- **复用**：`core/notifier.py`（高危告警投递）、`python-dotenv`（凭证加载）、项目 `LOG_CONFIG` 日志格式惯例、`claude` CLI（系统 PATH 已装 v2.1.190）。
- **新增**：`bridge/` 包（6 文件）+ `scripts/dingtalk_claude_bridge.py` + `.env.example` 配置项 + `dingtalk-stream` 依赖。

---

## 3. 组件分解（文件结构 + 职责）

遵循 CLAUDE.md「极简、扁平、拒绝深层抽象」：6 个文件各扛一个职责，互相只通过显式接口说话，每个都能单测。

```
bridge/
├── __main__.py          # 入口：python -m bridge。load_dotenv → 装配 → asyncio 主循环
├── config.py            # 从 .env 读全部配置，纯数据类，无副作用
├── safety.py            # 纯逻辑：白名单校验 + 指令解析（/new /status /help）
├── session_store.py     # conversationId ↔ claude session_id 映射，JSON 落盘
├── claude_pool.py       # ★核心：每会话常驻 claude 子进程 + stream-json 双流 + 超时/崩溃重建
├── replier.py           # 回复钉钉：长消息分段 + Markdown 清洗
└── stream_client.py     # 装配 dingtalk-stream：鉴权闸 → ACK → 异步派发

scripts/dingtalk_claude_bridge.py   # thin 入口（项目惯例：可执行放 scripts/），等价 python -m bridge
```

| 组件 | 做什么 | 接口 | 依赖 |
|---|---|---|---|
| `config.py` | 读 `DINGTALK_APP_KEY/SECRET`、白名单、`CLAUDE_BIN`(默认 `claude`)、`CLAUDE_WORKDIR`(默认项目根)、单轮超时、空闲回收、频控阈值 | `BridgeConfig.from_env()` | python-dotenv(已有) |
| `safety.py` | 纯函数裁决：消息是否来自白名单、是否 @机器人、是否指令 | `classify(msg, cfg) -> Verdict` | config。**无 IO，最好测** |
| `session_store.py` | `conversationId → claude session_id` 持久化映射，落 `logs/dingtalk_sessions.json`（带文件锁防并发写坏） | `get(conv_id)` / `set(conv_id, sid)` | config |
| `claude_pool.py` ★ | 维护 `{conv_id: ClaudeProcess}`。`ClaudeProcess` 封装常驻 claude 子进程(stream-json 双流)，提供 `async ask(text)->str`；懒启动、stdout 事件聚合到 `result`、单轮超时 watchdog、崩溃检测后 `--resume` 重建、空闲回收 | `answer = await pool.ask(conv_id, text)` | config, session_store |
| `replier.py` | 把 claude 文本发回钉钉（handler reply，`@`回复原消息）；按 ~1800 字切段；剥离钉钉不支持的 Markdown（表格/`<font>`等） | `await reply(handler, incoming, text)` | dingtalk-stream |
| `stream_client.py` | `ChatbotHandler.process` 回调：`safety.classify` → 立即 ACK → `asyncio.create_task` 派发 → 结果交 replier | `await client.start_forever()` | dingtalk-stream + 其余全部 |

**新增依赖**（`requirements.txt`）：仅 `dingtalk-stream`（钉钉官方 Stream SDK，纯 Python，自带 WebSocket 重连）。**不引入** `claude_agent_sdk`、不引重型 IM 框架。

---

## 4. 会话模型 + 进程池状态机

### 4.1 会话映射（三级链）
```
钉钉 conversationId  ──(session_store.json 持久化)──▶  claude session_id  ──(内存)──▶  常驻 ClaudeProcess
        │                                                      │
        │  /new 指令                                            │  进程崩溃/超时
        ▼                                                      ▼
   映射清空 → 下次全新会话                              session_id 保留 → --resume 重建（上下文不丢）
```
关键：**进程会死，session_id 不丢**。claude CLI 把会话历史存本地（`~/.claude/`），拿着 `session_id` 即可 `--resume <session_id>` 续上下文。"常驻进程"是性能优化（省每轮启动开销），不是上下文唯一载体——这让崩溃恢复很干净。

### 4.2 单个 ClaudeProcess 状态机
```
        ┌──────────────────────────────────────────────────────┐
        │  ask() 首次触发：subprocess 拉起 claude（stream-json） │
        ▼                                                      │
   ┌──────────┐   写 stdin 一行 user 帧   ┌──────────┐         │
   │  READY   │ ─────────────────────────▶│   BUSY   │         │
   │ (空闲)   │ ◀───────────────────────── │(思考中)  │         │
   └──────────┘   读到 result 帧(聚合完成) └──────────┘         │
        │                                                      │
        │ 空闲 > IDLE_TTL(15min)    BUSY 超 ASK_TIMEOUT(120s) ──┤
        ▼                                                      │
   TERMINATE(优雅 term→kill)        KILL → SPAWN(--resume)重试1次│
        │                                                      │
        ▼                                          重试仍失败 → 回错误文本，不无限重试
   (回收，session_id 仍在 store)                    进程意外 EOF → 同上 --resume 路径
```
- **同会话串行**：每个 `ClaudeProcess` 自带 `asyncio.Lock`，第二条消息等第一条 `result` 出来才进。
- **跨会话并行**：池里不同 `conversationId` 是不同进程，天然并行。

### 4.3 指令（safety 层拦截，不进 pool）
| 指令 | 行为 |
|---|---|
| `/new` | 杀该会话进程 + 清 `session_store` 映射 → 下条消息开全新会话 |
| `/status` | 回复池状态：活跃会话数、各会话 READY/BUSY、最近活跃时间 |
| `/help` | 列出指令 + 当前能力档（全放行） |

---

## 5. stream-json 双流协议（claude CLI 接口事实）

```
stdin（每行一个 JSON，进程内不可 EOF，否则 claude 退出）:
  {"type":"user","message":{"role":"user","content":[{"type":"text","text":"<钉钉消息文本>"}]}}

stdout（每行一个 JSON 事件，逐行读）:
  {"type":"assistant","message":{...}}          ← 增量文本（累加）
  {"type":"system", ...}                         ← 工具调用状态（全放行档会出现 Read/Write/Edit/Bash）
  {"type":"result","subtype":"success",          ← ★一轮结束标志
    "result":"<最终文本>","session_id":"<sid>",   ← 从这里抓 session_id 存 store
    "is_error":false}
```
聚合策略：累加 `assistant` 帧文本，**最终以 `result.result` 为权威输出**。一轮终止条件 = 读到 `type=="result"`。

> ⚠️ **事实防幻觉**：上述帧结构是 claude CLI stream-json 的既定形态，但字段细节会随版本微调。**实现第一件事**就是跑一次 `claude -p "hi" --output-format stream-json --verbose` 把真实帧抓下来核对，再固化到 `claude_pool.py` 的解析器——绝不凭记忆写解析逻辑（CLAUDE.md「无幻觉」红线）。同理 `--permission-mode bypassPermissions` 等参数以 `claude --help` 当前输出为准。

---

## 6. dingtalk-stream SDK 接入事实

> ⚠️ **事实防幻觉**：以下类名/字段为 `dingtalk-stream` 官方 SDK 的标准形态，但字段细节（`sender_staff_id` vs `sender_id`、reply 方法名）以**实现时安装的 SDK 版本**为准，落地代码前先 `pip show dingtalk-stream` + 读其源码确认。

装配骨架（仅示意，非最终代码）：
```python
import dingtalk_stream
from dingtalk_stream import AckMessage, ChatbotMessage

class BridgeHandler(dingtalk_stream.ChatbotHandler):
    async def process(self, callback):
        msg = ChatbotMessage.from_dict(callback.data)
        # msg.text.content / msg.sender_staff_id / msg.conversation_id
        # → safety.classify → ACK → asyncio.create_task(派发)
        return AckMessage.STATUS_OK, 'OK'

credential = dingtalk_stream.Credential(cfg.app_key, cfg.app_secret)
client = dingtalk_stream.DingTalkStreamClient(credential)
client.register_callback_handler(ChatbotMessage.TOPIC, BridgeHandler(...))
await client.start_forever()
```
- **鉴权字段**：白名单按 `sender_staff_id`（或 `sender_id`/`sender_union_id`，以 SDK 实际字段为准）匹配。
- **回复**：handler 自带 reply 方法（`@`回复原消息）；长消息分段后逐条 reply。
- **ACK**：`return AckMessage.STATUS_OK` 必须快速返回，否则钉钉重投——故重活（claude）走 `asyncio.create_task` 异步派发。

---

## 7. 与"PyCharm 终端 claude"的一致性边界

用户诉求是"和终端一致"。诚实区分三层：

| 层 | 是否一致 | 说明 |
|---|---|---|
| 能力/上下文/配置 | **完全一致** | 同工作目录（项目根）、同 `CLAUDE.md`（项目+全局）、同 `~/.claude/settings.json`、同 MCP servers、同默认模型、同工具集（全放行不收敛）、同历史 session（`--resume`） |
| 交互范式 | **必然不同（对用户透明）** | 终端是 REPL（TTY/彩色/按键），无法被子进程驱动；bridge 用 stream-json。但钉钉里看到的回答文本与终端内容一致 |
| 权限 | **一致（全放行）** | `bypassPermissions` = 终端里每个确认都按 y 的效果 |

---

## 8. 安全契约（全放行模式的固有对价）

`--permission-mode bypassPermissions` = claude 自主决策、自主执行工具，不问、不等。

**事前拦不住（技术事实）**：stream-json 模式下，工具调用事件是"正在执行/已完成"的通知，bridge 看到时 claude 已动手（除非用 Agent SDK 在工具层 hook，而本项目刻意不引 SDK）。若 claude 幻觉执行 `rm`、误改 `trading/`、误触下单函数——**bridge 拦不住，只能事后发现**。

**三道非拦截性纵深防御（做满）**：
1. **白名单铁闸（唯一身份闸）**：仅 `DINGTALK_ALLOWED_STAFF_IDS` 内 staffId 可触发；非白名单**静默丢弃 + 审计**（不回执，防探测）。
2. **全量审计**：每条消息落 `logs/dingtalk_bridge_audit.jsonl`——时间、sender_staff_id、conversation_id、消息文本、claude 工具调用序列、耗时、结果摘要、status。
3. **高危模式实时告警**：工具调用命中敏感模式（`trading/`、`emt_gateway`、`qmt`、`.env`、`rm `、`git push`、下单函数名）→ 实时复用 `core/notifier` 推钉钉告警给用户自己。事后审计变事中知情。

> 降级路径：若哪天"事前拦不住"不可接受，把 spawn 参数从 `bypassPermissions` 改回 `acceptEdits`（或 `plan`）即可，bridge 其余全部不动。

---

## 9. 错误处理矩阵

| 故障 | 检测 | 处置 |
|---|---|---|
| Stream WebSocket 断线 | SDK 自带 | `dingtalk-stream` 内置指数退避重连；连续失败记 CRITICAL |
| claude 子进程崩溃(EOF/非0退出) | stdout EOF / returncode≠0 | `--resume` 重建重试 **1 次**；仍失败回错误文本；**不无限重试** |
| 单轮超时 >120s | `asyncio.wait_for` | kill → `--resume` 重建 → 重试 1 次 |
| `session_id` 失效（本地历史被清） | `--resume` 报错 | 降级为全新会话（去掉 `--resume` 重 spawn）+ 更新 store |
| 非白名单消息 | `safety.classify` | 静默丢弃 + 审计日志（不回执） |
| 钉钉侧频控 >10条/60s | 滑窗计数 | 回"太快了，稍候" |
| 输出超长 | 字符数 | `replier` 分段（≤1800 字/条） |
| reply 投递失败 | httpx/aiohttp 异常 | 重试 2 次 + 审计记"投递失败" |

---

## 10. 可观测性

- `logs/dingtalk_bridge.log`：运行日志，复用项目 `LOG_CONFIG` 格式（`时间 | 级别 | logger | 消息`）。
- `logs/dingtalk_bridge_audit.jsonl`：全量审计（见 §8 契约②）。
- 高危告警：见 §8 契约③。
- `/status` 指令：回池状态（活跃会话数、各 READY/BUSY、最近活跃）。

---

## 11. 配置清单（`.env` 新增，`.env.example` 同步留空模板）

```dotenv
# ============ 钉钉企业内部应用（Stream 双向桥） ============
DINGTALK_APP_KEY=                          # 企业内部应用 Client ID（AppKey）
DINGTALK_APP_SECRET=                       # Client Secret（AppSecret），本地填真值，绝不进 git
DINGTALK_ALLOWED_STAFF_IDS=                # 白名单 staffId，逗号分隔；全放行模式下唯一身份闸
# ============ bridge 行为 ============
CLAUDE_BIN=claude                          # claude CLI 可执行，默认走 PATH
CLAUDE_WORKDIR=                            # claude 工作目录，默认项目根 quanter/
BRIDGE_ASK_TIMEOUT=120                     # 单轮超时（秒）
BRIDGE_IDLE_TTL=900                        # 空闲进程回收（秒，默认 15min）
BRIDGE_RATE_LIMIT_PER_MIN=10               # 单用户每分钟消息上限
```

---

## 12. 测试策略

| 层 | 对象 | 方式 |
|---|---|---|
| 单测 | `safety`、`session_store` | 纯逻辑：白名单匹配、指令解析、映射读写、文件锁 |
| 单测 | `claude_pool` | mock `asyncio.subprocess`（不真跑 claude）：状态机/串行锁/超时杀/崩溃重建/session 失效降级 |
| 单测 | `replier` | 分段逻辑、Markdown 清洗 |
| 集成 | `stream_client` | mock dingtalk-stream 的 `ChatbotMessage`，跑"收消息→派发→回复"全链路（pool 用 fake） |
| E2E | 全栈 | 手动：真连钉钉 + 真 claude，发"hi"看回环。写进 README，**不进 CI**（依赖凭证/网络） |

测试文件位置：`tests/bridge/test_*.py`（沿用项目 `tests/<模块>/` 惯例）。

---

## 13. 非目标（YAGNI）

明确**不做**的事，防过度设计：
- **不做**权限确认透传钉钉（`/y` `/n` 交互式审批）——全放行下无需，且 stream-json 的权限事件流向未验证，YAGNI。
- **不做**多用户隔离/配额管理——白名单内即信任，不引入租户模型。
- **不做**Web 管理面板——`/status` 指令够用。
- **不做**claude 输出彩色渲染——钉钉只支持有限 Markdown，清洗成纯 Markdown 即可。
- **不做**嵌入 FastAPI lifespan——旁路与交易后端解耦（见 §1 决策）。
- **不做**健康探活 HTTP 端口——独立进程，`/status` 指令 + 日志够用。

---

## 14. 落地顺序提示（供 writing-plans 细化）

1. `.env.example` 追加配置项；本地 `.env` 填真值。
2. `bridge/config.py` + `bridge/safety.py` + `bridge/session_store.py`（纯逻辑，先单测）。
3. 抓 claude stream-json 真实帧 → `bridge/claude_pool.py`（核心，重点测状态机）。
4. `bridge/replier.py`（分段+清洗）。
5. `bridge/stream_client.py` + `bridge/__main__.py` + `scripts/dingtalk_claude_bridge.py`（装配）。
6. 集成测 + 手动 E2E（真钉钉回环）。
7. README 补一段"如何启动钉钉桥"。
