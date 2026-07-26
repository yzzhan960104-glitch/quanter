# broadcast 升格为钉钉机器人总管 + market 下线 设计

- **日期**：2026-07-26
- **状态**：待评审
- **范围**：`broadcast` 包架构升级（单向播报 → 机器人总管）+ `market`（每日行情播报）机器人下线删除
- **不涉及**：自动交易引擎、uvicorn 服务、training loop 本身（仅收编其钉钉接入的"配置与拉起"）

---

## 1. 背景与动机

项目当前有两套并行的"机器人"体系，配置散落、职责混淆：

1. **broadcast 包（单向播报）**：`python -m broadcast --bot X` 生成文案 → `push_brief` 推一条 Markdown → 退出。现管 4 个 bot：`market`/`trading`/`data`/`strategy`。
2. **dws `dev connect` 常驻（双向对话）**：5 个对话机器人，靠 `scripts/start_dingtalk_bots.md` 里**手工命令**启动，配置散落在文档/README，**代码层零管理**：
   - `yzzhanCli通用`（CLI 大脑，`--channel claudecode`）——其 `unified-app-id` 连 `.env` 都没存，只写在文档里
   - `quanter交易`/`quanter数据`/`quanter策略`（专业 @查询，`--channel claudecode`）
   - `yzzhan参数优化`（training loop 人审桥，`--channel custom --agent-cmd`）

**痛点**：
- `yzzhanCli通用` 等 5 个对话机器人是项目核心入口，却无代码/配置管理，全靠手抄文档命令，易漂移、易漏配（`--agent-workdir` 省略就跑空白 Temp、不了解项目，文档明确"已踩坑"）。
- `market`（每日行情播报）机器人：`broadcast` 配置里有，但**既没进常驻清单也没进 schtasks**，等于未排程的死配置；与设计文档"三机器人各司其职"的后续演进不一致。

**目标**：
- broadcast 升格为**钉钉机器人总管**：统一登记 + 拉起全部 8 个机器人（3 播报 + 5 对话），消除文档散落。
- `market` 下线删除（代码 / 配置 / 文档 / 钉钉侧资源）。

---

## 2. 已确认的设计决策（用户拍板）

| # | 决策 | 选择 |
|---|---|---|
| D1 | yzzhanCli通用 调整到 broadcast 的程度 | **broadcast 能拉起它**（不只登记配置，要 subprocess 拉起 dev connect）|
| D2 | 收编范围 | **全部 5 个 dev connect 对话机器人**（cli + trading_q/data_q/strategy_q + review）|
| D3 | 进程生命周期 | **后台 detach + 统一管理**（PID 文件 + 日志 + `--start/--stop/--status/--logs`，接受 Windows 进程管理复杂度）|
| D4 | bot key 命名 | push 类 `trading/data/strategy`；connect 类 `cli`/`trading_q`/`data_q`/`strategy_q`/`review` |
| D5 | CLI 兼容 | 无子命令仍 = push（`manage_ops_schtasks.py` 生成的 `python -m broadcast --bot trading` 零改动）|
| D6 | market 删除边界 | 代码 + 配置 + 文档全清；**钉钉侧资源移除由用户执行**（AI 不替按外向动作）；钉钉应用是否彻底删由用户定 |

---

## 3. 总体架构

broadcast 从"单向播报包"升格为**机器人总管**，统管 8 个机器人，按生命周期分两类、走两条独立管线：

| 类别 | 机器人（bot key） | 管线 | 生命周期 | 触发 |
|---|---|---|---|---|
| **push（播报）** | `trading` / `data` / `strategy` | `_build_brief → push_brief`（生成文案→推送→退出）| 一次性 | schtasks 到点 / 手工 `--force` |
| **connect（对话）** | `cli` / `trading_q` / `data_q` / `strategy_q` / `review` | `dws dev connect` 后台常驻 | 长期驻留 | `broadcast connect --start` |

两类 bot 的启动参数、字段、运行模式完全不同，配置结构与 CLI 路由均**分类隔离**，互不污染。

---

## 4. 详细设计

### 4.1 配置结构：分类字典

现有 `_BOT_CFG`（播报专用 `{robot_env, last, title}`）拆成两个字典 + 一个共用默认值常量。

```python
# === 播报类（push）===
PUSH_BOTS = {
    "trading":  {"robot_env": "TRADING_BOT_ROBOT_CODE",   "last": ".last_trading_brief",  "title": "💰 每日交易播报"},
    "data":     {"robot_env": "DATA_BOT_ROBOT_CODE",      "last": ".last_data_brief",     "title": "🗄 每日数据播报"},
    "strategy": {"robot_env": "STRATEGY_BOT_ROBOT_CODE",  "last": ".last_strategy_brief", "title": "♟ 每日策略播报"},
}
SUPPORTED_BOTS = tuple(PUSH_BOTS.keys())  # ("trading","data","strategy") —— market 移除

# === 对话类（connect）===
CONNECT_BOTS = {
    "cli":         {"unified_env": "CLI_BOT_UNIFIED_APP_ID",        "channel": "claudecode"},  # yzzhanCli通用
    "trading_q":   {"unified_env": "TRADING_BOT_UNIFIED_APP_ID",    "channel": "claudecode"},  # quanter交易
    "data_q":      {"unified_env": "DATA_BOT_UNIFIED_APP_ID",       "channel": "claudecode"},  # quanter数据
    "strategy_q":  {"unified_env": "STRATEGY_BOT_UNIFIED_APP_ID",   "channel": "claudecode"},  # quanter策略
    "review":      {"unified_env": "REVIEW_BOT_UNIFIED_APP_ID",     "channel": "custom",
                    "agent_cmd": ".venv310/Scripts/python.exe scripts/dingtalk_review_bridge.py"},  # yzzhan参数优化
}
# claudecode 类共用的 dev connect 启动参数（DRY，不每 bot 重复）
CONNECT_DEFAULTS = {
    "allowed_users_env": "DINGTALK_ALLOWED_STAFF_IDS",  # 身份闸（.env 已配）
    "workdir_env":       "BROADCAST_AGENT_WORKDIR",     # 工作目录（新增，=F:/quanter）
    "agent_memory":      True,
    "approval_mode":     "ask",  # 审批闸，写死，不为任何 bot 开口子
}
```

**设计约束**：
- `CONNECT_DEFAULTS.approval_mode="ask"` 是安全底线，**绝不在代码里给任何 bot 留省略/覆盖口子**——省略 = 钉钉一句话驱动本机 Claude Code 自动改代码/跑高危命令。
- `agent_cmd` 仅 `channel=custom` 的 review 机器人有；claudecode 类不带（dev connect 自动拉起 Claude Code）。
- `agent_cmd` 用**相对路径**（`.venv310/Scripts/python.exe scripts/...`），靠 dev connect 的 cwd 锁项目根，**不照抄 `start_dingtalk_bots.md` 里的旧桌面绝对路径**（项目已迁到 F 盘）。

### 4.2 CLI 形态：向后兼容的子命令

```
python -m broadcast --bot trading [...]          # 无子命令 = push（兼容！schtasks bat 零改动）
python -m broadcast push --bot trading [...]     # 显式 push（等价上行）
python -m broadcast connect --start <bot|all>    # 后台拉起对话机器人
python -m broadcast connect --stop  <bot|all>    # 停止（树状杀）
python -m broadcast connect --status             # 全部 connect bot 状态 + 僵尸清理
python -m broadcast connect --logs <bot>         # 查日志（tail）
```

**兼容实现**：argparse 解析时，若第一个位置参数是 `push`/`connect` 则走子命令；否则按原 `--bot` 默认 push 路径（保留 `--bot`/`--date`/`--dry-run`/`--force` 语义不变）。

**回归红线**：无子命令 = push 必须保持，否则 `manage_ops_schtasks.py` 生成的 3 个 schtasks（`QuanterTradingBrief`@15:30 等）全断。

### 4.3 后台进程管理（Windows 实现）

新增 `broadcast/connect_manager.py`（纯进程管理，不依赖 dws/钉钉，可单测）。

| 能力 | 实现 |
|---|---|
| 拉起 | `subprocess.Popen(cmd, creationflags=CREATE_NEW_PROCESS_GROUP\|DETACHED_PROCESS)`，stdout/stderr 重定向到日志文件 |
| PID 文件 | `logs/broadcast_connect/<bot>.pid`（只存 PID 整数）|
| 日志 | `logs/broadcast_connect/<bot>.log`（dev connect 原始输出）|
| 防重复 | `--start` 前查 PID 文件 + 存活探测（Windows：`tasklist /FI "PID eq <pid>"`；进程已死则清文件视为未跑）|
| 停止 | 读 PID → `taskkill /F /T /PID <pid>`（**`/T` 树状杀**，连 dev connect 拉起的 Claude Code 子进程一并终止，否则留孤儿继续吃资源）|
| 僵尸清理 | `--status` 遍历所有 PID 文件，进程已死 → 删 PID 文件 + 标记 `dead` |
| `--all` | 遍历 `CONNECT_BOTS` 批量 start/stop；`--start all` 前打印清单 + 要求二次确认（防误启 5 个 Claude Code 实例） |

**命令组装**（`connect_manager.build_cmd(bot_cfg, defaults) -> list[str]`）：
- claudecode 类：`dws dev connect --unified-app-id <env> --channel claudecode --agent-memory --agent-approval-mode ask --allowed-users <env> --agent-workdir <env>`
- custom 类（review）：`dws dev connect --unified-app-id <env> --channel custom --agent-cmd "<agent_cmd>" --allowed-users <env>`

**边界拷问**：
- `taskkill /T` 是 Windows 树杀的关键；漏 `/T` = dev connect 死了但 Claude Code 子进程还活着（孤儿）。
- PID 文件 + 存活探测必须配合：只看 PID 文件存在不等于在跑（进程崩溃留死文件）。
- 5 个 claudechannel = 5 个 Claude Code 常驻实例，内存/API 并发压力（既有设计延伸，非本次引入）；`--start all` 加二次确认缓解误启。

### 4.4 配置收编（.env 单一真相源）

| 机器人 | unified-app-id 当前来源 | 动作 |
|---|---|---|
| `cli`（yzzhanCli通用）| 仅文档 `start_dingtalk_bots.md`/README | **新增** `CLI_BOT_UNIFIED_APP_ID` 入 `.env` |
| `review`（yzzhan参数优化）| 仅文档 `start_dingtalk_bots.md` | **新增** `REVIEW_BOT_UNIFIED_APP_ID` 入 `.env` |
| `trading_q`/`data_q`/`strategy_q` | `.env` 已有 `*_BOT_UNIFIED_APP_ID` | 复用 |
| 全部 connect 类 | — | 共用 `DINGTALK_ALLOWED_STAFF_IDS`（已有）+ **新增** `BROADCAST_AGENT_WORKDIR=F:/quanter` |

`.env` / `.env.example` 新增：
```dotenv
# 对话机器人（dev connect 常驻）——从 start_dingtalk_bots.md 收编，消除文档散落
CLI_BOT_UNIFIED_APP_ID=<yzzhanCli通用的 unified-app-id>
REVIEW_BOT_UNIFIED_APP_ID=<yzzhan参数优化的 unified-app-id>
BROADCAST_AGENT_WORKDIR=F:/quanter
```
（具体 unified-app-id 值由实施时从 `start_dingtalk_bots.md` / `dws dev app list` 填入；spec 不固化敏感值。）

`start_dingtalk_bots.md` 中 5 个 dev connect 的启动命令改为"参见 `python -m broadcast connect --start <bot>`"，文档降级为运维参考，配置真相源移至 `.env` + `broadcast/__main__.py:CONNECT_BOTS`。

### 4.5 market 下线删除清单

#### 代码层
- **`broadcast/__main__.py`**：
  - `_BOT_CFG` → 拆为 `PUSH_BOTS`/`CONNECT_BOTS`（§4.1），删 `market` 条目
  - `SUPPORTED_BOTS` 移除 `market`；`--bot default` 由 `"market"` 改 `"trading"`
  - `_build_brief` 删 `if bot == "market"` 分支
  - 删 `from broadcast.brief import build_daily_brief`（行 27）
  - `LAST_BC_FILE` 旧别名 + `last_brief_file("market")` 特例 + `_read_last_broadcast`/`_write_last_broadcast` 兼容函数：随 market 主流程测试一起清（见测试层）
- **`broadcast/brief.py`（外科手术，不整删！）**：
  - ❗ `brief_data.py`/`brief_trading.py`/`brief_strategy.py` 共享 `from broadcast.brief import BriefResult, _clean_markdown, _weekday_zh`，**这三个必须保留**
  - **删 market 专用**：`build_daily_brief`、`_section_index`/`_section_ths`/`_fmt_board`/`_section_moneyflow`/`_section_dragon`、`_fmt_pct`/`_safe_tail2`/`_start_date`、`_PCT_LOOKBACK_DAYS`、`name_resolver`/`DataLakeReader` 等仅 market 用的 import
  - **保留共享**：`BriefResult`（dataclass）、`_clean_markdown`、`_weekday_zh`
  - 文件头注释从"每日行情播报文案生成器"改为"brief 共享工具（BriefResult / Markdown 清洗 / 中文周几）"
  - 备选：若希望模块职责更清晰，把三个共享工具抽到 `broadcast/brief_common.py`，三个 `brief_*.py` 改 import 源，`brief.py` 整删——**默认不做（YAGNI），保留原地最小改动**

#### 测试层
- `tests/test_broadcast_brief.py`：**整删**（market 文案单测，5 处调 `build_daily_brief`）
- `tests/test_broadcast_main.py`：**重构**（现 mock `build_daily_brief` + patch `LAST_BC_FILE` 测 market 主流程 → 改用 `trading` bot 测"幂等/push/last"主流程，保留核心回归）
- `tests/broadcast/test_cli_routing.py`：删 `last_brief_file("market")` 断言行；补 `push`/`connect` 子命令路由断言

#### 配置层
- `.env` / `.env.example`：删 `DINGTALK_CHAT_ROBOT_CODE`
- `logs/.last_market_brief`：运行时幂等文件（部署侧删，不入库）

#### 文档层
- `scripts/setup_broadcast_bot.md`：**整删或归档**（market 专属建号文档，2026-07-16 Task 0）
- `scripts/setup_broadcast_schtasks.md`：**整删或归档**（market 专属 19:00 schtasks 文档；注意与现役的 `manage_ops_schtasks.py` 无关）
- `docs/superpowers/specs/2026-07-16-daily-market-brief-design.md` + `plans/2026-07-16-daily-market-brief.md`：顶部加"已废弃（market 下线，2026-07-26）"标注，不删（历史溯源）
- `scripts/start_dingtalk_bots.md`：删 market 相关段落，5 个 dev connect 段落改为引用 broadcast connect

#### 钉钉侧（外向 ⚠️，由用户执行，AI 不替按）
```bash
# 1. 从 yzzhan量化群移除 market 机器人
dws chat group members remove-bot --robot-code dingdya5o94mnmde7jlv --id ciduznBwLLiWKcMewBOF4+kWQ== -y
# 2.（可选，更彻底）删除 market dws 应用 —— 命令实施时用 dws dev app list 查 app-id 后走 dws dev app delete
```

---

## 5. 测试策略

- **push 类**：`test_cli_routing.py` 调整（去 market、加 `push`/`connect` 子命令路由）；`test_broadcast_main.py` 重构为 trading bot 主流程。
- **connect 类**：新增 `tests/broadcast/test_connect_manager.py`，**全程 mock `subprocess.Popen`**（不真拉 dev connect），覆盖：
  - `build_cmd` 命令组装（claudecode vs custom 两类参数正确性）
  - PID 文件读写
  - 防重复（PID 存在 + 进程存活 → 跳过；进程已死 → 清文件后允许）
  - `--stop` 组装 `taskkill /F /T /PID`（验证 `/T` 在场）
  - `--status` 僵尸清理逻辑
- **回归红线**：无子命令 = push 必须有测试守护（防 schtasks 断）。

---

## 6. 风险与拷问

| # | 风险 | 缓解 |
|---|---|---|
| R1 | 5 个 claudechannel = 5 个 Claude Code 常驻，内存/API 并发压力 | 既有设计延伸；`--start all` 二次确认；spec 标注，不做自动重启/健康检查（YAGNI）|
| R2 | `--stop` 漏 `taskkill /T` → 孤儿 Claude Code | 代码常量 + 测试断言 `/T` 在场 |
| R3 | 审批闸 `ask` 被绕过 → 钉钉一句话自动改代码 | `CONNECT_DEFAULTS.approval_mode` 写死、不暴露 CLI 覆盖口子；测试守护 |
| R4 | CLI 子命令化破坏 schtasks 兼容 | 无子命令=push 兼容 + 回归测试守护（D5 红线）|
| R5 | `brief.py` 外科手术误删共享工具 → 三个 brief 崩 | 删除清单显式列出"保留 BriefResult/_clean_markdown/_weekday_zh"；CI 跑 brief_data/trading/strategy 单测验回归 |
| R6 | `agent_cmd` 用旧桌面绝对路径 → 找不到 python.exe | 用相对路径 + dev connect cwd 锁项目根 |
| R7 | 4 个 claudechannel 共享 workdir `F:/quanter`，并发改同文件冲突 | 审批闸 `ask` 挡无意识同时改；职责隔离（cli 通用对话 vs _q 专业查询）；spec 标注为已知约束 |

---

## 7. 不做（YAGNI）

- ❌ connect 机器人的自动重启 / 崩溃健康检查 / 日志轮转（先跑通 start/stop/status/logs 四件套）
- ❌ 进程资源监控（CPU/内存告警）
- ❌ `brief.py` 共享工具抽到 `brief_common.py`（原地最小改动优先）
- ❌ market dws 应用的自动删除（外向动作，用户手工）
- ❌ connect 机器人的 schtasks 开机自启（常驻进程托管已够，不叠加系统任务）

---

## 8. 验收标准

- [ ] `python -m broadcast --bot trading` 行为不变（schtasks 兼容红线）
- [ ] `python -m broadcast connect --start cli` 后台拉起 yzzhanCli通用，PID 文件落盘、日志可查
- [ ] `--status` 正确显示 5 个 connect bot 状态，清僵尸
- [ ] `--stop <bot>` 树状杀干净（无孤儿 Claude Code）
- [ ] market 在代码/配置/文档中全清；`broadcast/brief.py` 仅保留共享工具，三个 brief 单测全绿
- [ ] 新增 `test_connect_manager.py` 全绿（mock Popen，不真连钉钉）
- [ ] `.env` 新增 `CLI_BOT_UNIFIED_APP_ID`/`REVIEW_BOT_UNIFIED_APP_ID`/`BROADCAST_AGENT_WORKDIR`；`start_dingtalk_bots.md` 指向 broadcast connect
