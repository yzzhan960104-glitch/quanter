# broadcast 升格为钉钉机器人总管 + market 下线 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `broadcast` 从单向播报包升格为钉钉机器人总管（统管 3 个 push 播报 + 5 个 connect 对话机器人，新增 `connect` 子命令做后台进程托管），并下线删除 `market`（每日行情播报）机器人的代码/配置/文档。

**Architecture:** `broadcast/__main__.py` 配置拆为 `PUSH_BOTS`（trading/data/strategy，走 `_build_brief → push_brief → 退出`）与 `CONNECT_BOTS`（cli/trading_q/data_q/strategy_q/review，走 `dws dev connect` 后台常驻）。新增 `broadcast/connect_manager.py`（纯进程管理：Popen 拉起 + PID 文件 + 日志 + `taskkill /T` 树杀 + 僵尸清理，不依赖 dws/钉钉，可全程 mock 单测）。CLI 子命令化向后兼容：首参为 `push`/`connect` 走子命令，否则默认 push（schtasks 生成的 `python -m broadcast --bot trading` 零改动）。

**Tech Stack:** Python 3.10 stdlib（argparse / subprocess / pathlib / dataclasses / os），零新依赖。Windows 进程管理原语（`tasklist /FI`、`taskkill /F /T`、`CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS`）。

## Global Constraints

> 以下为 spec 全局约束，逐字抄录，每个任务的隐含前置：

- **C1（D5 回归红线）**：`python -m broadcast --bot trading`（无子命令）必须仍走 push —— `scripts/run_trading_brief.bat` / `manage_ops_schtasks.py` 生成的 3 个 schtasks 零改动。
- **C2（R3 安全底线）**：`CONNECT_DEFAULTS.approval_mode="ask"` 写死，**绝不在代码里给任何 bot 留省略/覆盖口子**。
- **C3（R2 防孤儿）**：`stop` 必须用 `taskkill /F /T /PID`，`/T` 树杀 dev connect 拉起的 Claude Code 子进程；代码常量 + 测试断言 `/T` 在场。
- **C4（R6 相对路径 + cwd 锁根）**：review 的 `agent_cmd` 用相对路径 `.venv310/Scripts/python.exe infra/tools/dingtalk_review_bridge.py`（**修正 spec §4.1 误写的 `scripts/`**）；`connect_manager.start()` 在 `subprocess.Popen` 时显式传 `cwd=PROJECT_ROOT`（化解 `start_dingtalk_bots.md:36`「dws cwd 非项目根、相对路径踩坑」教训）。
- **C5（R5 共享工具保留）**：`broadcast/brief.py` 外科手术必须保留 `BriefResult` / `_clean_markdown` / `_weekday_zh`（三个 `brief_*.py` 共享 import）；CI 跑 `test_brief_data/trading/strategy` 验回归。
- **C6（D6 外向动作边界）**：钉钉侧移除 market 机器人/应用由用户手工执行，AI 不替按。
- **C7（CLAUDE.md 语言/审美）**：所有对话、文档、代码注释 100% 中文；显式逻辑、无黑盒依赖；每个 connect 类 bot 的边界拷问（断线/限流/孤儿）在代码注释里标明。
- **C8（spec 范围修正）**：`.env.example` 本无 `DINGTALK_CHAT_ROBOT_CODE`（market 凭证只在 `.env` 实文件），故「删 market 凭证」落在 `.env` 实文件 + `__main__.py` 配置，`.env.example` 仅做**新增** 3 项 + 改注释，无需删该项。

---

## File Structure

| 文件 | 动作 | 责任 |
|------|------|------|
| `broadcast/brief.py` | **改（外科手术）** | 删 market 专用（`build_daily_brief`/四节/`_fmt_*`/`_safe_tail2`/`_start_date`/`_PCT_LOOKBACK_DAYS`/`name_resolver` import/`timedelta`）；保留 `BriefResult`/`_clean_markdown`/`_weekday_zh` |
| `broadcast/connect_manager.py` | **新建** | 纯进程管理：`build_cmd` / `start` / `stop` / `status` / `_is_alive` / PID 文件读写 |
| `broadcast/__main__.py` | **改（核心）** | `_BOT_CFG`→`PUSH_BOTS`+`CONNECT_BOTS`+`CONNECT_DEFAULTS`；删 market；`--bot` 默认 `trading`；CLI 子命令路由；`connect` 子命令接入 connect_manager |
| `tests/test_broadcast_brief.py` | **删** | market 文案单测（5 处调 `build_daily_brief`），market 下线即整删 |
| `tests/test_broadcast_main.py` | **改（重构）** | market 主流程 → trading 主流程；去 `LAST_BC_FILE` patch，改 patch `last_brief_file` |
| `tests/broadcast/test_cli_routing.py` | **改** | 删 `last_brief_file("market")` 断言；补 `push`/`connect` 子命令路由断言 |
| `tests/broadcast/test_connect_manager.py` | **新建** | 全程 mock `subprocess`：`build_cmd` 两类、PID 读写、防重复、`stop` 树杀 `/T`、`status` 僵尸清理 |
| `.env.example` | **改** | 新增 `CLI_BOT_UNIFIED_APP_ID` / `REVIEW_BOT_UNIFIED_APP_ID` / `BROADCAST_AGENT_WORKDIR`；注释「3 播报 + 3 查询」→「3 播报 + 5 对话」 |
| `.env`（实文件，不入库） | **改（用户侧）** | 同上新增 + 删 `DINGTALK_CHAT_ROBOT_CODE` |
| `scripts/start_dingtalk_bots.md` | **改** | 5 个 dev connect 段落改为引用 `python -m broadcast connect --start <bot>`；删 market 段；文档降级为运维参考 |
| `scripts/setup_broadcast_bot.md` | **归档** | market 专属建号文档 → 移至 `scripts/archive/` |
| `scripts/setup_broadcast_schtasks.md` | **归档** | market 专属 19:00 schtasks 文档 → 移至 `scripts/archive/` |
| `docs/superpowers/specs/2026-07-16-daily-market-brief-design.md` | **改（标注）** | 顶部加「已废弃（market 下线 2026-07-26）」 |
| `docs/superpowers/plans/2026-07-16-daily-market-brief.md` | **改（标注）** | 同上 |

---

## Task 1: brief.py 外科手术 —— 删 market 专用，保留共享工具

**Files:**
- Modify: `broadcast/brief.py`（整文件改写为共享工具）
- Delete: `tests/test_broadcast_brief.py`

**Interfaces:**
- Consumes: 无（共享工具自包含）
- Produces: `broadcast/brief.py` 保留 `BriefResult`（dataclass）、`_clean_markdown(text:str)->str`、`_weekday_zh(date:str)->str` 三个符号，供 `brief_data.py`/`brief_trading.py`/`brief_strategy.py` 继续使用。

- [ ] **Step 1: 跑基线，确认三个 brief_* 单测当前全绿（共享工具被覆盖）**

Run: `.venv310/Scripts/python.exe -m pytest tests/broadcast/test_brief_data.py tests/broadcast/test_brief_trading.py tests/broadcast/test_brief_strategy.py -v`
Expected: 3 个文件全 PASS（这是 C5 回归守护的基线）。

- [ ] **Step 2: 改写 `broadcast/brief.py` 为共享工具模块**

整文件替换为：

```python
# -*- coding: utf-8 -*-
"""brief 共享工具（BriefResult / Markdown 清洗 / 中文周几）。

历史：本模块曾是「每日行情播报文案生成器」（build_daily_brief + 大盘/板块/资金流/龙虎榜
四节）。2026-07-26 market 机器人下线，market 专用代码全删；保留下列三个被
brief_data / brief_trading / brief_strategy 共享 import 的工具：
  - BriefResult：播报结果 dataclass（date + markdown）
  - _clean_markdown：钉钉 Markdown 防御性清洗（去 <font>/<br>/表格分隔行）
  - _weekday_zh：日期 → 中文周几

物理定位：纯函数·零 IO 副作用·可单测。任一调用方失败均由调用方自己降级，本模块不抛。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime


@dataclass
class BriefResult:
    """播报结果（纯数据，供 __main__ 推送/日志/去重）。"""

    date: str       # 播报日（应播日）
    markdown: str   # 拼好并清洗的钉钉 Markdown 文案


def _weekday_zh(date: str) -> str:
    """日期 → 中文周几（如「周二」；解析失败返空串，不抛）。"""
    try:
        return "周" + "一二三四五六日"[datetime.strptime(date, "%Y-%m-%d").weekday()]
    except Exception:
        return ""


def _clean_markdown(text: str) -> str:
    """钉钉 Markdown 防御性清洗（内联，避免 broadcast→caisen 跨包耦合）。

    钉钉群机器人 Markdown 不支持：<font>着色、<br>、表格分隔行 |---|、代码块。
    brief 本身只用 #/列表/粗体/引用（安全）；本函数防御板块/个股名内混入的特殊字符。
    """
    text = re.sub(r"<font[^>]*>|</font>", "", text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*\|[-:\s|]+\|\s*$", "", text, flags=re.MULTILINE)
    return text
```

> **删除清单（对照 spec §4.5）**：`build_daily_brief`、`_section_index`/`_section_ths`/`_fmt_board`/`_section_moneyflow`/`_section_dragon`、`_fmt_pct`/`_safe_tail2`/`_start_date`、`_PCT_LOOKBACK_DAYS`、`from broadcast import name_resolver as _default_resolver`、`import logging`/`logger`（删 market 后无人用）、`from datetime import timedelta`（仅 `_start_date` 用，`_weekday_zh` 只需 `datetime`）。

- [ ] **Step 3: 整删 `tests/test_broadcast_brief.py`（market 文案单测）**

Run: `git rm tests/test_broadcast_brief.py`（或 Windows 下 `del tests\test_broadcast_brief.py` 后 `git add -A`）

- [ ] **Step 4: 跑回归，确认共享工具仍在 + 三个 brief_* 单测全绿**

Run: `.venv310/Scripts/python.exe -m pytest tests/broadcast/test_brief_data.py tests/broadcast/test_brief_trading.py tests/broadcast/test_brief_strategy.py tests/test_broadcast_name_resolver.py -v`
Expected: 全 PASS（验证 C5：BriefResult/_clean_markdown/_weekday_zh 保留正确，name_resolver 独立模块未受影响）。

- [ ] **Step 5: 提交**

```bash
git add broadcast/brief.py tests/test_broadcast_brief.py
git commit -m "refactor(broadcast): brief.py 外科手术删 market 专用，保留 BriefResult/_clean_markdown/_weekday_zh 共享工具

- 删 build_daily_brief + 大盘/板块/资金流/龙虎榜四节 + _fmt_* / _safe_tail2 / _start_date / _PCT_LOOKBACK_DAYS
- 删 name_resolver import（仅 market 用）、timedelta import、logging/logger（删 market 后无人用）
- 保留三个 brief_*.py 共享的 BriefResult / _clean_markdown / _weekday_zh
- 整删 tests/test_broadcast_brief.py（market 文案单测）
- 三个 brief_* 单测 + name_resolver 单测全绿（C5 回归守护）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2: connect_manager.py —— dev connect 后台进程管理（TDD）

**Files:**
- Create: `broadcast/connect_manager.py`
- Test: `tests/broadcast/test_connect_manager.py`

**Interfaces:**
- Consumes: `CONNECT_BOTS`（dict，Task 3 定义）、`CONNECT_DEFAULTS`（dict，Task 3 定义）。本任务先用字面量 fixture 测，Task 3 接入真实常量后回归。
- Produces:
  - `build_cmd(bot:str, cfg:dict, defaults:dict) -> list[str]`：组装 dws dev connect 命令（claudecode vs custom 两类）。
  - `start(bot:str, cfg:dict, defaults:dict) -> str`：后台拉起，返 `"started"|"already_running"`。
  - `stop(bot:str) -> str`：树杀，返 `"stopped"|"not_running"`。
  - `status(bot:str) -> str`：返 `"running"|"dead"|"not_running"`，dead 则清 PID 文件。
  - `PROJECT_ROOT: Path`（模块级，`Path(__file__).resolve().parents[1]`，= `F:/quanter`，作 Popen cwd）。
  - `RUN_DIR: Path`（`Path("logs")/"broadcast_connect"`，PID/日志目录）。

- [ ] **Step 1: 写 `build_cmd` 失败测试（claudecode 类）**

`tests/broadcast/test_connect_manager.py`：

```python
# -*- coding: utf-8 -*-
"""connect_manager 单测：全程 mock subprocess，不真拉 dev connect / 不真调 tasklist|taskkill。

覆盖：build_cmd 两类命令组装 / PID 读写 / 防重复 / stop 树杀 /T / status 僵尸清理。
"""
from __future__ import annotations

import broadcast.connect_manager as cm

# ── fixture：等价 Task 3 的 CONNECT_BOTS / CONNECT_DEFAULTS（本任务先用字面量）──
CLS_CFG = {"unified_env": "CLI_BOT_UNIFIED_APP_ID", "channel": "claudecode"}
CUSTOM_CFG = {
    "unified_env": "REVIEW_BOT_UNIFIED_APP_ID",
    "channel": "custom",
    "agent_cmd": ".venv310/Scripts/python.exe infra/tools/dingtalk_review_bridge.py",
}
DEFAULTS = {
    "allowed_users_env": "DINGTALK_ALLOWED_STAFF_IDS",
    "workdir_env": "BROADCAST_AGENT_WORKDIR",
    "agent_memory": True,
    "approval_mode": "ask",
}


def test_build_cmd_claudecode(monkeypatch):
    """claudecode 类：全套 agent 参数，approval_mode 写死 ask，带 workdir。"""
    monkeypatch.setenv("CLI_BOT_UNIFIED_APP_ID", "f0b2740f-c029-4b99-943c-58de139c7463")
    monkeypatch.setenv("DINGTALK_ALLOWED_STAFF_IDS", "staff001")
    monkeypatch.setenv("BROADCAST_AGENT_WORKDIR", "F:/quanter")
    cmd = cm.build_cmd("cli", CLS_CFG, DEFAULTS)
    assert cmd[0:4] == ["dws", "dev", "connect", "--unified-app-id"]
    assert "f0b2740f-c029-4b99-943c-58de139c7463" in cmd
    assert "--channel" in cmd and "claudecode" in cmd
    assert "--agent-memory" in cmd                       # agent_memory=True
    assert "--agent-approval-mode" in cmd
    assert cmd[cmd.index("--agent-approval-mode") + 1] == "ask"  # C2 安全底线
    assert "--allowed-users" in cmd and "staff001" in cmd
    assert "--agent-workdir" in cmd and "F:/quanter" in cmd
```

- [ ] **Step 2: 跑测试，确认 ImportError 失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/broadcast/test_connect_manager.py::test_build_cmd_claudecode -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'broadcast.connect_manager'`）。

- [ ] **Step 3: 实现 `broadcast/connect_manager.py` 的 `build_cmd` + 模块骨架**

```python
# -*- coding: utf-8 -*-
"""dev connect 对话机器人后台进程管理（Windows · 纯进程管理 · 不依赖 dws/钉钉）。

物理定位：subprocess.Popen 拉起 dws dev connect 常驻 + PID 文件 + 日志 + taskkill /T 树杀。
零钉钉依赖（不 import dws/push/notifier），可独立单测（mock subprocess.Popen / tasklist / taskkill）。

命令组装（spec §4.3）：
- claudecode 类：dws dev connect --unified-app-id <env> --channel claudecode
  --agent-memory --agent-approval-mode ask --allowed-users <env> --agent-workdir <env>
- custom 类（review）：dws dev connect --unified-app-id <env> --channel custom
  --agent-cmd "<agent_cmd>" --allowed-users <env>

安全底线（spec R3 / C2）：approval_mode 永远取 defaults["approval_mode"]="ask"，
本模块不接受 cfg 覆盖——省略 = 钉钉一句话驱动本机 Claude Code 自动改代码/跑高危命令。

cwd 锁根（spec R6 / C4）：start() 在 Popen 时显式传 cwd=PROJECT_ROOT，
化解 start_dingtalk_bots.md「dws cwd 非项目根、相对 agent_cmd 踩坑」教训——
dev connect 继承项目根 cwd，review 的相对 agent_cmd 才能找到 python.exe 与桥脚本。
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# 项目根（broadcast/ 的上级 = F:/quanter）：作 Popen cwd，锁 dev connect 工作目录
PROJECT_ROOT = Path(__file__).resolve().parents[1]
# PID/日志目录（运行时幂等文件，.gitignore 已含 logs/）
RUN_DIR = Path("logs") / "broadcast_connect"

# Windows 进程创建标志：新进程组 + detach（后台独立，不随父 CLI 退出而死）
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008


def build_cmd(bot: str, cfg: dict, defaults: dict) -> list[str]:
    """组装 dws dev connect 启动命令（claudecode vs custom 两类）。

    cfg:      CONNECT_BOTS[bot]，含 unified_env / channel（+ custom 类的 agent_cmd）。
    defaults: CONNECT_DEFAULTS（allowed_users_env / workdir_env / agent_memory / approval_mode）。

    安全：approval_mode 永远取 defaults["approval_mode"]（="ask"），不接受 cfg 覆盖（C2）。
    身份闸：allowed_users 缺失 → RuntimeError（省略 = 任何钉钉用户都能驱动本机 Claude Code）。
    """
    unified = os.getenv(cfg["unified_env"], "")
    if not unified:
        raise RuntimeError(f"缺环境变量 {cfg['unified_env']}（bot={bot} 的 unified-app-id）")
    allowed = os.getenv(defaults["allowed_users_env"], "")
    if not allowed:
        raise RuntimeError(f"缺身份闸 {defaults['allowed_users_env']}（省略=全放行，高危）")

    cmd: list[str] = [
        "dws", "dev", "connect",
        "--unified-app-id", unified,
        "--channel", cfg["channel"],
    ]
    if cfg["channel"] == "claudecode":
        # claudecode 类：dev connect 自动拉起 Claude Code，走全套 agent 参数
        if defaults.get("agent_memory"):
            cmd.append("--agent-memory")
        # 审批闸：写死 ask（C2），不暴露覆盖口子
        cmd += ["--agent-approval-mode", defaults["approval_mode"]]
        cmd += ["--allowed-users", allowed]
        workdir = os.getenv(defaults["workdir_env"], "")
        if workdir:
            cmd += ["--agent-workdir", workdir]
    elif cfg["channel"] == "custom":
        # custom 类：agent-cmd 喂业务脚本（review 桥）；相对路径靠 Popen cwd 锁根（C4）
        cmd += ["--agent-cmd", cfg["agent_cmd"]]
        cmd += ["--allowed-users", allowed]
    else:
        raise ValueError(f"未知 channel={cfg['channel']}（bot={bot}）")
    return cmd
```

- [ ] **Step 4: 跑测试，确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/broadcast/test_connect_manager.py::test_build_cmd_claudecode -v`
Expected: PASS。

- [ ] **Step 5: 写 `build_cmd` custom 类 + 缺凭证测试**

追加到 `tests/broadcast/test_connect_manager.py`：

```python
def test_build_cmd_custom_review(monkeypatch):
    """custom 类（review）：channel=custom + agent-cmd 相对路径（C4），无 workdir/memory。"""
    monkeypatch.setenv("REVIEW_BOT_UNIFIED_APP_ID", "e2695383-6fe9-4617-9439-2a8538af3107")
    monkeypatch.setenv("DINGTALK_ALLOWED_STAFF_IDS", "staff001")
    cmd = cm.build_cmd("review", CUSTOM_CFG, DEFAULTS)
    assert "--channel" in cmd and "custom" in cmd
    assert "--agent-cmd" in cmd
    assert cmd[cmd.index("--agent-cmd") + 1] == CUSTOM_CFG["agent_cmd"]  # 相对路径原样
    assert "--agent-memory" not in cmd      # custom 类不带
    assert "--agent-workdir" not in cmd     # custom 类不带
    assert "--agent-approval-mode" not in cmd  # custom 类不带（agent-cmd 自管审批）


def test_build_cmd_missing_unified_raises(monkeypatch):
    """缺 unified-app-id → RuntimeError（防静默启动一个无身份的 connect）。"""
    monkeypatch.delenv("CLI_BOT_UNIFIED_APP_ID", raising=False)
    try:
        cm.build_cmd("cli", CLS_CFG, DEFAULTS)
        assert False, "应抛 RuntimeError"
    except RuntimeError:
        pass


def test_build_cmd_missing_allowed_users_raises(monkeypatch):
    """缺身份闸 → RuntimeError（C2 延伸：身份闸不可省）。"""
    monkeypatch.setenv("CLI_BOT_UNIFIED_APP_ID", "x")
    monkeypatch.delenv("DINGTALK_ALLOWED_STAFF_IDS", raising=False)
    try:
        cm.build_cmd("cli", CLS_CFG, DEFAULTS)
        assert False, "应抛 RuntimeError"
    except RuntimeError:
        pass
```

- [ ] **Step 6: 跑测试，确认通过（build_cmd 已覆盖两类 + 两道闸）**

Run: `.venv310/Scripts/python.exe -m pytest tests/broadcast/test_connect_manager.py -v`
Expected: 4 PASS。

- [ ] **Step 7: 写 PID 读写 + start + 防重复测试**

追加（用 monkeypatch 替换 `_pid_file` 指向 tmp_path，mock `subprocess.Popen` 与 `_is_alive`）：

```python
def test_start_writes_pid_and_detaches(monkeypatch, tmp_path):
    """start：Popen 用 DETACHED 标志 + cwd=PROJECT_ROOT，落 PID 文件。"""
    monkeypatch.setenv("CLI_BOT_UNIFIED_APP_ID", "u-cli")
    monkeypatch.setenv("DINGTALK_ALLOWED_STAFF_IDS", "s1")
    monkeypatch.setenv("BROADCAST_AGENT_WORKDIR", "F:/quanter")
    monkeypatch.setattr(cm, "_pid_file", lambda bot: tmp_path / f"{bot}.pid")
    monkeypatch.setattr(cm, "_log_file", lambda bot: tmp_path / f"{bot}.log")

    captured = {}
    class FakeProc:
        pid = 4242
    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["creationflags"] = kwargs.get("creationflags", 0)
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc()
    monkeypatch.setattr(cm.subprocess, "Popen", fake_popen)

    res = cm.start("cli", CLS_CFG, DEFAULTS)
    assert res == "started"
    assert (tmp_path / "cli.pid").read_text() == "4242"
    # C4：cwd 锁项目根
    assert captured["cwd"] == cm.PROJECT_ROOT
    # 后台 detach：必须同时含两个标志
    assert captured["creationflags"] == cm.CREATE_NEW_PROCESS_GROUP | cm.DETACHED_PROCESS


def test_start_skips_when_already_running(monkeypatch, tmp_path):
    """防重复：PID 文件在 + 进程存活 → 跳过，不再 Popen。"""
    monkeypatch.setenv("CLI_BOT_UNIFIED_APP_ID", "u-cli")
    monkeypatch.setenv("DINGTALK_ALLOWED_STAFF_IDS", "s1")
    monkeypatch.setattr(cm, "_pid_file", lambda bot: tmp_path / f"{bot}.pid")
    (tmp_path / "cli.pid").write_text("9999", encoding="utf-8")
    monkeypatch.setattr(cm, "_is_alive", lambda pid: True)  # 存活

    popped = []
    monkeypatch.setattr(cm.subprocess, "Popen", lambda *a, **k: popped.append(1) or type("P", (), {"pid": 1})())

    assert cm.start("cli", CLS_CFG, DEFAULTS) == "already_running"
    assert popped == []  # 没有重复拉起


def test_start_clears_dead_pid_then_starts(monkeypatch, tmp_path):
    """PID 文件在但进程已死 → 清死文件后允许新拉起（防崩溃留死文件卡死）。"""
    monkeypatch.setenv("CLI_BOT_UNIFIED_APP_ID", "u-cli")
    monkeypatch.setenv("DINGTALK_ALLOWED_STAFF_IDS", "s1")
    monkeypatch.setattr(cm, "_pid_file", lambda bot: tmp_path / f"{bot}.pid")
    monkeypatch.setattr(cm, "_log_file", lambda bot: tmp_path / f"{bot}.log")
    (tmp_path / "cli.pid").write_text("8888", encoding="utf-8")
    monkeypatch.setattr(cm, "_is_alive", lambda pid: False)  # 死了

    monkeypatch.setattr(cm.subprocess, "Popen", lambda *a, **k: type("P", (), {"pid": 7777})())
    assert cm.start("cli", CLS_CFG, DEFAULTS) == "started"
    assert (tmp_path / "cli.pid").read_text() == "7777"  # 死 PID 被新 PID 覆盖
```

- [ ] **Step 8: 实现 PID 读写 + `_is_alive` + `start`**

追加到 `broadcast/connect_manager.py`：

```python
# ------------------------------------------------------------------ PID 文件

def _pid_file(bot: str) -> Path:
    return RUN_DIR / f"{bot}.pid"


def _log_file(bot: str) -> Path:
    return RUN_DIR / f"{bot}.log"


def _read_pid(bot: str) -> int | None:
    """读 PID；文件不存在/损坏 → None。"""
    try:
        return int(_pid_file(bot).read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None


def _write_pid(bot: str, pid: int) -> None:
    f = _pid_file(bot)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(str(pid), encoding="utf-8")


def _clear_pid(bot: str) -> None:
    try:
        _pid_file(bot).unlink()
    except FileNotFoundError:
        pass


def _is_alive(pid: int) -> bool:
    """Windows 存活探测：tasklist /FI "PID eq <pid>"。返 True=在跑。

    tasklist 命中 → stdout 含 PID 数字；未命中 → "INFO: No tasks are running ..."。
    tasklist 不在 PATH / 超时 → 视为不存活（保守，让 start 重拉）。
    """
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and str(pid) in (r.stdout or "")


# ------------------------------------------------------------------ 生命周期

def start(bot: str, cfg: dict, defaults: dict) -> str:
    """后台拉起 dev connect（幂等：已跑则跳过）。返 'started' | 'already_running'。

    cwd 锁根（C4）：Popen 传 cwd=PROJECT_ROOT，dev connect 继承 → review 相对 agent_cmd 可用。
    后台 detach：creationflags=CREATE_NEW_PROCESS_GROUP|DETACHED_PROCESS，不随父 CLI 退出而死。
    """
    pid = _read_pid(bot)
    if pid is not None and _is_alive(pid):
        return "already_running"
    if pid is not None:
        # PID 文件在但进程已死 → 清死文件（防崩溃留死文件卡死后续 start）
        _clear_pid(bot)

    cmd = build_cmd(bot, cfg, defaults)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    # 日志追加写：dev connect 原始 stdout/stderr，便于 --logs tail 排查
    log_fh = _log_file(bot).open("a", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        cwd=PROJECT_ROOT,                                   # C4：锁项目根
        creationflags=CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS,
        close_fds=True,
    )
    _write_pid(bot, proc.pid)
    logger.info("connect bot=%s 已拉起 pid=%s", bot, proc.pid)
    return "started"
```

- [ ] **Step 9: 跑测试，确认 start 三场景通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/broadcast/test_connect_manager.py -v`
Expected: 7 PASS。

- [ ] **Step 10: 写 stop（树杀 /T）+ status（僵尸清理）测试**

追加：

```python
def test_stop_uses_tree_kill(monkeypatch, tmp_path):
    """stop：taskkill /F /T /PID —— /T 必须在场（C3 防孤儿 Claude Code）。"""
    monkeypatch.setattr(cm, "_pid_file", lambda bot: tmp_path / f"{bot}.pid")
    (tmp_path / "cli.pid").write_text("4242", encoding="utf-8")
    captured = {}
    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return type("R", (), {"returncode": 0})()
    monkeypatch.setattr(cm.subprocess, "run", fake_run)

    assert cm.stop("cli") == "stopped"
    assert captured["cmd"][0] == "taskkill"
    assert "/F" in captured["cmd"]      # 强制
    assert "/T" in captured["cmd"]      # C3：树杀（连 Claude Code 子进程）
    assert "/PID" in captured["cmd"] and "4242" in captured["cmd"]
    assert not (tmp_path / "cli.pid").exists()  # 停后清 PID 文件


def test_stop_no_pid_returns_not_running(monkeypatch, tmp_path):
    """无 PID 文件 → not_running（幂等，不报错）。"""
    monkeypatch.setattr(cm, "_pid_file", lambda bot: tmp_path / f"{bot}.pid")
    assert cm.stop("cli") == "not_running"


def test_status_running(monkeypatch, tmp_path):
    monkeypatch.setattr(cm, "_pid_file", lambda bot: tmp_path / f"{bot}.pid")
    (tmp_path / "cli.pid").write_text("4242", encoding="utf-8")
    monkeypatch.setattr(cm, "_is_alive", lambda pid: True)
    assert cm.status("cli") == "running"


def test_status_dead_clears_pid(monkeypatch, tmp_path):
    """僵尸清理：PID 文件在但进程已死 → 返 'dead' 且删 PID 文件。"""
    monkeypatch.setattr(cm, "_pid_file", lambda bot: tmp_path / f"{bot}.pid")
    (tmp_path / "cli.pid").write_text("4242", encoding="utf-8")
    monkeypatch.setattr(cm, "_is_alive", lambda pid: False)
    assert cm.status("cli") == "dead"
    assert not (tmp_path / "cli.pid").exists()


def test_status_not_running(monkeypatch, tmp_path):
    monkeypatch.setattr(cm, "_pid_file", lambda bot: tmp_path / f"{bot}.pid")
    assert cm.status("cli") == "not_running"
```

- [ ] **Step 11: 实现 `stop` + `status`**

追加到 `broadcast/connect_manager.py`：

```python
def stop(bot: str) -> str:
    """树杀：taskkill /F /T /PID（C3：/T 连 dev connect 拉起的 Claude Code 子进程一并终止）。

    返 'stopped' | 'not_running'。taskkill 失败/超时仍清 PID 文件（避免死文件卡死后续 start）。
    """
    pid = _read_pid(bot)
    if pid is None:
        return "not_running"
    try:
        # /F 强制 /T 树杀：漏 /T = dev connect 死了但 Claude Code 子进程还活着（孤儿吃资源）
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        logger.warning("taskkill bot=%s pid=%s 异常，仍清 PID 文件", bot, pid, exc_info=True)
    _clear_pid(bot)
    logger.info("connect bot=%s 已停止 pid=%s", bot, pid)
    return "stopped"


def status(bot: str) -> str:
    """单个 bot 状态：'running' | 'dead' | 'not_running'。dead 则清 PID 文件（僵尸清理）。"""
    pid = _read_pid(bot)
    if pid is None:
        return "not_running"
    if _is_alive(pid):
        return "running"
    _clear_pid(bot)   # 僵尸清理：进程已死 → 删死 PID 文件
    return "dead"
```

- [ ] **Step 12: 跑全量 connect_manager 测试**

Run: `.venv310/Scripts/python.exe -m pytest tests/broadcast/test_connect_manager.py -v`
Expected: 12 PASS（build_cmd 4 + start 3 + stop 2 + status 3）。

- [ ] **Step 13: 提交**

```bash
git add broadcast/connect_manager.py tests/broadcast/test_connect_manager.py
git commit -m "feat(broadcast): 新增 connect_manager —— dev connect 后台进程管理（Popen/PID/树杀/僵尸清理）

- build_cmd 组装 claudecode vs custom 两类命令；approval_mode='ask' 写死不接受覆盖（C2）
- start：DETACHED 后台 + cwd=PROJECT_ROOT 锁根（C4 化解 dws cwd 非项目根踩坑）+ 防重复
- stop：taskkill /F /T 树杀（C3 防孤儿 Claude Code）
- status：running/dead/not_running，dead 自动清 PID 文件（僵尸清理）
- 全程 mock subprocess 单测，12 例全绿

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3: __main__.py 配置重构 + market 下线（push 侧）+ 测试同步

**Files:**
- Modify: `broadcast/__main__.py:27,32,42-57,60,73,76-90,149-156,404-438,445-489`（多处）
- Modify: `tests/test_broadcast_main.py`（整文件重构 market→trading）
- Modify: `tests/broadcast/test_cli_routing.py:11-16`（删 market 断言）

**Interfaces:**
- Consumes: Task 1 的 `brief.py`（已无 `build_daily_brief`，import 必须同步删）。
- Produces:
  - `PUSH_BOTS: dict`（trading/data/strategy，键 `robot_env`/`last`/`title`）。
  - `CONNECT_BOTS: dict`（cli/trading_q/data_q/strategy_q/review，键 `unified_env`/`channel`[+`agent_cmd`]）。
  - `CONNECT_DEFAULTS: dict`（`allowed_users_env`/`workdir_env`/`agent_memory`/`approval_mode`）。
  - `SUPPORTED_BOTS: tuple`（= `("trading","data","strategy")`，去 market）。
  - `last_brief_file(bot)->Path`（简化：去 market 特例，直接 `Path("logs")/PUSH_BOTS[bot]["last"]`）。

> **本任务边界**：只做 push 侧 + 配置结构 + market 删除。CLI 子命令路由（push/connect）放 Task 4，避免本任务过大。本任务完成后 `python -m broadcast --bot trading` 仍工作（无子命令兼容，C1 红线），market 不再可选。

- [ ] **Step 1: 重写 `tests/broadcast/test_cli_routing.py`（去 market，先让现状测试失败）**

整文件替换：

```python
# -*- coding: utf-8 -*-
"""broadcast CLI 路由 + 幂等单测。

Task 3：market 下线后，last_brief_file 仅服务 push 类（trading/data/strategy），
未知 bot 抛 ValueError（防 CLI 笔误静默落到默认 bot）。
Task 4 将在此补 push/connect 子命令路由断言。
"""
from broadcast import __main__ as bc


def test_last_brief_path_per_push_bot():
    """每个 push 机器人独立幂等文件，互不干扰（防跨机器人误判重复）。"""
    assert bc.last_brief_file("trading").name == ".last_trading_brief"
    assert bc.last_brief_file("data").name == ".last_data_brief"
    assert bc.last_brief_file("strategy").name == ".last_strategy_brief"


def test_last_brief_file_unknown_bot():
    """未知 bot 抛 ValueError（防误用）。"""
    try:
        bc.last_brief_file("market")  # market 已下线 → 视为未知
        assert False, "应抛 ValueError"
    except ValueError:
        pass
    try:
        bc.last_brief_file("unknown")
        assert False, "应抛 ValueError"
    except ValueError:
        pass


def test_supported_bots_no_market():
    """SUPPORTED_BOTS 不含 market（下线红线）。"""
    assert "market" not in bc.SUPPORTED_BOTS
    assert set(bc.SUPPORTED_BOTS) == {"trading", "data", "strategy"}
```

- [ ] **Step 2: 跑测试，确认失败（market 仍在）**

Run: `.venv310/Scripts/python.exe -m pytest tests/broadcast/test_cli_routing.py -v`
Expected: FAIL（`last_brief_file("market")` 仍返路径不抛、`SUPPORTED_BOTS` 仍含 market）。

- [ ] **Step 3: 改 `broadcast/__main__.py` 配置区（`_BOT_CFG`→`PUSH_BOTS`+`CONNECT_BOTS`+`CONNECT_DEFAULTS`，删 market）**

替换 `broadcast/__main__.py:27` 的 import 与 `:37-95` 的配置区。

**3a. 删 market 专用 import（行 27）**：删 `from broadcast.brief import build_daily_brief`（Task 1 已删该函数）。保留其余 import。`from data.lake_reader import DataLakeReader`（行 33）**保留** —— `_load_reader`/`_latest_trade_date` 仍需 reader 确定 index_daily 最新交易日作为默认播报日。

**3b. 替换配置区（行 37-95，即 `SUPPORTED_BOTS`/`_BOT_CFG`/`_GROUP_ID_ENV`/`LAST_BC_FILE`/`last_brief_file` 整段）**：

```python
# ===========================================================================
# 机器人总管配置（push 播报类 + connect 对话类）
# ===========================================================================
# 播报类（push）：一次性 → schtasks 到点 / 手工 --force 触发
#   robot_env：对应 .env 中该机器人 dws 应用 robot_code（不同机器人=不同 dws 应用=不同群）
#   last      ：幂等去重文件名；分文件防跨机器人误判已播
#   title     ：钉钉消息标题前缀，便于一眼区分来源
PUSH_BOTS = {
    "trading":  {"robot_env": "TRADING_BOT_ROBOT_CODE",  "last": ".last_trading_brief",
                 "title": "💰 每日交易播报"},
    "data":     {"robot_env": "DATA_BOT_ROBOT_CODE",     "last": ".last_data_brief",
                 "title": "🗄 每日数据播报"},
    "strategy": {"robot_env": "STRATEGY_BOT_ROBOT_CODE", "last": ".last_strategy_brief",
                 "title": "♟ 每日策略播报"},
}
# market 已下线（2026-07-26）：代码/配置/文档全清，钉钉侧资源由用户移除。
SUPPORTED_BOTS = tuple(PUSH_BOTS.keys())  # ("trading","data","strategy")

# 对话类（connect）：dws dev connect 后台常驻，broadcast connect --start 拉起
#   unified_env：.env 中该机器人 unified-app-id（dev connect 建联用）
#   channel    ：claudecode=对话（dev connect 自动拉 Claude Code）/ custom=业务脚本
#   agent_cmd  ：仅 channel=custom 的 review 有；相对路径靠 connect_manager.Popen cwd 锁根
CONNECT_BOTS = {
    "cli":         {"unified_env": "CLI_BOT_UNIFIED_APP_ID",      "channel": "claudecode"},  # yzzhanCli通用
    "trading_q":   {"unified_env": "TRADING_BOT_UNIFIED_APP_ID",  "channel": "claudecode"},  # quanter交易
    "data_q":      {"unified_env": "DATA_BOT_UNIFIED_APP_ID",     "channel": "claudecode"},  # quanter数据
    "strategy_q":  {"unified_env": "STRATEGY_BOT_UNIFIED_APP_ID", "channel": "claudecode"},  # quanter策略
    "review":      {"unified_env": "REVIEW_BOT_UNIFIED_APP_ID",   "channel": "custom",
                    "agent_cmd": ".venv310/Scripts/python.exe infra/tools/dingtalk_review_bridge.py"},  # yzzhan参数优化
}
# claudecode 类共用的 dev connect 启动参数（DRY，不每 bot 重复）
CONNECT_DEFAULTS = {
    "allowed_users_env": "DINGTALK_ALLOWED_STAFF_IDS",  # 身份闸（.env 已配）
    "workdir_env":       "BROADCAST_AGENT_WORKDIR",     # Claude Code 工作目录（新增=F:/quanter）
    "agent_memory":      True,
    "approval_mode":     "ask",  # 审批闸，写死，绝不为任何 bot 留覆盖口子（C2 安全底线）
}

# 钉钉群组（所有 push 机器人共用一个运营群；机器人身份靠 robot_code 区分）
_GROUP_ID_ENV = "BROADCAST_GROUP_ID"


def last_brief_file(bot: str) -> Path:
    """返回某 push 机器人的幂等去重文件路径（logs/.last_<bot>_brief）。

    Why 工厂式：每机器人独立幂等文件，防跨机器人误判已播。
    未知 bot 抛 ValueError（CLI argparse choices 已挡一道，这里是第二道防线）。

    market 下线后特例消除：所有 push bot 统一走 Path("logs")/_BOT_CFG[bot]["last"]。
    """
    if bot not in PUSH_BOTS:
        raise ValueError(f"未知 bot={bot}，支持：{SUPPORTED_BOTS}")
    return Path("logs") / PUSH_BOTS[bot]["last"]


# 播报只用 index_daily 这 1 个湖（market 下线后 ths_daily/moneyflow/dragon_list 无人用）：
# 仅 _latest_trade_date 需读 index_daily 取最新交易日作默认播报日。trading/data/strategy
# brief 各自走 trading_service/data_service/plans+json，不依赖这三个湖，load 它们纯浪费内存。
_BRIEF_LAKES = ("index_daily",)
```

**3c. 删 `LAST_BC_FILE` 常量 + `_read_last_broadcast`/`_write_last_broadcast` 兼容函数（行 62-73、149-156）**：market 下线，这些 market 专属兼容代码全删。`_read_last`/`_write_last`（行 132-146）**保留**（push 主流程用）。

- [ ] **Step 4: 删 `_build_brief` 的 market 分支（行 411-413）**

替换 `_build_brief`（行 404-438）中 market 分支。新 `_build_brief` 首部去掉 `if bot == "market": return build_daily_brief(...)`，保留 trading/data/strategy 三分支 + 兜底 ValueError。docstring 去掉 market 字样。

```python
def _build_brief(bot: str, date: str, reader: DataLakeReader):
    """按 push 机器人路由到对应 brief 构造器（注入式取数 + 纯函数渲染）。

    market 已下线；trading/data/strategy 各自取数失败均降级，不阻断播报。
    本函数集中路由，避免 main() 里散落 if/elif。
    """
    if bot == "trading":
        trades, asset, positions, status = _fetch_trading_snapshot(date)
        return build_trading_brief(
            date, trades=trades, asset=asset, positions=positions, status=status,
        )
    if bot == "data":
        datasets = _fetch_data_snapshot()
        freshness = _fetch_data_freshness()
        return build_data_brief(date, datasets=datasets, freshness=freshness)
    if bot == "strategy":
        scan_count, param_iter_state, recent_runs = _fetch_strategy_snapshot(date)
        return build_strategy_brief(
            date, scan_count=scan_count, param_iter_state=param_iter_state,
            recent_runs=recent_runs,
        )
    raise ValueError(f"未知 bot={bot}，支持：{SUPPORTED_BOTS}")
```

- [ ] **Step 5: 改 `main()` 的 `--bot` 默认 `market`→`trading`（行 454）**

```python
    p.add_argument("--bot", default="trading", choices=SUPPORTED_BOTS,
                   help="push 机器人身份（默认 trading）")
```

同时把 `main()` docstring（行 446-449）里 `--bot {market|trading|data|strategy}（默认 market）` 改为 `--bot {trading|data|strategy}（默认 trading）`，去掉「market 分支行为与一期前完全一致」句。

> **注意**：本步**不动** main 的子命令路由（仍是单层 argparse），Task 4 再加 push/connect 路由。此时 `python -m broadcast --bot trading` 行为不变（C1 红线维持）。

- [ ] **Step 6: 跑 test_cli_routing.py，确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/broadcast/test_cli_routing.py -v`
Expected: 3 PASS。

- [ ] **Step 7: 重写 `tests/test_broadcast_main.py`（market→trading 主流程）**

整文件替换：

```python
# -*- coding: utf-8 -*-
"""__main__ CLI 单测（trading 主流程）：dry-run / 幂等去重 / --force / 推送失败 / 无日期兜底。

market 下线后，默认 bot=trading。trading 分支取数走 _fetch_trading_snapshot（重 import），
测试 mock 它 + build_trading_brief，避免依赖真实 trading_service。
幂等文件隔离：monkeypatch last_brief_file 返回 tmp_path，不碰真实 logs/。
"""
import broadcast.__main__ as bm
from broadcast.brief import BriefResult


def _stub_trading(monkeypatch, date="2026-07-15"):
    """桩：reader/date/取数/brief 渲染，让 main 主流程不依赖真实 IO。"""
    monkeypatch.setattr(bm, "_load_reader", lambda: "fake_reader")
    monkeypatch.setattr(bm, "_latest_trade_date", lambda r: date)
    # mock trading 取数四件套（避免 import trading_service 重链路 + 真实网关）
    monkeypatch.setattr(bm, "_fetch_trading_snapshot",
                        lambda d: ([], None, None, {"mode": "live"}))
    # mock brief 渲染（主流程测幂等/push/last，不应依赖 brief 真渲染）
    monkeypatch.setattr(
        bm, "build_trading_brief",
        lambda *a, **k: BriefResult(date=date, markdown="### 每日交易播报\n样例正文"),
    )


def _isolate_last_file(monkeypatch, tmp_path, date="2026-07-15"):
    """把 trading 幂等文件重定向到 tmp_path（不碰真实 logs/.last_trading_brief）。"""
    f = tmp_path / ".last_trading_brief"
    monkeypatch.setattr(bm, "last_brief_file", lambda bot: f)
    return f


def test_main_dry_run_prints_and_pushes_dry(monkeypatch):
    _stub_trading(monkeypatch)
    pushed = []
    monkeypatch.setattr(bm, "push_brief", lambda *a, **k: pushed.append((a, k)) or True)
    rc = bm.main(["--dry-run"])
    assert rc == 0
    assert pushed and pushed[0][1].get("dry_run") is True
    assert "每日交易播报" in pushed[0][0][1]


def test_main_dedup_skips_when_already_broadcast(monkeypatch, tmp_path):
    _stub_trading(monkeypatch, date="2026-07-15")
    f = _isolate_last_file(monkeypatch, tmp_path)
    f.write_text("2026-07-15", encoding="utf-8")
    pushed = []
    monkeypatch.setattr(bm, "push_brief", lambda *a, **k: pushed.append(1) or True)
    rc = bm.main([])
    assert rc == 0
    assert pushed == []                      # 今日已播 → 跳过


def test_main_force_overrides_dedup(monkeypatch, tmp_path):
    _stub_trading(monkeypatch, date="2026-07-15")
    f = _isolate_last_file(monkeypatch, tmp_path)
    f.write_text("2026-07-15", encoding="utf-8")
    pushed = []
    monkeypatch.setattr(bm, "push_brief", lambda *a, **k: pushed.append(1) or True)
    rc = bm.main(["--force"])
    assert rc == 0
    assert pushed == [1]                     # --force 覆盖去重
    assert f.read_text(encoding="utf-8") == "2026-07-15"


def test_main_success_writes_last(monkeypatch, tmp_path):
    _stub_trading(monkeypatch, date="2026-07-15")
    f = _isolate_last_file(monkeypatch, tmp_path)
    monkeypatch.setattr(bm, "push_brief", lambda *a, **k: True)
    rc = bm.main([])
    assert rc == 0
    assert f.read_text(encoding="utf-8") == "2026-07-15"


def test_main_push_failure_no_last(monkeypatch, tmp_path):
    _stub_trading(monkeypatch, date="2026-07-15")
    f = _isolate_last_file(monkeypatch, tmp_path)
    monkeypatch.setattr(bm, "push_brief", lambda *a, **k: False)
    rc = bm.main([])
    assert rc == 2                           # 推送失败
    assert not f.exists()                    # 失败不写 last（下次重试）


def test_main_no_date_returns_1(monkeypatch):
    _stub_trading(monkeypatch, date=None)
    assert bm.main([]) == 1
```

- [ ] **Step 8: 跑 test_broadcast_main.py，确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_broadcast_main.py -v`
Expected: 6 PASS。

- [ ] **Step 9: 全量回归（broadcast 包 + name_resolver）**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_broadcast_main.py tests/test_broadcast_push.py tests/test_broadcast_name_resolver.py tests/broadcast/ -v`
Expected: 全 PASS（market 相关测试已清，trading 主流程 + 三个 brief_* + connect_manager + cli_routing + push + name_resolver 全绿）。

- [ ] **Step 10: 提交**

```bash
git add broadcast/__main__.py tests/test_broadcast_main.py tests/broadcast/test_cli_routing.py
git commit -m "refactor(broadcast): __main__ 配置重构 PUSH/CONNECT 分类 + market 下线（push 侧）

- _BOT_CFG → PUSH_BOTS（trading/data/strategy）+ CONNECT_BOTS（cli/trading_q/data_q/strategy_q/review）+ CONNECT_DEFAULTS
- 删 market：_BOT_CFG 条目 / SUPPORTED_BOTS / build_daily_brief import / LAST_BC_FILE / last_brief_file 特例 / _read|_write_last_broadcast 兼容函数 / _build_brief market 分支
- --bot 默认 market→trading
- test_broadcast_main.py 重构为 trading 主流程（mock _fetch_trading_snapshot + build_trading_brief）
- test_cli_routing.py 删 market 断言 + 补 SUPPORTED_BOTS 无 market / market 视为未知 bot
- 无子命令=push 兼容不变（C1 schtasks 红线）；CONNECT_BOTS/CONNECT_DEFAULTS 待 Task 4 子命令接入

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 4: __main__.py CLI 子命令化 + connect 接入

**Files:**
- Modify: `broadcast/__main__.py`（`main()` 重构为子命令路由 + 新增 `_main_push`/`_main_connect`/`_connect_start`/`_connect_stop`/`_connect_logs`）
- Modify: `tests/broadcast/test_cli_routing.py`（补 push/connect 子命令路由断言）

**Interfaces:**
- Consumes: Task 2 的 `connect_manager.start/stop/status/_log_file`；Task 3 的 `CONNECT_BOTS`/`CONNECT_DEFAULTS`/`PUSH_BOTS`。
- Produces: `main(argv)` 子命令路由 —— 首参 `connect` → `_main_connect`；首参 `push` 或无子命令 → `_main_push`（C1 兼容）。

- [ ] **Step 1: 写子命令路由失败测试**

追加到 `tests/broadcast/test_cli_routing.py`：

```python
def test_no_subcommand_routes_to_push(monkeypatch):
    """C1 红线：无子命令 = push（schtasks 生成的 'python -m broadcast --bot trading' 零改动）。"""
    routed = {}
    monkeypatch.setattr(bc, "_main_push", lambda a: routed.setdefault("push", a) or 0)
    bc.main(["--bot", "trading", "--dry-run"])
    assert routed.get("push") == ["--bot", "trading", "--dry-run"]


def test_explicit_push_subcommand_routes(monkeypatch):
    routed = {}
    monkeypatch.setattr(bc, "_main_push", lambda a: routed.setdefault("push", a) or 0)
    bc.main(["push", "--bot", "data"])
    assert routed.get("push") == ["--bot", "data"]


def test_connect_subcommand_routes(monkeypatch):
    """connect 首参 → _main_connect，不再走 push。"""
    routed = {}
    monkeypatch.setattr(bc, "_main_connect", lambda a: routed.setdefault("connect", a) or 0)
    bc.main(["connect", "--status"])
    assert routed.get("connect") == ["--status"]


def test_connect_start_all_prompts_confirm(monkeypatch, capsys):
    """--start all 二次确认：输入非 y → 取消，不拉起任何 bot（防误启 5 个 Claude Code）。"""
    monkeypatch.setattr(bc, "_read_confirm", lambda: "n")
    monkeypatch.setattr(bc.connect_manager, "start", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应拉起")))
    rc = bc.main(["connect", "--start", "all"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "取消" in out


def test_connect_status_calls_manager(monkeypatch, capsys):
    """--status 遍历 CONNECT_BOTS 调 connect_manager.status。"""
    reported = {}
    monkeypatch.setattr(bc.connect_manager, "status", lambda bot: reported.setdefault(bot, "running"))
    rc = bc.main(["connect", "--status"])
    assert rc == 0
    assert set(reported.keys()) == set(bc.CONNECT_BOTS)  # 5 个全报
```

- [ ] **Step 2: 跑测试，确认失败（`_main_push`/`_main_connect`/`_read_confirm` 未定义）**

Run: `.venv310/Scripts/python.exe -m pytest tests/broadcast/test_cli_routing.py -v`
Expected: 新增 5 例 FAIL（AttributeError）。

- [ ] **Step 3: 重构 `main()` 为子命令路由 + 拆 `_main_push`**

替换 `broadcast/__main__.py` 现有 `main()`（行 445-489）。把原 main 体（解析 args → 路由）改名为 `_main_push`，新增路由 `main`：

```python
def main(argv: list[str] | None = None) -> int:
    """CLI 总入口（机器人总管）。返回 0=成功/跳过，1=无法定播报日，2=推送失败。

    子命令路由（C1 兼容红线）：
      - 首参为 'connect' → _main_connect（对话机器人后台托管）
      - 首参为 'push'    → _main_push（显式等价默认）
      - 其余（含 --bot/无参）→ _main_push（schtasks 'python -m broadcast --bot trading' 零改动）
    """
    raw = list(argv) if argv is not None else sys.argv[1:]
    if raw and raw[0] == "connect":
        return _main_connect(raw[1:])
    if raw and raw[0] == "push":
        return _main_push(raw[1:])
    return _main_push(raw)


def _main_push(argv: list[str]) -> int:
    """push 子命令（播报）：生成文案 → push_brief → 退出。无子命令默认即此路径。

    返回 0=成功/跳过，1=无法定播报日，2=推送失败。
    """
    p = argparse.ArgumentParser(
        prog="python -m broadcast", description="钉钉播报（push 类 · 一次性）"
    )
    p.add_argument("--bot", default="trading", choices=SUPPORTED_BOTS, help="push 机器人身份")
    p.add_argument("--date", help="播报日 YYYY-MM-DD（缺省=index_daily 最新交易日）")
    p.add_argument("--dry-run", action="store_true", help="只打印文案不发钉钉")
    p.add_argument("--force", action="store_true", help="忽略幂等去重强制重发")
    args = p.parse_args(argv)

    reader = _load_reader()
    date = args.date or _latest_trade_date(reader)
    if date is None:
        logger.error("无法确定播报日（index_daily 未加载/为空）；用 --date 显式指定")
        return 1

    last_file = last_brief_file(args.bot)
    if not args.dry_run and not args.force and _read_last(last_file) == date:
        print(f"{args.bot} 今日({date})已播报，跳过（--force 可重发）")
        return 0

    brief = _build_brief(args.bot, date, reader)
    title = f"{PUSH_BOTS[args.bot]['title']} {date}"
    robot_code = os.getenv(PUSH_BOTS[args.bot]["robot_env"], "")
    group_id = os.getenv(_GROUP_ID_ENV, "")
    ok = push_brief(
        title, brief.markdown,
        robot_code=robot_code, group_id=group_id, dry_run=args.dry_run,
    )

    if args.dry_run:
        return 0
    if ok:
        _write_last(date, last_file)
        print(f"{args.bot} 播报已推送({date})")
        return 0
    logger.error("%s 推送失败，未写 %s（下次触发重试）", args.bot, last_file)
    return 2
```

- [ ] **Step 4: 实现 `_main_connect` + 辅助函数**

追加到 `broadcast/__main__.py`（顶部 import 区补 `from broadcast import connect_manager`）：

```python
def _read_confirm() -> str:
    """读二次确认输入（y/N）。无 tty（schtasks/管道）→ EOFError 兜底返 'n'（保守不启）。"""
    try:
        return input("确认？[y/N] ").strip().lower()
    except EOFError:
        return "n"


def _main_connect(argv: list[str]) -> int:
    """connect 子命令：dev connect 对话机器人后台托管（start/stop/status/logs）。

    生命周期托管给 connect_manager（PID 文件 + 日志 + 树杀 + 僵尸清理）。
    """
    p = argparse.ArgumentParser(
        prog="python -m broadcast connect",
        description="对话机器人后台托管（connect 类 · dev connect 常驻）",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--start", metavar="BOT|all", help="拉起 connect 机器人（bot key 或 all）")
    g.add_argument("--stop", metavar="BOT|all", help="停止（bot key 或 all）")
    g.add_argument("--status", action="store_true", help="全部 connect bot 状态 + 僵尸清理")
    g.add_argument("--logs", metavar="BOT", help="查日志（tail 最后 40 行）")
    args = p.parse_args(argv)

    if args.status:
        for bot in CONNECT_BOTS:
            print(f"{bot}: {connect_manager.status(bot)}")
        return 0
    if args.start:
        return _connect_start(args.start)
    if args.stop:
        return _connect_stop(args.stop)
    if args.logs:
        return _connect_logs(args.logs)
    return 0


def _connect_start(target: str) -> int:
    """拉起 connect 机器人。target='all' 需二次确认（防误启 5 个 Claude Code 实例）。"""
    bots = list(CONNECT_BOTS) if target == "all" else [target]
    for b in bots:
        if b not in CONNECT_BOTS:
            print(f"未知 connect bot={b}，支持：{list(CONNECT_BOTS)}")
            return 1
    if target == "all":
        print(f"即将拉起 {len(bots)} 个 connect 机器人：{bots}（= {len(bots)} 个 Claude Code 常驻实例）")
        if _read_confirm() != "y":
            print("已取消")
            return 0
    for b in bots:
        try:
            res = connect_manager.start(b, CONNECT_BOTS[b], CONNECT_DEFAULTS)
        except RuntimeError as e:
            # 缺 unified-app-id / 身份闸 → 该 bot 跳过，不让单点阻断其余
            print(f"{b}: 配置缺失跳过（{e}）")
            continue
        print(f"{b}: {res}")
    return 0


def _connect_stop(target: str) -> int:
    """停止 connect 机器人（树杀）。target='all' 批量停。"""
    bots = list(CONNECT_BOTS) if target == "all" else [target]
    for b in bots:
        if b not in CONNECT_BOTS:
            print(f"未知 connect bot={b}，支持：{list(CONNECT_BOTS)}")
            return 1
        print(f"{b}: {connect_manager.stop(b)}")
    return 0


def _connect_logs(bot: str) -> int:
    """tail 某 connect bot 日志最后 40 行（dev connect 原始输出）。"""
    if bot not in CONNECT_BOTS:
        print(f"未知 connect bot={bot}，支持：{list(CONNECT_BOTS)}")
        return 1
    log_path = connect_manager._log_file(bot)
    if not log_path.exists():
        print(f"无日志（{bot} 未启动过）：{log_path}")
        return 0
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-40:]:
        print(line)
    return 0
```

> **import 调整**：在 `broadcast/__main__.py` 顶部 import 段（行 27-33 附近）新增 `from broadcast import connect_manager`。注意 `_connect_start`/`_connect_stop`/`_connect_logs` 用到的 `connect_manager` 即此 import；测试里 `monkeypatch.setattr(bc.connect_manager, ...)` 能命中。

- [ ] **Step 5: 跑 test_cli_routing.py 全量，确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/broadcast/test_cli_routing.py -v`
Expected: 8 PASS（原 3 + 新 5）。

- [ ] **Step 6: 冒烟验证 C1 红线（无子命令 = push，真实 CLI 不依赖钉钉）**

Run: `.venv310/Scripts/python.exe -m broadcast --bot trading --dry-run --date 2026-07-15 2>&1 | head -5`
Expected: 打印 trading brief 文案（dry-run 不发钉钉），无 argparse 报错、无 ModuleNotFoundError —— 证明 schtasks 路径未断。

Run: `.venv310/Scripts/python.exe -m broadcast connect --help`
Expected: 打印 connect 子命令帮助（--start/--stop/--status/--logs），无报错。

- [ ] **Step 7: 全量回归**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_broadcast_main.py tests/test_broadcast_push.py tests/test_broadcast_name_resolver.py tests/broadcast/ -v`
Expected: 全 PASS。

- [ ] **Step 8: 提交**

```bash
git add broadcast/__main__.py tests/broadcast/test_cli_routing.py
git commit -m "feat(broadcast): CLI 子命令化（push/connect）+ connect 接入 connect_manager

- main 路由：首参 connect→_main_connect / push→_main_push / 其余默认 _main_push（C1 兼容红线）
- _main_connect：--start/--stop/--status/--logs，--start all 二次确认防误启 5 个 Claude Code
- _connect_start：缺 unified-app-id/身份闸 → RuntimeError 跳过该 bot 不阻断其余
- test_cli_routing 补 5 例子命令路由断言（无子命令=push / connect 路由 / all 确认 / status 遍历）
- schtasks 'python -m broadcast --bot trading' 零改动（C1 冒烟验证）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 5: .env / .env.example 配置收编

**Files:**
- Modify: `.env.example`（新增 3 项 + 改注释；C8：本无 `DINGTALK_CHAT_ROBOT_CODE`，无需删）
- Modify: `.env`（实文件，不入库；用户侧填真值 + 删 market 凭证）

**Interfaces:**
- Consumes: Task 3 的 `CONNECT_BOTS`/`CONNECT_DEFAULTS` 键名。
- Produces: `.env.example` 新增 `CLI_BOT_UNIFIED_APP_ID` / `REVIEW_BOT_UNIFIED_APP_ID` / `BROADCAST_AGENT_WORKDIR`，注释从「3 播报 + 3 查询」改为「3 播报 + 5 对话」。

> **真实 unified-app-id 来源**（填 `.env` 实文件用，不入库不进 .env.example）：
> - `cli`（yzzhanCli通用）= `f0b2740f-c029-4b99-943c-58de139c7463`（`scripts/start_dingtalk_bots.md:19`）
> - `review`（yzzhan参数优化）= `e2695383-6fe9-4617-9439-2a8538af3107`（`scripts/start_dingtalk_bots.md:31`）

- [ ] **Step 1: 改 `.env.example` 的「一期观测运营层」段（行 52-70）**

把段标题注释与内容改为：

```dotenv
# ============ 一期观测运营层（3 播报 push + 5 对话 connect） ============
# broadcast 升格为机器人总管：push 类（trading/data/strategy，schtasks 到点播报）+
# connect 类（cli/trading_q/data_q/strategy_q/review，dws dev connect 后台常驻）。
# 三个播报机器人的 dws 应用 robotCode（dws dev app robot submit 产出，见
# scripts/start_dingtalk_bots.md「观测层上线 SOP」Step 1；缺则 push_brief 跳过推送）。
TRADING_BOT_ROBOT_CODE=
DATA_BOT_ROBOT_CODE=
STRATEGY_BOT_ROBOT_CODE=
# 五个 dev connect 对话机器人所需 unifiedAppId（建号后 dws dev app list / 开放平台控制台「统一应用ID」）。
# cli/review 从 start_dingtalk_bots.md 收编（原仅文档散落，2026-07-26 入 .env 单一真相源）；
# trading_q/data_q/strategy_q 即 quanter交易/数据/策略 专业 @查询。
CLI_BOT_UNIFIED_APP_ID=
TRADING_BOT_UNIFIED_APP_ID=
DATA_BOT_UNIFIED_APP_ID=
STRATEGY_BOT_UNIFIED_APP_ID=
REVIEW_BOT_UNIFIED_APP_ID=
# connect 类 claudecode 机器人共用的 Claude Code 工作目录（dev connect --agent-workdir；
# 锁项目根 F:/quanter，省略则跑空白 Temp 不了解项目，已踩坑）。
BROADCAST_AGENT_WORKDIR=F:/quanter
# 钉钉运营群 openConversationId（push 机器人共用：复用 yzzhan量化群，不新建群）。
BROADCAST_GROUP_ID=
# 三个播报机器人每日触发时间（HH:MM，schtasks /ST；改时间改 .env + --register 幂等重建）
TRADING_BRIEF_TIME=15:30
STRATEGY_BRIEF_TIME=16:00
DATA_BRIEF_TIME=17:00
```

> **market 凭证**：`.env.example` 本无 `DINGTALK_CHAT_ROBOT_CODE`（C8），无需删。`.env` 实文件里的该行在 Step 2 由用户删。

- [ ] **Step 2: 提示用户改 `.env` 实文件（外向/本地真值，AI 不直接改不入库文件）**

向用户输出操作清单（不替按）：

```dotenv
# 在 .env 实文件中：
# 1. 新增（填真值，从 start_dingtalk_bots.md / dws dev app list 取）：
CLI_BOT_UNIFIED_APP_ID=f0b2740f-c029-4b99-943c-58de139c7463
REVIEW_BOT_UNIFIED_APP_ID=e2695383-6fe9-4617-9439-2a8538af3107
BROADCAST_AGENT_WORKDIR=F:/quanter
# 2. 删除（market 下线）：
# DINGTALK_CHAT_ROBOT_CODE=dingdya5o94mnmde7jlv   ← 删此行
```

- [ ] **Step 3: 提交（仅 .env.example 入库）**

```bash
git add .env.example
git commit -m "chore(env): 收编 5 个 dev connect 配置入 .env 单一真相源

- .env.example 新增 CLI_BOT_UNIFIED_APP_ID / REVIEW_BOT_UNIFIED_APP_ID / BROADCAST_AGENT_WORKDIR
- 段注释「3 播报 + 3 查询」→「3 播报 push + 5 对话 connect」（broadcast 升格机器人总管）
- .env 实文件由用户填真值 + 删 DINGTALK_CHAT_ROBOT_CODE（market 凭证，不入库）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 6: 文档层 —— start_dingtalk_bots.md 收编 + market 文档归档/废弃标注

**Files:**
- Modify: `scripts/start_dingtalk_bots.md`（5 个 dev connect 段改为引用 `broadcast connect`；删 market 段）
- Move: `scripts/setup_broadcast_bot.md` → `scripts/archive/setup_broadcast_bot.md`
- Move: `scripts/setup_broadcast_schtasks.md` → `scripts/archive/setup_broadcast_schtasks.md`
- Modify: `docs/superpowers/specs/2026-07-16-daily-market-brief-design.md`（顶部加废弃标注）
- Modify: `docs/superpowers/plans/2026-07-16-daily-market-brief.md`（顶部加废弃标注）

**Interfaces:**
- Consumes: Task 4 的 `broadcast connect --start/--stop/--status/--logs` CLI。

- [ ] **Step 1: 归档两个 market 专属文档**

Run:
```bash
mkdir -p scripts/archive
git mv scripts/setup_broadcast_bot.md scripts/archive/setup_broadcast_bot.md
git mv scripts/setup_broadcast_schtasks.md scripts/archive/setup_broadcast_schtasks.md
```

> **归档不删**：保留历史溯源（建号/schtasks 命令可查）。在两个文件顶部各加一行：`> ⚠️ 已归档（market 机器人下线，2026-07-26）。保留作历史参考，配置真相源已移至 .env + broadcast/__main__.py:CONNECT_BOTS。`

- [ ] **Step 2: 改 `scripts/start_dingtalk_bots.md` —— 5 个 dev connect 段改为引用 broadcast connect**

**2a. 「一、1」yzzhanCli通用（行 17-27）**：启动命令改为：

```bash
# 拉起（后台常驻 + PID 文件 + 日志，详见 broadcast/connect_manager.py）
python -m broadcast connect --start cli
```

保留身份闸/审批闸/workdir 的解释段（这些参数现在由 `CONNECT_BOTS`/`CONNECT_DEFAULTS` 固化，文档改为「参数由 broadcast 配置固化，详见 `broadcast/__main__.py`」）。真实 unified-app-id（`f0b2740f-...`）已入 `.env:CLI_BOT_UNIFIED_APP_ID`，文档不再重复硬编码。

**2b. 「一、2」yzzhan参数优化（行 29-37）**：改为：

```bash
python -m broadcast connect --start review
```

注明 `agent_cmd` 由 `CONNECT_BOTS.review` 固化为相对路径 `.venv310/Scripts/python.exe infra/tools/dingtalk_review_bridge.py`（靠 connect_manager Popen cwd 锁项目根，化解旧绝对路径踩坑）。

**2c. 「二、Step 6」3 个 _q 机器人（行 124-148）**：三条 `dws dev connect` 命令改为：

```bash
python -m broadcast connect --start trading_q
python -m broadcast connect --start data_q
python -m broadcast connect --start strategy_q
```

或一次性：`python -m broadcast connect --start all`（带二次确认）。

**2d. 新增「connect 托管四件套」说明段**（替代散落的手工命令）：

```markdown
## connect 机器人统一托管（broadcast 升格 · 2026-07-26）

5 个 dev connect 对话机器人不再手工敲命令，统一由 broadcast 托管：

| 命令 | 作用 |
|------|------|
| `python -m broadcast connect --start <bot\|all>` | 后台拉起（PID 文件 + 日志；all 二次确认） |
| `python -m broadcast connect --stop <bot\|all>` | 树杀（taskkill /T，连 Claude Code 子进程） |
| `python -m broadcast connect --status` | 全部 connect bot 状态 + 僵尸清理 |
| `python -m broadcast connect --logs <bot>` | 查日志（tail 最后 40 行） |

bot key：`cli` / `trading_q` / `data_q` / `strategy_q` / `review`。
配置真相源：`.env`（unified-app-id/身份闸/workdir）+ `broadcast/__main__.py:CONNECT_BOTS`。
进程管理实现：`broadcast/connect_manager.py`。
```

**2e. 删 market 相关残留**：全文搜索 `market` / `行情播报` / `DINGTALK_CHAT_ROBOT_CODE` / `4 机器人`。重点修正：
- 行 64 附近 `BROADCAST_GROUP_ID` 注释「4 机器人共用：行情既有 + 3 个观测层新机器人」→「3 机器人共用（trading/data/strategy，market 已下线）」。
- 「上线后常驻进程清单」表（行 174-185）：实测无 market 行（6 常驻 + 3 schtasks），此步对该表 no-op，但需 grep 确认无 `行情` 残留。

**2f. 「上线后常驻进程清单（一期 · 共 6 个）」表（行 174-185）**：yzzhanCli通用 / yzzhan参数优化 / 3 个 _q 的「启动命令见」列改为「`broadcast connect --start <bot>`」。

- [ ] **Step 3: 给两个 2026-07-16 market spec/plan 顶部加废弃标注**

在 `docs/superpowers/specs/2026-07-16-daily-market-brief-design.md` 与 `docs/superpowers/plans/2026-07-16-daily-market-brief.md` 的首个 `#` 标题行**下方**插入：

```markdown
> ⚠️ **已废弃（market 机器人下线，2026-07-26）**：本文档设计的「每日行情播报（market）」机器人已从 broadcast 移除。下线决策与清理清单见 `docs/superpowers/specs/2026-07-26-broadcast-robot-manager-design.md` §4.5。本文件保留作历史溯源，不再代表当前架构。
```

- [ ] **Step 4: 提交**

```bash
git add scripts/archive/ scripts/start_dingtalk_bots.md docs/superpowers/specs/2026-07-16-daily-market-brief-design.md docs/superpowers/plans/2026-07-16-daily-market-brief.md
git commit -m "docs: start_dingtalk_bots 5 dev connect 收编为 broadcast connect + market 文档归档/废弃

- start_dingtalk_bots.md：5 个 dev connect 手工命令 → 'broadcast connect --start <bot>'，新增 connect 托管四件套说明
- setup_broadcast_bot.md / setup_broadcast_schtasks.md 归档至 scripts/archive/（market 专属，保留历史溯源）
- 2026-07-16 daily-market-brief spec/plan 顶部加「已废弃」标注（不删，溯源）
- 配置真相源统一移至 .env + broadcast/__main__.py:CONNECT_BOTS

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## 验收对照（spec §8）

- [ ] `python -m broadcast --bot trading` 行为不变 → Task 4 Step 6 冒烟 + test_broadcast_main.py 全绿
- [ ] `python -m broadcast connect --start cli` 后台拉起 → Task 2 connect_manager + Task 4 _main_connect（真拉需用户配 .env 后执行）
- [ ] `--status` 显示 5 个 connect bot + 清僵尸 → Task 2 status + Task 4 Step 1 `test_connect_status_calls_manager`
- [ ] `--stop <bot>` 树杀无孤儿 → Task 2 `test_stop_uses_tree_kill` 断言 `/T`
- [ ] market 在代码/配置/文档全清；brief.py 仅共享工具；三个 brief 单测全绿 → Task 1 Step 4 + Task 3
- [ ] `test_connect_manager.py` 全绿（mock Popen，不真连钉钉）→ Task 2 Step 12
- [ ] `.env` 新增三项；start_dingtalk_bots.md 指向 broadcast connect → Task 5 + Task 6

---

## 用户侧外向动作（C6 · AI 不替按 · 实施完成后由用户执行）

```bash
# 1. 从 yzzhan量化群移除 market 机器人（spec §4.5 钉钉侧）
dws chat group members remove-bot --robot-code dingdya5o94mnmde7jlv --id ciduznBwLLiWKcMewBOF4+kWQ== -y
# 2.（可选）删除 market dws 应用：先 dws dev app list 查 app-id，再 dws dev app delete
# 3. 部署侧：删除 logs/.last_market_brief（运行时幂等文件，不入库）
# 4. 填 .env 实文件真值（Task 5 Step 2 清单）+ 删 DINGTALK_CHAT_ROBOT_CODE
# 5. 首次拉起 connect 机器人：python -m broadcast connect --start all（二次确认后逐个验）
```
