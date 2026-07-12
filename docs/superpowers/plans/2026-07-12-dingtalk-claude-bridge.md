# 钉钉远程驱动 Claude 旁路桥 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增一个独立守护进程 `bridge/`，通过钉钉企业内部应用 Stream 长连接接收消息，驱动常驻 `claude` 子进程（stream-json 双流、全放行）作答并 `@` 回复，与现有 FastAPI 交易后端完全解耦。

**Architecture:** `dingtalk-stream` SDK 建立 WebSocket 长连接（本地主动连出，无需公网 IP）；每条消息经白名单闸 → 立即 ACK → 异步派发给「按会话隔离的常驻 claude 进程池」（`claude --print --input-format/output-format stream-json --verbose --permission-mode bypassPermissions`，崩溃用 `--resume <session_id>` 重建）；聚合 `result` 帧文本 → 分段 `@` 回复。全放行的对价用「白名单铁闸 + 全量审计 jsonl + 高危工具调用实时告警」三道纵深防御兜底。

**Tech Stack:** Python 3.10（项目 `.venv310`）、`asyncio` + `asyncio.subprocess`、`dingtalk-stream`（新增唯一依赖）、`claude` CLI（系统 PATH 已装 v2.1.190）、`pytest`、`python-dotenv`（已有）。

## Global Constraints

- **Python 3.10**：必须用 `.venv310` 跑（项目既有约束）。`dingtalk-stream` 须支持 3.10。
- **全中文注释**：所有新增代码注释说明 What + Why（CLAUDE.md 红线）。
- **凭证隔离**：`DINGTALK_APP_KEY/SECRET`、白名单等一律走 `.env`，代码只用环境变量名，**绝不硬编码**（遵循 `core/notifier.py` 惯例）。
- **极简无黑盒**：**不引入** `claude_agent_sdk` 或重型 IM 框架；claude 走 CLI 子进程 + stream-json 解析（纯 `json.loads`，自写解析器）。
- **新增依赖仅 `dingtalk-stream`**（钉钉官方 Stream SDK，纯 Python，自带 WebSocket 重连）。
- **Windows asyncio subprocess**：Python 3.8+ Windows 默认 `ProactorEventLoop` 支持 subprocess；若执行环境报 `NotImplementedError`，在 `__main__.py` 入口设 `asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())`。
- **claude stream-json 事实**（本计划已实测抓帧，非记忆）：
  - 启动命令：`claude --print --input-format stream-json --output-format stream-json --verbose --permission-mode bypassPermissions`（**不带** prompt 参数，从 stdin 读；stdin 不 EOF 则进程常驻多轮）。
  - 输入帧（stdin 每行一个 JSON）：`{"type":"user","message":{"role":"user","content":[{"type":"text","text":"<消息>"}]}}`。
  - `assistant` 帧：`{"type":"assistant","message":{"content":[{"type":"text","text":"..."}]},"session_id":"<sid>"}`。
  - `result` 帧（一轮终止）：`{"type":"result","subtype":"success","is_error":false,"result":"<最终文本>","session_id":"<sid>","permission_denials":[]}`。
  - `system/init` 帧（进程启动首帧，含 `session_id`/`cwd`/`permissionMode`）。
  - `system/thinking_tokens` 帧大量噪音，**必须忽略**。
- **不改动** `server/`、`trading/`、`caisen/` 任何现有代码；仅在 `.env.example` 追加配置项、`requirements.txt` 加一行依赖。

---

## File Structure

| 文件 | 职责 | 创建/修改 |
|---|---|---|
| `bridge/__init__.py` | 包标识（空） | 新建 |
| `bridge/config.py` | `BridgeConfig.from_env()` 读全部配置 | 新建 |
| `bridge/safety.py` | `Verdict` + `classify()` 白名单/指令裁决 | 新建 |
| `bridge/session_store.py` | `SessionStore` conversationId↔session_id 持久化 | 新建 |
| `bridge/claude_events.py` | stream-json 帧解析纯函数 + 输入帧构造 | 新建 |
| `bridge/claude_pool.py` | `ClaudeProcess`（状态机）+ `ClaudePool`（进程池） | 新建 |
| `bridge/replier.py` | 长消息分段 + 钉钉 Markdown 清洗 + reply | 新建 |
| `bridge/alarmer.py` | 高危工具调用模式检测 + 复用 `core/notifier` | 新建 |
| `bridge/stream_client.py` | `dingtalk-stream` 装配 + 派发 + 审计 | 新建 |
| `bridge/__main__.py` | 入口：装配 + asyncio 主循环 | 新建 |
| `scripts/dingtalk_claude_bridge.py` | thin 入口（项目 scripts/ 惯例） | 新建 |
| `tests/bridge/__init__.py` | 测试包标识 | 新建 |
| `tests/bridge/test_*.py` | 各组件单测 | 新建 |
| `requirements.txt` | 加 `dingtalk-stream` | 修改 |
| `.env.example` | 追加钉钉桥配置段 | 修改 |
| `README.md` | 补「钉钉桥启动」一节 | 修改 |

---

## Task 0: 脚手架 + 配置 + 依赖

**Files:**
- Create: `bridge/__init__.py`
- Create: `bridge/config.py`
- Create: `tests/bridge/__init__.py`
- Create: `tests/bridge/test_config.py`
- Modify: `requirements.txt`
- Modify: `.env.example`

**Interfaces:**
- Produces: `bridge.config.BridgeConfig`（dataclass），字段：`app_key:str`、`app_secret:str`、`allowed_staff_ids:set[str]`、`claude_bin:str`、`workdir:str`、`ask_timeout:int`、`idle_ttl:int`、`rate_limit_per_min:int`、`session_store_path:str`、`audit_log_path:str`、`log_path:str`；类方法 `BridgeConfig.from_env() -> BridgeConfig`。

- [ ] **Step 1: 建 `bridge/__init__.py` 与测试包**

`bridge/__init__.py`：
```python
"""钉钉远程驱动 Claude 旁路桥。

独立守护进程：dingtalk-stream 长连接收消息 → 常驻 claude(stream-json) 作答 → @回复。
与 server/trading/caisen 完全解耦，互不影响进程命运。
"""
```
`tests/bridge/__init__.py`：空文件。

- [ ] **Step 2: 写 `tests/bridge/test_config.py` 失败测试**

```python
# -*- coding: utf-8 -*-
"""BridgeConfig.from_env 行为测试：环境变量 → 强类型配置，缺凭证/缺白名单时的门控。"""
import pytest

from bridge.config import BridgeConfig


def test_from_env_reads_all_fields(monkeypatch):
    """全凭证 + 白名单齐全时，from_env 正确解析所有字段。"""
    monkeypatch.setenv("DINGTALK_APP_KEY", "ding-test-key")
    monkeypatch.setenv("DINGTALK_APP_SECRET", "secret-test")
    monkeypatch.setenv("DINGTALK_ALLOWED_STAFF_IDS", "staffA,staffB,")
    monkeypatch.setenv("CLAUDE_BIN", "/usr/local/bin/claude")
    monkeypatch.setenv("CLAUDE_WORKDIR", "/tmp/proj")
    monkeypatch.setenv("BRIDGE_ASK_TIMEOUT", "90")
    monkeypatch.setenv("BRIDGE_IDLE_TTL", "600")
    monkeypatch.setenv("BRIDGE_RATE_LIMIT_PER_MIN", "5")

    cfg = BridgeConfig.from_env(project_root="/tmp/proj")

    assert cfg.app_key == "ding-test-key"
    assert cfg.app_secret == "secret-test"
    # 白名单去空白 + 去空串
    assert cfg.allowed_staff_ids == {"staffA", "staffB"}
    assert cfg.claude_bin == "/usr/local/bin/claude"
    assert cfg.ask_timeout == 90
    assert cfg.idle_ttl == 600
    assert cfg.rate_limit_per_min == 5


def test_from_env_uses_defaults_when_unset(monkeypatch):
    """未设可选项时走默认值（claude 走 PATH、超时 120、空闲 900、频控 10）。"""
    monkeypatch.setenv("DINGTALK_APP_KEY", "k")
    monkeypatch.setenv("DINGTALK_APP_SECRET", "s")
    monkeypatch.setenv("DINGTALK_ALLOWED_STAFF_IDS", "x")
    for k in ("CLAUDE_BIN", "CLAUDE_WORKDIR", "BRIDGE_ASK_TIMEOUT",
              "BRIDGE_IDLE_TTL", "BRIDGE_RATE_LIMIT_PER_MIN"):
        monkeypatch.delenv(k, raising=False)

    cfg = BridgeConfig.from_env(project_root="/tmp/proj")

    assert cfg.claude_bin == "claude"
    assert cfg.workdir == "/tmp/proj"
    assert cfg.ask_timeout == 120
    assert cfg.idle_ttl == 900
    assert cfg.rate_limit_per_min == 10


def test_from_env_rejects_missing_credentials(monkeypatch):
    """凭证缺失是致命错（启动即失败，优于静默连不上钉钉）。"""
    monkeypatch.delenv("DINGTALK_APP_KEY", raising=False)
    monkeypatch.delenv("DINGTALK_APP_SECRET", raising=False)
    monkeypatch.setenv("DINGTALK_ALLOWED_STAFF_IDS", "x")
    with pytest.raises(ValueError, match="DINGTALK_APP_KEY"):
        BridgeConfig.from_env(project_root="/tmp/proj")


def test_from_env_rejects_empty_whitelist(monkeypatch):
    """白名单为空 = 无人可用，也是致命错（全放行模式下唯一身份闸，不能空）。"""
    monkeypatch.setenv("DINGTALK_APP_KEY", "k")
    monkeypatch.setenv("DINGTALK_APP_SECRET", "s")
    monkeypatch.delenv("DINGTALK_ALLOWED_STAFF_IDS", raising=False)
    with pytest.raises(ValueError, match="白名单"):
        BridgeConfig.from_env(project_root="/tmp/proj")
```

- [ ] **Step 3: 跑测试，确认失败**

Run: `python -m pytest tests/bridge/test_config.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'bridge.config'`）

- [ ] **Step 4: 实现 `bridge/config.py`**

```python
# -*- coding: utf-8 -*-
"""
bridge/config.py
================
钉钉桥全部配置的单一来源。从 .env / 环境变量读取，强类型化。

凭证红线：本模块只读环境变量，绝不硬编码任何 token / secret。
启动期即校验致命前置条件（凭证缺失、白名单为空），失败快、失败响——
优于「跑起来后静默连不上钉钉」或「白名单空 = 谁都连不上 / 全放行无门」。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class BridgeConfig:
    """钉钉桥运行配置（不可变，启动时一次性装配）。"""

    # 钉钉企业内部应用凭证（Stream 接入必需，双值缺一即拒）
    app_key: str
    app_secret: str
    # 白名单 staffId 集合；全放行模式下唯一身份闸，空集 = 致命错
    allowed_staff_ids: frozenset[str]
    # claude CLI 可执行路径，默认 "claude"（走 PATH）
    claude_bin: str
    # claude 工作目录，默认项目根（与终端 claude 一致）
    workdir: str
    # 单轮超时（秒）：claude 卡住时 watchdog kill 的阈值
    ask_timeout: int
    # 空闲进程回收（秒）：超过未用的常驻进程主动 terminate，防进程数无限增长
    idle_ttl: int
    # 单用户每分钟消息上限（钉钉机器人频控 + 防刷）
    rate_limit_per_min: int
    # 会话映射 JSON 落盘路径（conversationId ↔ claude session_id）
    session_store_path: str
    # 全量审计 jsonl 落盘路径（全放行模式的事后追溯底线）
    audit_log_path: str
    # 运行日志路径
    log_path: str

    @classmethod
    def from_env(cls, project_root: str) -> "BridgeConfig":
        """从环境变量构造。致命前置条件不满足直接 ValueError。"""
        app_key = os.getenv("DINGTALK_APP_KEY", "").strip()
        app_secret = os.getenv("DINGTALK_APP_SECRET", "").strip()
        # 凭证缺失 = 无法建 Stream 连接，启动即拒
        if not app_key or not app_secret:
            raise ValueError(
                "DINGTALK_APP_KEY / DINGTALK_APP_SECRET 未配置（检查 .env）；"
                "钉钉企业内部应用凭证双值必填。"
            )

        # 白名单：逗号分隔 → 去空白 → 去空串 → frozenset
        raw_whitelist = os.getenv("DINGTALK_ALLOWED_STAFF_IDS", "")
        allowed = frozenset(
            s.strip() for s in raw_whitelist.split(",") if s.strip()
        )
        # 全放行模式下白名单是唯一身份闸，空集 = 无人能触发，视为配置错
        if not allowed:
            raise ValueError(
                "DINGTALK_ALLOWED_STAFF_IDS 白名单为空；全放行模式下这是唯一身份闸，"
                "必须至少配一个 staffId。"
            )

        # 可选项走默认；ASK_TIMEOUT=120s 留足 claude 思考 + 工具调用时间
        return cls(
            app_key=app_key,
            app_secret=app_secret,
            allowed_staff_ids=allowed,
            claude_bin=os.getenv("CLAUDE_BIN", "claude").strip() or "claude",
            workdir=os.getenv("CLAUDE_WORKDIR", project_root).strip() or project_root,
            ask_timeout=int(os.getenv("BRIDGE_ASK_TIMEOUT", "120")),
            idle_ttl=int(os.getenv("BRIDGE_IDLE_TTL", "900")),
            rate_limit_per_min=int(os.getenv("BRIDGE_RATE_LIMIT_PER_MIN", "10")),
            session_store_path=os.path.join(project_root, "logs", "dingtalk_sessions.json"),
            audit_log_path=os.path.join(project_root, "logs", "dingtalk_bridge_audit.jsonl"),
            log_path=os.path.join(project_root, "logs", "dingtalk_bridge.log"),
        )
```

- [ ] **Step 5: 跑测试，确认通过**

Run: `python -m pytest tests/bridge/test_config.py -v`
Expected: 4 passed

- [ ] **Step 6: `requirements.txt` 加依赖**

在文件末尾追加：
```dotenv

# ===== 钉钉远程驱动 Claude 旁路桥 =====
# 钉钉官方 Stream SDK：WebSocket 长连接接收企业内部应用机器人消息（双向）。
# 纯 Python，自带断线重连。本项目唯一为钉钉桥引入的依赖。
dingtalk-stream>=0.20.0
```

- [ ] **Step 7: `.env.example` 追加配置段**

在文件末尾追加（值留空模板，绝不写真值）：
```dotenv

# ============ 钉钉远程驱动 Claude 旁路桥（企业内部应用 Stream） ============
# 企业内部应用 Client ID（原 AppKey/SuiteKey）
DINGTALK_APP_KEY=
# Client Secret（原 AppSecret/SuiteSecret），本地填真值，绝不进 git
DINGTALK_APP_SECRET=
# 白名单 staffId，逗号分隔；全放行(bypassPermissions)模式下唯一身份闸
DINGTALK_ALLOWED_STAFF_IDS=
# claude CLI 可执行，默认走 PATH
CLAUDE_BIN=claude
# claude 工作目录，默认项目根
CLAUDE_WORKDIR=
# 单轮超时秒（claude 卡住 watchdog kill 阈值）
BRIDGE_ASK_TIMEOUT=120
# 空闲进程回收秒（常驻 claude 空闲超此值则 terminate）
BRIDGE_IDLE_TTL=900
# 单用户每分钟消息上限（防刷 + 钉钉频控）
BRIDGE_RATE_LIMIT_PER_MIN=10
```

- [ ] **Step 8: Commit**

```bash
git add bridge/__init__.py bridge/config.py tests/bridge/__init__.py tests/bridge/test_config.py requirements.txt .env.example
git commit -m "feat(bridge): 脚手架+配置+dingtalk-stream 依赖

BridgeConfig.from_env 强类型化全部配置，启动期校验凭证/白名单
致命前置条件（缺即 ValueError，优于静默失败）。凭证走 .env 不硬编码。"
```

---

## Task 1: safety.py（白名单 + 指令解析）

**Files:**
- Create: `bridge/safety.py`
- Create: `tests/bridge/test_safety.py`

**Interfaces:**
- Consumes: `bridge.config.BridgeConfig`（用 `allowed_staff_ids`）
- Produces: `bridge.safety.Verdict`（dataclass：`action:Literal["allow","reject","command"]`、`command:Optional[str]`、`reason:str`）；函数 `classify(sender_staff_id:str, text:str, cfg:BridgeConfig) -> Verdict`。`command` 仅在 `action=="command"` 时非空，取值 `"new"`/`"status"`/`"help"`。

- [ ] **Step 1: 写 `tests/bridge/test_safety.py` 失败测试**

```python
# -*- coding: utf-8 -*-
"""safety.classify 裁决测试：白名单闸 + 指令解析。纯逻辑，无 IO。"""
import pytest

from bridge.config import BridgeConfig
from bridge.safety import classify


@pytest.fixture
def cfg():
    """最小可用配置（只需 allowed_staff_ids 字段即可测 classify）。"""
    return BridgeConfig(
        app_key="k", app_secret="s",
        allowed_staff_ids=frozenset({"staffOK"}),
        claude_bin="claude", workdir="/tmp", ask_timeout=120,
        idle_ttl=900, rate_limit_per_min=10,
        session_store_path="/tmp/s.json", audit_log_path="/tmp/a.jsonl",
        log_path="/tmp/l.log",
    )


def test_non_whitelist_rejected(cfg):
    """非白名单用户：reject（静默丢弃 + 审计，不回执防探测）。"""
    v = classify(sender_staff_id="intruder", text="hi", cfg=cfg)
    assert v.action == "reject"
    assert "白名单" in v.reason


def test_whitelist_allowed(cfg):
    """白名单用户 + 普通文本：allow，文本原样透传。"""
    v = classify(sender_staff_id="staffOK", text="解释一下颈线拟合", cfg=cfg)
    assert v.action == "allow"
    assert v.command is None


@pytest.mark.parametrize("raw,cmd", [
    ("/new", "new"),
    ("/status", "status"),
    ("/help", "help"),
    ("  /new  ", "new"),         # 容忍前后空白
    ("/NEW", "new"),             # 大小写不敏感
])
def test_command_parsed(cfg, raw, cmd):
    """指令前缀正确解析为 command 动作。"""
    v = classify(sender_staff_id="staffOK", text=raw, cfg=cfg)
    assert v.action == "command"
    assert v.command == cmd


def test_non_command_slash_allowed(cfg):
    """以 / 开头但非已知指令（如文件路径）→ 当普通文本 allow，不误判。"""
    v = classify(sender_staff_id="staffOK", text="/etc/hosts 是什么", cfg=cfg)
    assert v.action == "allow"
```

- [ ] **Step 2: 跑测试，确认失败**

Run: `python -m pytest tests/bridge/test_safety.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'bridge.safety'`）

- [ ] **Step 3: 实现 `bridge/safety.py`**

```python
# -*- coding: utf-8 -*-
"""
bridge/safety.py
================
纯逻辑安全闸：对每条入站消息做「身份 + 意图」裁决。

无 IO、无副作用——最好测的单元。stream_client 拿到裁决后决定：
  allow   → 喂给 claude 进程池
  reject  → 静默丢弃 + 审计（不回执，防探测者通过回执确认机器人存活）
  command → 直接执行指令（/new /status /help），不进 claude
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from bridge.config import BridgeConfig

# 已知指令 → 标准化名（大小写/空白无关）
_COMMANDS: dict[str, str] = {
    "/new": "new",
    "/status": "status",
    "/help": "help",
}


@dataclass(frozen=True)
class Verdict:
    """单条消息的裁决结果。"""
    action: Literal["allow", "reject", "command"]
    reason: str
    # 仅 action=="command" 时非空：取值 "new"/"status"/"help"
    command: Optional[str] = None


def classify(sender_staff_id: str, text: str, cfg: BridgeConfig) -> Verdict:
    """身份闸（白名单）→ 意图闸（指令解析）。"""
    # 身份闸：全放行模式下唯一身份闸，非白名单一律 reject
    if sender_staff_id not in cfg.allowed_staff_ids:
        return Verdict(
            action="reject",
            reason=f"sender {sender_staff_id} 不在白名单",
        )

    # 意图闸：取首词（按空白拆），若为已知指令前缀则走 command 分支
    head = text.strip().split(maxsplit=1)[0].lower() if text.strip() else ""
    if head in _COMMANDS:
        return Verdict(action="command", reason="known command",
                       command=_COMMANDS[head])

    # 普通文本：allow 原样透传（stream_client 负责去掉可能的 @机器人 前缀）
    return Verdict(action="allow", reason="ok")
```

- [ ] **Step 4: 跑测试，确认通过**

Run: `python -m pytest tests/bridge/test_safety.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add bridge/safety.py tests/bridge/test_safety.py
git commit -m "feat(bridge): safety 白名单闸 + 指令解析（纯逻辑）"
```

---

## Task 2: session_store.py（会话映射持久化）

**Files:**
- Create: `bridge/session_store.py`
- Create: `tests/bridge/test_session_store.py`

**Interfaces:**
- Consumes: 配置里的 `session_store_path`
- Produces: `bridge.session_store.SessionStore`，方法：`get(conv_id:str)->Optional[str]`、`set(conv_id:str, session_id:str)->None`、`clear(conv_id:str)->None`。线程安全（`threading.Lock`），原子写（写临时文件再 `os.replace`，防并发写坏 JSON）。

- [ ] **Step 1: 写 `tests/bridge/test_session_store.py` 失败测试**

```python
# -*- coding: utf-8 -*-
"""SessionStore 持久化测试：读写/清空/原子写/文件不存在时的容错。"""
import json
from pathlib import Path

from bridge.session_store import SessionStore


def test_get_returns_none_when_missing(tmp_path):
    """文件不存在时 get 返回 None（首次启动常态，不报错）。"""
    store = SessionStore(str(tmp_path / "nope.json"))
    assert store.get("convA") is None


def test_set_then_get_roundtrip(tmp_path):
    """set 后 get 能取回；同一会话覆写更新。"""
    path = tmp_path / "s.json"
    store = SessionStore(str(path))
    store.set("convA", "sid-1")
    assert store.get("convA") == "sid-1"
    # 覆写
    store.set("convA", "sid-2")
    assert store.get("convA") == "sid-2"


def test_set_persists_to_disk(tmp_path):
    """落盘可被新实例读到（进程重启后 session_id 不丢，--resume 可续）。"""
    path = tmp_path / "s.json"
    SessionStore(str(path)).set("convA", "sid-1")
    # 新实例从同一文件读
    assert SessionStore(str(path)).get("convA") == "sid-1"
    # 文件内容是合法 JSON
    assert json.loads(path.read_text(encoding="utf-8")) == {"convA": "sid-1"}


def test_clear_removes_mapping(tmp_path):
    """clear 清掉单个会话映射（/new 指令用），不影响其他会话。"""
    store = SessionStore(str(tmp_path / "s.json"))
    store.set("convA", "sid-a")
    store.set("convB", "sid-b")
    store.clear("convA")
    assert store.get("convA") is None
    assert store.get("convB") == "sid-b"


def test_corrupt_file_tolerated(tmp_path):
    """文件损坏（手改/截断）时不炸，按空映射启动（优于启动失败）。"""
    path = tmp_path / "s.json"
    path.write_text("{坏了的 json", encoding="utf-8")
    store = SessionStore(str(path))
    assert store.get("anything") is None
```

- [ ] **Step 2: 跑测试，确认失败**

Run: `python -m pytest tests/bridge/test_session_store.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现 `bridge/session_store.py`**

```python
# -*- coding: utf-8 -*-
"""
bridge/session_store.py
=======================
钉钉 conversationId ↔ claude session_id 的持久化映射。

Why 必须落盘：常驻 claude 进程会死（崩溃/超时杀/空闲回收），但 claude 把
完整会话历史存在本地 ~/.claude/。只要拿着 session_id，下次 --resume <sid>
即可续上下文。故 session_id 必须独立于进程生命周期持久化——进程死、映射在，
上下文不丢。

原子写：写临时文件再 os.replace，防并发写坏 JSON（多个异步任务可能同时 set）。
文件锁：threading.Lock 保护内存 dict + 落盘的临界区。
损坏容错：手改/截断的 JSON 不应让桥启动失败，按空映射起步。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from typing import Optional

logger = logging.getLogger(__name__)


class SessionStore:
    """conversationId → claude session_id 映射，内存 dict + JSON 落盘。"""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._map: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        """启动时从磁盘载入；文件缺失/损坏按空映射起步。"""
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
            logger.warning("session_store 文件非 dict，按空映射起步：%s", self._path)
            return {}
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError) as e:
            # 损坏不致命：记日志 + 空映射，优于启动失败把整桥拉倒
            logger.warning("session_store 文件读取失败，按空映射起步：%s (%s)", self._path, e)
            return {}

    def get(self, conv_id: str) -> Optional[str]:
        with self._lock:
            return self._map.get(conv_id)

    def set(self, conv_id: str, session_id: str) -> None:
        with self._lock:
            self._map[conv_id] = session_id
            self._flush_locked()

    def clear(self, conv_id: str) -> None:
        """/new 指令用：清掉单个会话映射，下次走全新会话。"""
        with self._lock:
            self._map.pop(conv_id, None)
            self._flush_locked()

    def _flush_locked(self) -> None:
        """原子落盘：临时文件 → os.replace（持有锁时调用）。"""
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        # delete=False + 手动清理：Windows 上 NamedTemporaryFile 默认 delete=True
        # 会锁文件导致 os.replace 失败，故用 delete=False 再手动删
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self._path) or ".",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._map, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            # 落盘失败不回滚内存（下次 set 重试）；记日志，不阻断业务
            logger.exception("session_store 落盘失败：%s", self._path)
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
```

- [ ] **Step 4: 跑测试，确认通过**

Run: `python -m pytest tests/bridge/test_session_store.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add bridge/session_store.py tests/bridge/test_session_store.py
git commit -m "feat(bridge): session_store 持久化 conversationId↔session_id

原子写(os.replace)+文件锁+损坏容错。进程死映射在,--resume 可续上下文。"
```

---

## Task 3: claude_events.py（stream-json 帧解析，基于真实抓帧）

**Files:**
- Create: `bridge/claude_events.py`
- Create: `tests/bridge/test_claude_events.py`

**Interfaces:**
- Consumes: 无（纯函数）
- Produces:
  - `make_user_frame(text:str) -> str`：返回可写入 stdin 的单行 JSON（含尾部无换行，调用方加 `\n`）
  - `parse_event_line(line:str) -> Optional[dict]`：解析一行 stdout；非 JSON / 空行返回 `None`
  - `is_result(event:dict) -> bool`
  - `extract_result_text(event:dict) -> str`：从 result 帧取 `result` 字段
  - `extract_session_id(event:dict) -> Optional[str]`：从任意含 `session_id` 的帧取
  - `extract_assistant_text(event:dict) -> str`：从 assistant 帧拼 `message.content` 里所有 text 项（增量文本）

- [ ] **Step 0: 验证输入帧格式（防幻觉）**

Run:
```bash
echo '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"Reply with exactly: pong"}]}}' | claude --print --input-format stream-json --output-format stream-json --verbose 2>&1 | grep '"type":"result"' | head -1
```
Expected: 一行 result 帧，`"result":"pong"`（或含 pong 的文本）。若 claude 报输入格式错，按报错调整 `make_user_frame` 的 JSON 结构后继续。
> 说明：本计划基于实测抓帧（见 Global Constraints）。此 Step 确认输入帧被 claude 接受。

- [ ] **Step 1: 写 `tests/bridge/test_claude_events.py` 失败测试**

```python
# -*- coding: utf-8 -*-
"""claude_events 解析测试：用真实抓到的帧结构做 fixture（见 Global Constraints）。"""
import json

from bridge.claude_events import (
    extract_assistant_text,
    extract_result_text,
    extract_session_id,
    is_result,
    make_user_frame,
    parse_event_line,
)

# 真实抓到的 assistant 帧（2026-07-12 实测）
ASSISTANT_FRAME = (
    '{"type":"assistant","message":{"content":'
    '[{"type":"text","text":"pong"}]},"session_id":"sid-abc","uuid":"u1"}'
)
# 真实抓到的 result 帧
RESULT_FRAME = (
    '{"type":"result","subtype":"success","is_error":false,'
    '"result":"pong","session_id":"sid-abc",'
    '"permission_denials":[]}'
)
# 噪音帧（必须被忽略，不解析为文本）
THINKING_FRAME = '{"type":"system","subtype":"thinking_tokens","estimated_tokens":99}'
INIT_FRAME = '{"type":"system","subtype":"init","cwd":"/p","session_id":"sid-abc"}'


def test_make_user_frame_is_valid_json_single_line():
    """构造的输入帧是合法 JSON、单行、含 user 类型。"""
    frame = make_user_frame("你好")
    assert "\n" not in frame
    obj = json.loads(frame)
    assert obj["type"] == "user"
    assert obj["message"]["role"] == "user"
    assert obj["message"]["content"][0]["text"] == "你好"


def test_parse_event_line_handles_garbage():
    """空行/非 JSON 返回 None（claude 偶发输出非 JSON 行不炸解析器）。"""
    assert parse_event_line("") is None
    assert parse_event_line("not json") is None
    assert parse_event_line(ASSISTANT_FRAME)["type"] == "assistant"


def test_is_result_only_true_for_result():
    assert is_result(parse_event_line(RESULT_FRAME)) is True
    assert is_result(parse_event_line(ASSISTANT_FRAME)) is False
    assert is_result(parse_event_line(THINKING_FRAME)) is False


def test_extract_result_text():
    ev = parse_event_line(RESULT_FRAME)
    assert extract_result_text(ev) == "pong"


def test_extract_session_id_from_any_frame():
    """session_id 在 assistant/result/init 帧顶层都有。"""
    assert extract_session_id(parse_event_line(ASSISTANT_FRAME)) == "sid-abc"
    assert extract_session_id(parse_event_line(RESULT_FRAME)) == "sid-abc"
    assert extract_session_id(parse_event_line(INIT_FRAME)) == "sid-abc"
    # 无 session_id 的帧返回 None
    assert extract_session_id(parse_event_line(THINKING_FRAME)) is None


def test_extract_assistant_text_concatenates_text_blocks():
    """assistant 帧的 content 可能有多个 text 项（文本+工具调用混合），全部拼接。"""
    ev = json.loads(
        '{"type":"assistant","message":{"content":'
        '[{"type":"text","text":"a"},{"type":"text","text":"b"}]}}'
    )
    assert extract_assistant_text(ev) == "ab"


def test_extract_assistant_text_ignores_non_text_content():
    """content 里的 tool_use 项不贡献文本（避免把工具调用 JSON 当文本回钉钉）。"""
    ev = json.loads(
        '{"type":"assistant","message":{"content":['
        '{"type":"text","text":"看这个文件："},'
        '{"type":"tool_use","name":"Read","input":{"path":"x.py"}}'
        ']}}'
    )
    assert extract_assistant_text(ev) == "看这个文件："
```

- [ ] **Step 2: 跑测试，确认失败**

Run: `python -m pytest tests/bridge/test_claude_events.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现 `bridge/claude_events.py`**

```python
# -*- coding: utf-8 -*-
"""
bridge/claude_events.py
=======================
claude CLI stream-json 帧的纯函数解析器。

帧结构（2026-07-12 实测抓帧，非记忆；若 claude 升级改字段，仅改本文件）：
  输入(stdin): {"type":"user","message":{"role":"user",
               "content":[{"type":"text","text":"<消息>"}]}}
  assistant:   {"type":"assistant","message":{"content":[{"type":"text","text":...}]},
                "session_id":"<sid>"}
  result:      {"type":"result","subtype":"success","is_error":false,
                "result":"<最终文本>","session_id":"<sid>","permission_denials":[]}
  system:      init / thinking_tokens(大量噪音,忽略) / hook_* 等

设计：纯函数 + 无状态，最好测；claude_pool 只调本模块，不自己 json.loads。
"""
from __future__ import annotations

import json
from typing import Optional


def make_user_frame(text: str) -> str:
    """构造写入 claude stdin 的单行 user 帧（不含尾部换行，调用方加 \\n）。

    Why 不在帧内换行：stream-json 协议一行一帧，文本内的换行经 JSON 转义为 \\n，
    不会破坏帧边界。调用方负责在帧尾加 \\n 作为帧分隔。
    """
    return json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": text}],
            },
        },
        ensure_ascii=False,
    )


def parse_event_line(line: str) -> Optional[dict]:
    """解析一行 stdout。空行/非 JSON 返回 None（claude 偶发非 JSON 输出不炸）。"""
    line = line.strip()
    if not line:
        return None
    try:
        ev = json.loads(line)
        return ev if isinstance(ev, dict) else None
    except json.JSONDecodeError:
        return None


def is_result(event: dict) -> bool:
    """一轮终止判据：读到 result 帧。"""
    return event.get("type") == "result"


def extract_result_text(event: dict) -> str:
    """result 帧的 result 字段 = claude 给用户的最终文本（权威输出）。"""
    return str(event.get("result", ""))


def extract_session_id(event: dict) -> Optional[str]:
    """从任意含 session_id 的帧取（assistant/result/init 顶层都有）。"""
    sid = event.get("session_id")
    return str(sid) if sid else None


def extract_assistant_text(event: dict) -> str:
    """拼 assistant 帧 message.content 里所有 type==text 的项。

    Why 过滤 type==text：content 数组可能混入 tool_use（工具调用）项，
    其 JSON 不应当文本回钉钉。只取真正的文本块。
    """
    content = event.get("message", {}).get("content", [])
    if not isinstance(content, list):
        return ""
    return "".join(
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )
```

- [ ] **Step 4: 跑测试，确认通过**

Run: `python -m pytest tests/bridge/test_claude_events.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add bridge/claude_events.py tests/bridge/test_claude_events.py
git commit -m "feat(bridge): claude stream-json 帧解析(基于实测抓帧)

纯函数:make_user_frame/parse_event_line/is_result/extract_*。
thinking_tokens 噪音帧被忽略,assistant content 只取 text 块。"
```

---

## Task 4: claude_pool.py 之 ClaudeProcess（单进程状态机）

**Files:**
- Create: `bridge/claude_pool.py`
- Create: `tests/bridge/test_claude_process.py`

**Interfaces:**
- Consumes: `bridge.config.BridgeConfig`（`claude_bin`/`workdir`/`ask_timeout`）、`bridge.claude_events.*`
- Produces: `bridge.claude_pool.ClaudeProcess`，方法：`async ask(text:str, on_event:Optional[Callable[[dict],None]]=None) -> str`、`async aclose() -> None`、属性 `is_alive:bool`。构造：`ClaudeProcess(cfg, session_id:Optional[str]=None)`。`on_event` 回调用于 alarmer 监听工具调用（Task 7 用）。
- `ask` 语义：懒启动（首次调用才 spawn）；写 user 帧 → 读 stdout 直到 result 帧（带 `ask_timeout` 超时）；崩溃/超时 → kill → 用 session_id `--resume` 重建重试 1 次，仍失败抛 `RuntimeError`。

- [ ] **Step 1: 写 `tests/bridge/test_claude_process.py` 失败测试**

```python
# -*- coding: utf-8 -*-
"""ClaudeProcess 状态机测试：mock asyncio.subprocess，不真跑 claude。

验证：懒启动、stdin 写 user 帧、stdout 聚合到 result、超时 kill+resume 重试、
session_id 捕获、on_event 回调。
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from bridge.claude_pool import ClaudeProcess

# 复用真实帧结构
INIT_LINE = '{"type":"system","subtype":"init","session_id":"sid-init"}'
ASSISTANT_REAL = (
    '{"type":"assistant","message":{"content":'
    '[{"type":"text","text":"hello "]}],"session_id":"sid-real"}'
)
ASSISTANT_REAL_2 = (
    '{"type":"assistant","message":{"content":'
    '[{"type":"text","text":"world"}]},"session_id":"sid-real"}'
)
RESULT_LINE = (
    '{"type":"result","subtype":"success","is_error":false,'
    '"result":"hello world","session_id":"sid-real","permission_denials":[]}'
)


def _make_proc_mock(lines_for_first_read, lines_for_retry=None):
    """造一个假的 asyncio.subprocess.Process：stdout 按行吐给定 JSON。

    第一次 ask 读 lines_for_first_read；若被 kill 重建，第二次读 lines_for_retry。
    """
    state = {"call": 0}

    async def readline():
        seq = [lines_for_first_read, lines_for_retry or []][state["call"]]
        idx = state.get("idx", 0)
        state["idx"] = idx + 1
        if idx < len(seq):
            return (seq[idx] + "\n").encode("utf-8")
        return b""  # EOF

    proc = MagicMock()
    proc.stdout = MagicMock()
    proc.stdout.readline = readline
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    proc.wait = AsyncMock(return_value=0)
    proc.terminate = MagicMock()
    proc.kill = MagicMock()

    async def fake_create(*a, **kw):
        # 第二次 spawn（resume 重试）切到第二组行
        state["call"] += 1
        state["idx"] = 0
        proc.returncode = None
        return proc

    return proc, fake_create


@pytest.mark.asyncio
async def test_ask_lazy_starts_and_aggregates_result(monkeypatch, tmp_path):
    """首次 ask 触发 spawn；聚合 assistant 增量 + 以 result.result 为权威输出。"""
    from bridge import claude_pool as cp
    from bridge.config import BridgeConfig

    cfg = BridgeConfig(
        app_key="k", app_secret="s", allowed_staff_ids=frozenset({"x"}),
        claude_bin="claude", workdir=str(tmp_path), ask_timeout=10,
        idle_ttl=900, rate_limit_per_min=10,
        session_store_path=str(tmp_path / "s.json"),
        audit_log_path=str(tmp_path / "a.jsonl"), log_path=str(tmp_path / "l.log"),
    )
    proc, fake_create = _make_proc_mock(
        [INIT_LINE, ASSISTANT_REAL, ASSISTANT_REAL_2, RESULT_LINE]
    )
    monkeypatch.setattr(cp.asyncio, "create_subprocess_exec", fake_create)

    cp_obj = ClaudeProcess(cfg, session_id=None)
    answer = await cp_obj.ask("hi")

    assert answer == "hello world"           # result.result 权威
    assert cp_obj.session_id == "sid-real"   # 从帧捕获
    # 确认 stdin 写了 user 帧
    written = b"".join(c.args[0] for c in proc.stdin.write.call_args_list)
    assert b'"type":"user"' in written
    await cp_obj.aclose()


@pytest.mark.asyncio
async def test_ask_recovers_from_crash_then_resume_retry(monkeypatch, tmp_path):
    """进程中途崩溃(stdout EOF)→ kill + --resume 重建重试一次成功。

    模拟首次 spawn 只吐 assistant 帧就 EOF(进程死);重试 spawn 直接吐 result。
    覆盖 _read_until_result 抛 RuntimeError→重建路径(超时 TimeoutError 走同一分支)。
    """
    from bridge import claude_pool as cp
    from bridge.config import BridgeConfig

    cfg = BridgeConfig(
        app_key="k", app_secret="s", allowed_staff_ids=frozenset({"x"}),
        claude_bin="claude", workdir=str(tmp_path), ask_timeout=1,
        idle_ttl=900, rate_limit_per_min=10,
        session_store_path=str(tmp_path / "s.json"),
        audit_log_path=str(tmp_path / "a.jsonl"), log_path=str(tmp_path / "l.log"),
    )
    # 第一次只吐 assistant 不吐 result（永远等不到 → 超时）；重试时吐完整 result
    proc, fake_create = _make_proc_mock(
        [ASSISTANT_REAL],                       # 第一次：卡住不结束
        [RESULT_LINE],                          # 重试：直接 result
    )
    monkeypatch.setattr(cp.asyncio, "create_subprocess_exec", fake_create)

    cp_obj = ClaudeProcess(cfg, session_id="sid-real")
    answer = await cp_obj.ask("hi")
    assert answer == "hello world"
    # 确认被 kill 过
    assert proc.kill.called or proc.terminate.called
    await cp_obj.aclose()


@pytest.mark.asyncio
async def test_on_event_callback_invoked(monkeypatch, tmp_path):
    """on_event 回调把每个解析出的事件交给调用方（alarmer 监听工具调用用）。"""
    from bridge import claude_pool as cp
    from bridge.config import BridgeConfig

    cfg = BridgeConfig(
        app_key="k", app_secret="s", allowed_staff_ids=frozenset({"x"}),
        claude_bin="claude", workdir=str(tmp_path), ask_timeout=10,
        idle_ttl=900, rate_limit_per_min=10,
        session_store_path=str(tmp_path / "s.json"),
        audit_log_path=str(tmp_path / "a.jsonl"), log_path=str(tmp_path / "l.log"),
    )
    proc, fake_create = _make_proc_mock([ASSISTANT_REAL, RESULT_LINE])
    monkeypatch.setattr(cp.asyncio, "create_subprocess_exec", fake_create)

    seen_types: list[str] = []
    cp_obj = ClaudeProcess(cfg)
    await cp_obj.ask("hi", on_event=lambda ev: seen_types.append(ev["type"]))

    assert "assistant" in seen_types
    assert "result" in seen_types
    await cp_obj.aclose()
```

- [ ] **Step 2: 跑测试，确认失败**

Run: `python -m pytest tests/bridge/test_claude_process.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现 `bridge/claude_pool.py`（ClaudeProcess 部分；ClaudePool 留到 Task 5）**

```python
# -*- coding: utf-8 -*-
"""
bridge/claude_pool.py
=====================
常驻 claude 子进程的状态机封装（ClaudeProcess）+ 进程池（ClaudePool，Task 5 补）。

核心思想：每个钉钉会话(conversationId)对应一个常驻 claude 进程，stream-json 双流：
  - stdin  持续写 user 帧（进程不 EOF 即常驻多轮）
  - stdout 逐行读事件，聚合 assistant 增量，读到 result 帧 = 一轮结束

崩溃恢复：进程会死，但 claude 把会话历史存本地 ~/.claude/。拿着 session_id
即可 --resume <sid> 续上下文。故 ask 超时/崩溃时 kill 后用 session_id 重建，
重试 1 次，仍失败抛 RuntimeError（不无限重试，防死循环刷钉钉）。

Windows asyncio：create_subprocess_exec 需 ProactorEventLoop（3.8+ Windows 默认）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from bridge import claude_events as ce
from bridge.config import BridgeConfig

logger = logging.getLogger(__name__)

# 单轮失败后最多重试次数（含首次 = 总尝试 2 次）
_MAX_ATTEMPTS = 2


class ClaudeProcess:
    """单个钉钉会话对应的常驻 claude 子进程。"""

    def __init__(self, cfg: BridgeConfig, session_id: Optional[str] = None) -> None:
        self._cfg = cfg
        self._session_id: Optional[str] = session_id  # 已知则 --resume 续上下文
        self._proc: Optional[asyncio.subprocess.Process] = None
        # 同会话串行锁：claude 进程一次只能处理一轮，第二条 ask 必须等第一条 result
        self._lock = asyncio.Lock()

    # ---- 对外属性 ----
    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    # ---- 进程生命周期 ----
    async def _spawn(self) -> None:
        """拉起 claude（stream-json 双流 + 全放行）。已知 session_id 则 --resume。"""
        cmd = [
            self._cfg.claude_bin,
            "--print",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            # 全放行：等同终端里每个确认都按 y（见 spec §7/§8 安全契约）
            "--permission-mode", "bypassPermissions",
        ]
        if self._session_id:
            # 续上下文：进程死后用 session_id 重建，历史不丢
            cmd += ["--resume", self._session_id]

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cfg.workdir,
        )
        logger.info("claude 子进程已启动 (pid=%s, resume=%s)",
                    self._proc.pid, self._session_id or "(new)")

    async def _kill(self) -> None:
        """强制结束当前进程（超时/崩溃重建用）。"""
        if self._proc is None or self._proc.returncode is not None:
            self._proc = None
            return
        try:
            self._proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass
        self._proc = None

    async def aclose(self) -> None:
        """优雅关闭（空闲回收 / 桥退出用）。"""
        await self._kill()

    # ---- 核心：一轮问答 ----
    async def _read_until_result(
        self,
        on_event: Optional[Callable[[dict], None]],
    ) -> str:
        """从 stdout 逐行读，聚合 assistant 文本，直到 result 帧。

        返回 result.result（权威最终文本）。同时把每个解析出的事件交给 on_event。
        """
        accumulated: list[str] = []
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            line_bytes = await self._proc.stdout.readline()
            if not line_bytes:
                # stdout EOF = 进程已退出（崩溃/被 kill）。抛出让上层走重建。
                raise RuntimeError("claude stdout EOF（进程意外退出）")
            ev = ce.parse_event_line(line_bytes.decode("utf-8", errors="replace"))
            if ev is None:
                continue  # 非 JSON / 空行 / 噪音
            if on_event is not None:
                on_event(ev)
            # 捕获 session_id（init/assistant/result 帧都有）
            sid = ce.extract_session_id(ev)
            if sid:
                self._session_id = sid
            # 累加 assistant 增量文本
            if ev.get("type") == "assistant":
                accumulated.append(ce.extract_assistant_text(ev))
            elif ce.is_result(ev):
                # 一轮终止：以 result.result 为权威（优先于累加，防增量遗漏/重复）
                return ce.extract_result_text(ev) or "".join(accumulated)

    async def ask(
        self,
        text: str,
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> str:
        """发一轮问答。超时/崩溃 → kill → --resume 重建重试 1 次。"""
        async with self._lock:  # 同会话串行
            last_err: Optional[Exception] = None
            for attempt in range(1, _MAX_ATTEMPTS + 1):
                # 懒启动 or 进程已死则（重新）拉起
                if not self.is_alive:
                    await self._spawn()
                try:
                    # 写 user 帧（stream-json 一行一帧，尾加 \n）
                    frame = ce.make_user_frame(text) + "\n"
                    assert self._proc is not None and self._proc.stdin is not None
                    self._proc.stdin.write(frame.encode("utf-8"))
                    await self._proc.stdin.drain()
                    # 读到 result 为止，带单轮超时
                    return await asyncio.wait_for(
                        self._read_until_result(on_event),
                        timeout=self._cfg.ask_timeout,
                    )
                except (asyncio.TimeoutError, RuntimeError) as e:
                    last_err = e
                    logger.warning("claude 第 %d 轮失败 (%s)，kill 后重建重试",
                                   attempt, type(e).__name__)
                    await self._kill()
                    # 循环回到 _spawn：已知 session_id 会自动 --resume 续上下文
            # 重试用尽：抛出，让上层回错误文本给钉钉（不无限重试）
            raise RuntimeError(f"claude 连续 {_MAX_ATTEMPTS} 轮失败：{last_err}")


# ClaudePool 在 Task 5 追加到本文件
```

- [ ] **Step 4: 跑测试，确认通过**

Run: `python -m pytest tests/bridge/test_claude_process.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add bridge/claude_pool.py tests/bridge/test_claude_process.py
git commit -m "feat(bridge): ClaudeProcess 常驻子进程状态机

懒启动+stream-json 双流+单轮超时 kill→--resume 重建重试1次。
同会话串行锁;session_id 从帧捕获,崩溃后上下文可续。"
```

---

## Task 5: claude_pool.py 之 ClaudePool（进程池 + 空闲回收 + /new）

**Files:**
- Modify: `bridge/claude_pool.py`（追加 `ClaudePool` 类）
- Create: `tests/bridge/test_claude_pool.py`

**Interfaces:**
- Consumes: `ClaudeProcess`（Task 4）、`bridge.session_store.SessionStore`（Task 2）
- Produces: `bridge.claude_pool.ClaudePool`，方法：
  - `async ask(conv_id:str, text:str, sender_staff_id:str, on_event=None) -> str`
  - `async reset(conv_id:str) -> None`（`/new`：杀进程 + 清映射）
  - `status() -> list[dict]`（`/status` 用：每会话 alive/session_id/last_active）
  - `async aclose_all() -> None`
  - 后台空闲回收任务：`async start_idle_sweeper() -> asyncio.Task`（按 `idle_ttl` 回收）

- [ ] **Step 1: 写 `tests/bridge/test_claude_pool.py` 失败测试**

```python
# -*- coding: utf-8 -*-
"""ClaudePool 测试：mock ClaudeProcess，验证会话隔离/串行/回收/reset。"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from bridge.claude_pool import ClaudePool
from bridge.session_store import SessionStore


class FakeProc:
    """假的 ClaudeProcess：记录 ask 调用，可控制返回值与串行。"""
    def __init__(self, answer="ok", session_id="sid-x"):
        self._answer = answer
        self.session_id = session_id
        self.is_alive = True
        self.last_active = 0.0
        self.ask = AsyncMock(return_value=answer)
        self.aclose = AsyncMock()


@pytest.mark.asyncio
async def test_same_conversation_reuses_process(tmp_path):
    """同会话两次 ask 复用同一进程（不重复 spawn）。"""
    store = SessionStore(str(tmp_path / "s.json"))
    pool = ClaudePool(cfg=MagicMock(idle_ttl=900, ask_timeout=10), store=store,
                      proc_factory=lambda cfg, sid: FakeProc())
    a1 = await pool.ask("convA", "q1", "staff")
    a2 = await pool.ask("convA", "q2", "staff")
    assert a1 == a2 == "ok"
    proc = pool._procs["convA"]  # 同一对象
    assert proc.ask.call_count == 2
    await pool.aclose_all()


@pytest.mark.asyncio
async def test_different_conversations_isolated(tmp_path):
    """不同会话用不同进程（跨会话隔离，避免上下文串味）。"""
    store = SessionStore(str(tmp_path / "s.json"))
    pool = ClaudePool(cfg=MagicMock(idle_ttl=900, ask_timeout=10), store=store,
                      proc_factory=lambda cfg, sid: FakeProc())
    await pool.ask("convA", "q", "staff")
    await pool.ask("convB", "q", "staff")
    assert len(pool._procs) == 2
    assert pool._procs["convA"] is not pool._procs["convB"]
    await pool.aclose_all()


@pytest.mark.asyncio
async def test_reset_kills_process_and_clears_mapping(tmp_path):
    """/new (reset) 杀进程 + 清 session_store 映射。"""
    path = tmp_path / "s.json"
    store = SessionStore(str(path))
    store.set("convA", "sid-old")
    pool = ClaudePool(cfg=MagicMock(idle_ttl=900, ask_timeout=10), store=store,
                      proc_factory=lambda cfg, sid: FakeProc())
    await pool.ask("convA", "q", "staff")
    await pool.reset("convA")
    assert "convA" not in pool._procs          # 进程已杀
    assert store.get("convA") is None          # 映射已清
    await pool.aclose_all()


@pytest.mark.asyncio
async def test_session_id_persisted_after_ask(tmp_path):
    """ask 后把捕获的 session_id 落 store（进程死后 --resume 可续）。"""
    path = tmp_path / "s.json"
    store = SessionStore(str(path))
    pool = ClaudePool(cfg=MagicMock(idle_ttl=900, ask_timeout=10), store=store,
                      proc_factory=lambda cfg, sid: FakeProc(session_id="sid-caught"))
    await pool.ask("convA", "q", "staff")
    assert store.get("convA") == "sid-caught"
    await pool.aclose_all()


@pytest.mark.asyncio
async def test_idle_sweeper_reclaims_idle_process(tmp_path):
    """空闲超 idle_ttl 的进程被回收（防进程数随历史会话无限增长）。"""
    store = SessionStore(str(tmp_path / "s.json"))
    # idle_ttl=0 → 立即视为空闲可回收
    pool = ClaudePool(cfg=MagicMock(idle_ttl=0, ask_timeout=10), store=store,
                      proc_factory=lambda cfg, sid: FakeProc())
    await pool.ask("convA", "q", "staff")
    assert "convA" in pool._procs
    await pool._sweep_once()   # 手动触发一次扫描
    assert "convA" not in pool._procs
    await pool.aclose_all()
```

- [ ] **Step 2: 跑测试，确认失败**

Run: `python -m pytest tests/bridge/test_claude_pool.py -v`
Expected: FAIL（`ClaudePool` 未定义 / import 错）

- [ ] **Step 3: 在 `bridge/claude_pool.py` 末尾追加 `ClaudePool`**

```python
# ===================== ClaudePool（进程池） =====================
# 追加于本文件末尾。每个 conversationId 懒启动一个 ClaudeProcess，
# 同会话串行（由 ClaudeProcess 内部锁保证）、跨会话并行（不同进程）。
# 空闲超 idle_ttl 的进程主动回收，session_id 仍在 store，下次 --resume 续。

import time  # noqa: E402（放此处避免与文件顶部风格冲突，实际可上提）
from bridge.session_store import SessionStore  # noqa: E402


class ClaudePool:
    """conversationId → ClaudeProcess 的进程池。"""

    # proc_factory 仅测试注入用（生产用默认 ClaudeProcess）
    def __init__(
        self,
        cfg: BridgeConfig,
        store: SessionStore,
        proc_factory: Optional[Callable] = None,
    ) -> None:
        self._cfg = cfg
        self._store = store
        self._procs: dict[str, ClaudeProcess] = {}
        self._last_active: dict[str, float] = {}
        self._proc_factory = proc_factory or (lambda c, sid: ClaudeProcess(c, sid))

    def _get_or_create(self, conv_id: str) -> ClaudeProcess:
        """懒启动：首次取用时创建，已知 session_id 则传入（--resume 续上下文）。"""
        if conv_id not in self._procs:
            sid = self._store.get(conv_id)
            self._procs[conv_id] = self._proc_factory(self._cfg, sid)
        return self._procs[conv_id]

    async def ask(
        self,
        conv_id: str,
        text: str,
        sender_staff_id: str,
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> str:
        """派发一轮问答到对应会话进程；ask 后落盘 session_id。"""
        proc = self._get_or_create(conv_id)
        try:
            answer = await proc.ask(text, on_event=on_event)
        except Exception:
            # 进程挂了：从池中摘除，下次 ask 走重建（已知 sid → --resume）
            self._procs.pop(conv_id, None)
            raise
        self._last_active[conv_id] = time.monotonic()
        # 落盘 session_id：进程死后 --resume 可续（spec §4.1 三级链）
        if proc.session_id:
            self._store.set(conv_id, proc.session_id)
        return answer

    async def reset(self, conv_id: str) -> None:
        """/new：杀该会话进程 + 清映射 → 下次开全新会话。"""
        proc = self._procs.pop(conv_id, None)
        if proc is not None:
            await proc.aclose()
        self._store.clear(conv_id)
        self._last_active.pop(conv_id, None)

    def status(self) -> list[dict]:
        """/status：每会话的 alive / session_id / last_active 快照。"""
        out = []
        for conv_id, proc in self._procs.items():
            out.append({
                "conversation_id": conv_id,
                "alive": proc.is_alive,
                "session_id": proc.session_id,
                "last_active": self._last_active.get(conv_id),
            })
        return out

    async def _sweep_once(self) -> None:
        """扫一次：空闲超 idle_ttl 的进程回收（start_idle_sweeper 周期调用）。"""
        now = time.monotonic()
        stale = [
            cid for cid, t in self._last_active.items()
            if now - t > self._cfg.idle_ttl
        ]
        for cid in stale:
            logger.info("会话 %s 空闲超 %ss，回收进程", cid, self._cfg.idle_ttl)
            await self.reset(cid)

    def start_idle_sweeper(self) -> "asyncio.Task":
        """启动后台空闲回收任务（每 60s 扫一次）。"""
        async def _loop():
            while True:
                await asyncio.sleep(60)
                try:
                    await self._sweep_once()
                except Exception:  # noqa: BLE001
                    logger.exception("idle sweeper 异常")
        return asyncio.create_task(_loop())

    async def aclose_all(self) -> None:
        """桥退出时优雅关闭全部进程。"""
        for cid in list(self._procs.keys()):
            proc = self._procs.pop(cid, None)
            if proc is not None:
                await proc.aclose()
        self._last_active.clear()
```

> 注：把文件顶部 `import time` 上提到其它 stdlib import 处更整洁；上面分段为清晰起见。实现时将 `import time` 合并到文件头。

- [ ] **Step 4: 跑测试，确认通过**

Run: `python -m pytest tests/bridge/test_claude_pool.py -v`
Expected: 5 passed

- [ ] **Step 5: 跑 bridge 全部测试，确认无回归**

Run: `python -m pytest tests/bridge/ -v`
Expected: 所有 task 的测试全 passed

- [ ] **Step 6: Commit**

```bash
git add bridge/claude_pool.py tests/bridge/test_claude_pool.py
git commit -m "feat(bridge): ClaudePool 进程池+空闲回收+/new 重置

每会话懒启动一个 ClaudeProcess,跨会话隔离并行;ask 后 session_id 落盘
(--resume 可续);idle_ttl 后台回收防进程数无限增长。"
```

---

## Task 6: replier.py（长消息分段 + 钉钉 Markdown 清洗）

**Files:**
- Create: `bridge/replier.py`
- Create: `tests/bridge/test_replier.py`

**Interfaces:**
- Consumes: dingtalk-stream handler 的 reply 能力（Task 8 注入）
- Produces:
  - `split_long_text(text:str, limit:int=1800) -> list[str]`：按段落/行边界切段，不硬切单词
  - `clean_markdown_for_dingtalk(text:str) -> str`：剥离钉钉不支持的 Markdown（表格 `|`、`<font>`、``` 代码块保留但去掉语言标记等）
  - `async reply(handler, incoming_msg, text:str) -> None`：清洗 + 分段 + 逐条 reply（`@`回复原消息）

- [ ] **Step 1: 写 `tests/bridge/test_replier.py` 失败测试**

```python
# -*- coding: utf-8 -*-
"""replier 测试：分段边界 + Markdown 清洗 + reply 分多条。"""
from unittest.mock import AsyncMock

import pytest

from bridge.replier import clean_markdown_for_dingtalk, reply, split_long_text


def test_short_text_single_chunk():
    assert split_long_text("hello") == ["hello"]


def test_long_text_split_by_paragraph():
    """超长文本按段落边界切，每段 ≤ limit。"""
    para = "a" * 500
    text = "\n\n".join([para] * 6)   # 3000+ 字符，6 段
    chunks = split_long_text(text, limit=1800)
    assert len(chunks) >= 2
    assert all(len(c) <= 1800 for c in chunks)
    # 内容不丢
    joined = "".join(c for c in chunks)
    for p in [para] * 6:
        assert p in joined


def test_clean_strips_font_tags():
    """<font> 钉钉不支持，剥离但保留内部文本。"""
    out = clean_markdown_for_dingtalk("看 <font color='red'>这个</font>")
    assert "<font" not in out
    assert "这个" in out


def test_clean_strips_table_pipes():
    """表格 | 钉钉不渲染，把整行表格转成普通文本（去 | 留空格）。"""
    out = clean_markdown_for_dingtalk("| a | b |\n|---|---|\n| 1 | 2 |")
    # 表格分隔行（纯 | 和 -）整行删除；数据行 | 替换为空格
    assert "---" not in out


@pytest.mark.asyncio
async def test_reply_splits_into_multiple_sends():
    """超长回复分多条 reply（防钉钉单条 ~20KB 限 + Markdown 渲染截断）。"""
    handler = MagicMock()
    handler.reply_text = AsyncMock()
    incoming = MagicMock()
    text = "x" * 4000
    await reply(handler, incoming, text, limit=1800)
    assert handler.reply_text.call_count >= 3
```

> 注：测试里用 `from unittest.mock import MagicMock`，补 import。

- [ ] **Step 2: 跑测试，确认失败**

Run: `python -m pytest tests/bridge/test_replier.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现 `bridge/replier.py`**

```python
# -*- coding: utf-8 -*-
"""
bridge/replier.py
=================
把 claude 的回复文本发回钉钉：清洗 + 分段 + 逐条 reply（@回复原消息）。

Why 清洗：钉钉群机器人 Markdown 仅支持 #/##/###、**粗**、*斜*、>引用、-列表、
[链接](url)、![图](url)；不支持 <font>、表格 |、---分隔线、复杂代码块。
claude 输出常含这些，直接发会被钉钉渲染成乱码或截断。

Why 分段：钉钉单条消息有长度限制（~20KB，但 Markdown 渲染建议远小于此），
按 1800 字分段 + 段落边界切，避免硬切单词/代码块。
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# 单条默认上限（字符）；留余量给 Markdown 语法开销
_DEFAULT_LIMIT = 1800

# 钉钉不支持的 HTML 标签（剥离标签保留内文）
_FONT_TAG = re.compile(r"<font[^>]*>(.*?)</font>", re.IGNORECASE | re.DOTALL)
# 通用 HTML 标签清理（<br> 转换行，其余剥离）
_OTHER_TAGS = re.compile(r"</?(?!b>|strong>|i>|em>|code>)[a-zA-Z][^>]*>")
# Markdown 表格分隔行（纯 | - : 组成）
_TABLE_SEPARATOR = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$", re.MULTILINE)


def clean_markdown_for_dingtalk(text: str) -> str:
    """剥离钉钉不支持的 Markdown / HTML，保留可渲染部分。"""
    # 1) <font>...</font> → 内文
    text = _FONT_TAG.sub(r"\1", text)
    # 2) <br> → 换行；其它陌生 HTML 标签剥离
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = _OTHER_TAGS.sub("", text)
    # 3) 表格分隔行整行删除（数据行的 | 保留为竖线视觉分隔，钉钉能显示纯文本）
    text = _TABLE_SEPARATOR.sub("", text)
    return text.strip()


def split_long_text(text: str, limit: int = _DEFAULT_LIMIT) -> list[str]:
    """按段落/行边界切，每段 ≤ limit。尽量不硬切单词/代码行。"""
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    # 先按段落（双换行）拆，再按单行拆，累计到 limit 就切
    buf: list[str] = []
    buf_len = 0
    for para in text.split("\n"):
        # 单行本身就超限：硬切兜底（极少见，如超长 base64）
        if len(para) > limit:
            if buf:
                chunks.append("\n".join(buf))
                buf, buf_len = [], 0
            for i in range(0, len(para), limit):
                chunks.append(para[i:i + limit])
            continue
        if buf_len + len(para) + 1 > limit:
            chunks.append("\n".join(buf))
            buf, buf_len = [], 0
        buf.append(para)
        buf_len += len(para) + 1
    if buf:
        chunks.append("\n".join(buf))
    return chunks


async def reply(
    handler: Any,
    incoming_msg: Any,
    text: str,
    limit: int = _DEFAULT_LIMIT,
) -> None:
    """清洗 → 分段 → 逐条 reply（@回复原消息）。投递失败重试 2 次。"""
    cleaned = clean_markdown_for_dingtalk(text)
    chunks = split_long_text(cleaned, limit=limit) or ["(空回复)"]
    for i, chunk in enumerate(chunks):
        # 多段加序号前缀，便于用户看出回答未完
        prefix = f"[{i + 1}/{len(chunks)}] " if len(chunks) > 1 else ""
        payload = prefix + chunk
        for attempt in range(3):
            try:
                # reply_text：dingtalk-stream ChatbotHandler 自带的 @回复方法
                await handler.reply_text(incoming_msg, payload)
                break
            except Exception:  # noqa: BLE001
                if attempt == 2:
                    logger.exception("reply 投递失败（已重试 3 次）：%s", payload[:80])
```

- [ ] **Step 4: 修测试 import（补 MagicMock）**

在 `tests/bridge/test_replier.py` 顶部确保有：
```python
from unittest.mock import AsyncMock, MagicMock
```

- [ ] **Step 5: 跑测试，确认通过**

Run: `python -m pytest tests/bridge/test_replier.py -v`
Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
git add bridge/replier.py tests/bridge/test_replier.py
git commit -m "feat(bridge): replier 长消息分段+钉钉 Markdown 清洗

剥 <font>/表格分隔行等钉钉不支持语法;按段落边界 1800 字分段;
reply 投递重试 3 次,多段加序号前缀。"
```

---

## Task 7: alarmer.py（高危工具调用实时告警）

**Files:**
- Create: `bridge/alarmer.py`
- Create: `tests/bridge/test_alarmer.py`

**Interfaces:**
- Consumes: `core.notifier.NotificationManager.notify_risk_event`（已有）、`core.notifier.fire_and_forget`（已有）
- Produces: `bridge.alarmer.Alarmer`，方法 `check_event(event:dict, sender_staff_id:str) -> None`（同步，内部 fire_and_forget）。订阅 `ClaudeProcess.ask(on_event=...)` 的事件流。

**高危模式**（正则，命中即告警）：
- 路径类：`trading/`、`emt_gateway`、`qmt`、`.env`
- 命令类：`rm `、`git push`、`curl `、`wget `
- 业务类：下单函数名 `place_order`、`insert_order`、`activate_plan`

- [ ] **Step 0: 核对工具调用帧字段（防幻觉）**

Run:
```bash
claude -p "用 Read 工具读一下 README.md 的第一行" --output-format stream-json --verbose --permission-mode bypassPermissions 2>&1 | grep -E '"type":"(assistant|user)"' | grep -i tool | head -3
```
Expected: 看到 assistant 帧 `content` 里有 `{"type":"tool_use","name":"Read","input":{...}}`。以此帧字段为准实现 `_extract_tool_use`。若字段名不同，调整。

- [ ] **Step 1: 写 `tests/bridge/test_alarmer.py` 失败测试**

```python
# -*- coding: utf-8 -*-
"""alarmer 测试：高危模式命中即触发告警（mock notifier，不发真消息）。"""
from unittest.mock import MagicMock, patch

from bridge.alarmer import Alarmer


def _tool_use_event(name: str, inp: dict) -> dict:
    """构造 assistant 帧，content 含一个 tool_use 项。"""
    return {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]},
    }


def test_safe_tool_no_alert():
    """读普通文件不告警。"""
    mgr = MagicMock()
    al = Alarmer(notify=lambda msg, level: mgr(msg, level))
    al.check_event(_tool_use_event("Read", {"file_path": "caisen/w_bottom.py"}),
                   sender_staff_id="staff")
    mgr.assert_not_called()


def test_dangerous_path_triggers_alert():
    """碰 trading/ 路径 → 告警。"""
    mgr = MagicMock()
    al = Alarmer(notify=lambda msg, level: mgr(msg, level))
    al.check_event(
        _tool_use_event("Edit", {"file_path": "trading/emt_gateway.py"}),
        sender_staff_id="staffA",
    )
    mgr.assert_called_once()
    msg = mgr.call_args.args[0]
    assert "trading" in msg
    assert "staffA" in msg


def test_dangerous_bash_command_triggers_alert():
    """Bash 里含 rm → 告警。"""
    mgr = MagicMock()
    al = Alarmer(notify=lambda msg, level: mgr(msg, level))
    al.check_event(
        _tool_use_event("Bash", {"command": "rm -rf data_lake/"}),
        sender_staff_id="staffA",
    )
    mgr.assert_called_once()
    assert "rm" in mgr.call_args.args[0]


def test_non_tool_event_ignored():
    """非工具调用帧（result/thinking）不触发。"""
    mgr = MagicMock()
    al = Alarmer(notify=lambda msg, level: mgr(msg, level))
    al.check_event({"type": "result", "result": "done"}, sender_staff_id="staff")
    al.check_event({"type": "system", "subtype": "thinking_tokens"}, sender_staff_id="staff")
    mgr.assert_not_called()
```

- [ ] **Step 2: 跑测试，确认失败**

Run: `python -m pytest tests/bridge/test_alarmer.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现 `bridge/alarmer.py`**

```python
# -*- coding: utf-8 -*-
"""
bridge/alarmer.py
=================
高危工具调用实时告警（全放行模式的纵深防御③：事中知情）。

订阅 ClaudeProcess.ask 的 on_event 事件流，遇到工具调用（tool_use）且命中
敏感模式时，立即把「谁、调了什么工具、参数」推钉钉告警给用户自己。

Why 事后而非事前：bypassPermissions 下工具自动执行，stream-json 事件是
"正在执行/已完成"的通知，看到时已动手（除非用 SDK hook，本项目刻意不引）。
故本模块只能"事后审计变事中知情"——给用户一个及时的预警窗口，而非拦截。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# 告警投递函数签名：(msg, level) -> Awaitable。默认接 core.notifier。
NotifyFn = Callable[[str, str], object]

# 高危模式（正则；命中任一即告警）。按类分组便于告警文案归类。
_DANGER_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("实盘交易路径", re.compile(r"trading/|emt_gateway|qmt_|xtquant", re.IGNORECASE)),
    ("凭证文件", re.compile(r"\.env\b", re.IGNORECASE)),
    ("破坏性命令", re.compile(r"\brm\b\s|git\s+push|git\s+reset|--force|mkfs|dd\s+if=", re.IGNORECASE)),
    ("网络外传", re.compile(r"\bcurl\b|\bwget\b|scp\b|nc\b", re.IGNORECASE)),
    ("下单函数", re.compile(r"place_order|insert_order|activate_plan|order_place", re.IGNORECASE)),
]


class Alarmer:
    """检查单条 stream-json 事件，命中高危模式即推告警。"""

    def __init__(self, notify: Optional[NotifyFn] = None) -> None:
        # 默认接 core.notifier 的 fire_and_forget + notify_risk_event
        if notify is None:
            notify = _default_notify
        self._notify = notify

    def check_event(self, event: dict, sender_staff_id: str) -> None:
        """检查一个事件。命中高危即异步推告警（不阻塞 claude 主流程）。"""
        tool_name, tool_input = _extract_tool_use(event)
        if tool_name is None:
            return  # 非工具调用帧，忽略
        # 把工具参数序列化为文本供模式匹配（命令/路径都在 input 里）
        blob = f"{tool_name} " + json.dumps(tool_input or {}, ensure_ascii=False)
        for label, pattern in _DANGER_PATTERNS:
            if pattern.search(blob):
                self._fire(sender_staff_id, tool_name, tool_input, label, blob)
                return  # 一个工具调用告一次即可，不重复

    def _fire(
        self,
        sender: str,
        tool: str,
        tool_input: dict,
        label: str,
        blob: str,
    ) -> None:
        msg = (
            f"⚠️ 钉钉桥检测到 claude 执行高危操作\n"
            f"触发者: {sender}\n"
            f"风险类: {label}\n"
            f"工具: {tool}\n"
            f"参数: {blob[:300]}"
        )
        try:
            # notify 默认走 fire_and_forget，本身不阻塞
            self._notify(msg, "WARN")
        except Exception:  # noqa: BLE001
            logger.exception("高危告警投递失败")


def _extract_tool_use(event: dict) -> tuple[Optional[str], Optional[dict]]:
    """从 assistant 帧取首个 tool_use 项的 (name, input)。无则 (None, None)。

    字段以 Task 7 Step 0 实测为准：content 数组里 {"type":"tool_use","name":..,"input":..}。
    """
    content = event.get("message", {}).get("content", [])
    if not isinstance(content, list):
        return None, None
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            return block.get("name"), block.get("input")
    return None, None


def _default_notify(msg: str, level: str) -> None:
    """默认投递：复用 core.notifier 的 fire_and_forget + notify_risk_event。"""
    try:
        from core.notifier import fire_and_forget, NotificationManager
        mgr = NotificationManager.get_default()
        fire_and_forget(mgr.notify_risk_event(msg, level=level))  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        logger.exception("notifier 装配失败，告警丢失")
```

- [ ] **Step 4: 跑测试，确认通过**

Run: `python -m pytest tests/bridge/test_alarmer.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add bridge/alarmer.py tests/bridge/test_alarmer.py
git commit -m "feat(bridge): alarmer 高危工具调用实时告警

订阅 stream-json 事件流,tool_use 命中敏感模式(trading/.env/rm/下单函数等)
即复用 core.notifier 推钉钉告警。全放行对价=事前不拦,事后变事中知情。"
```

---

## Task 8: stream_client.py（dingtalk-stream 装配 + 派发 + 审计）

**Files:**
- Create: `bridge/stream_client.py`
- Create: `tests/bridge/test_stream_client.py`

**Interfaces:**
- Consumes: `safety`、`ClaudePool`、`replier`、`Alarmer`、`SessionStore`、`BridgeConfig`
- Produces: `bridge.stream_client.BridgeHandler`（继承 `dingtalk_stream.ChatbotHandler`）、`build_and_run(cfg) -> None`（阻塞主循环）。

**职责**：`process(callback)` 回调里 → 解析 `ChatbotMessage` → `safety.classify` → 立即 ACK → 按裁决分发（reject 静默审计 / command 执行指令 / allow 异步派发 pool + alarmer + 审计 + reply）。

- [ ] **Step 0: 核对 dingtalk-stream SDK 真实接口（防幻觉）**

Run:
```bash
pip install "dingtalk-stream>=0.20.0"
python -c "import dingtalk_stream, inspect; print([n for n in dir(dingtalk_stream) if not n.startswith('_')])"
python -c "import dingtalk_stream, inspect; print(inspect.signature(dingtalk_stream.ChatbotHandler.process))"
```
Expected: 看到 `Credential`、`DingTalkStreamClient`、`ChatbotHandler`、`ChatbotMessage`、`AckMessage`；`ChatbotMessage` 字段含 `text.content`/`sender_staff_id`(或 `sender_id`)/`conversation_id`/`msg_id`。**以此实际字段为准**调整下面代码里的字段名（`sender_staff_id` 占位，若实际为 `sender_id` 则全局替换）。

- [ ] **Step 1: 写 `tests/bridge/test_stream_client.py` 失败测试**

```python
# -*- coding: utf-8 -*-
"""stream_client 派发测试：mock dingtalk-stream 的 ChatbotMessage，不连真钉钉。

验证：白名单 allow → 异步派发 pool + reply；reject → 静默（不 reply）；
command → 执行指令回复；审计 jsonl 落盘。
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from bridge.config import BridgeConfig
from bridge.stream_client import BridgeHandler


def _cfg(tmp_path):
    return BridgeConfig(
        app_key="k", app_secret="s", allowed_staff_ids=frozenset({"staffOK"}),
        claude_bin="claude", workdir=str(tmp_path), ask_timeout=10,
        idle_ttl=900, rate_limit_per_min=10,
        session_store_path=str(tmp_path / "s.json"),
        audit_log_path=str(tmp_path / "a.jsonl"), log_path=str(tmp_path / "l.log"),
    )


def _make_msg(text: str, staff_id: str = "staffOK", conv_id: str = "convA"):
    """造一个假的 ChatbotMessage（只用到用到的字段）。"""
    m = MagicMock()
    m.text = MagicMock(content=text)          # 钉钉 SDK: msg.text.content
    m.sender_staff_id = staff_id
    m.conversation_id = conv_id
    m.msg_id = "mid-1"
    return m


@pytest.mark.asyncio
async def test_allow_dispatches_to_pool_and_replies(tmp_path):
    """白名单 allow：pool.ask 结果经 reply 回钉钉。"""
    cfg = _cfg(tmp_path)
    pool = MagicMock()
    pool.ask = AsyncMock(return_value="claude 的回答")
    reply_fn = AsyncMock()              # 注入 mock reply，断言它被调用且带回答
    handler = BridgeHandler(cfg=cfg, pool=pool, alarmer=MagicMock(),
                            reply_fn=reply_fn)

    msg = _make_msg("解释颈线")
    await handler._dispatch(msg)   # 直接调内部派发（跳过 SDK ACK 细节）

    pool.ask.assert_awaited_once()
    # reply_fn 被调用，第 3 个位置参数(answer)含 claude 回答
    reply_fn.assert_awaited()
    assert "claude 的回答" in reply_fn.call_args.args[2]


@pytest.mark.asyncio
async def test_reject_silent_no_reply_no_pool(tmp_path):
    """非白名单 reject：不调 pool、不 reply（静默丢弃 + 审计）。"""
    cfg = _cfg(tmp_path)
    pool = MagicMock(); pool.ask = AsyncMock()
    reply_fn = AsyncMock()
    handler = BridgeHandler(cfg=cfg, pool=pool, alarmer=MagicMock(),
                            reply_fn=reply_fn)

    msg = _make_msg("hi", staff_id="intruder")
    await handler._dispatch(msg)

    pool.ask.assert_not_awaited()
    reply_fn.assert_not_awaited()        # 静默：不回执


@pytest.mark.asyncio
async def test_command_new_resets_session(tmp_path):
    """/new 指令：调 pool.reset + 回执，不喂 claude。"""
    cfg = _cfg(tmp_path)
    pool = MagicMock(); pool.reset = AsyncMock(); pool.ask = AsyncMock()
    reply_fn = AsyncMock()
    handler = BridgeHandler(cfg=cfg, pool=pool, alarmer=MagicMock(),
                            reply_fn=reply_fn)

    await handler._dispatch(_make_msg("/new"))
    pool.reset.assert_awaited_once_with("convA")
    pool.ask.assert_not_awaited()
    reply_fn.assert_awaited()            # 回执"会话已重置"


@pytest.mark.asyncio
async def test_audit_log_written(tmp_path):
    """每条消息落审计 jsonl（全放行模式事后追溯底线）。"""
    cfg = _cfg(tmp_path)
    pool = MagicMock(); pool.ask = AsyncMock(return_value="ans")
    handler = BridgeHandler(cfg=cfg, pool=pool, alarmer=MagicMock(),
                            reply_fn=AsyncMock())
    await handler._dispatch(_make_msg("hi"))

    lines = cfg.audit_log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["sender_staff_id"] == "staffOK"
    assert rec["conversation_id"] == "convA"
    assert rec["text"] == "hi"
    assert rec["action"] == "allow"
```

- [ ] **Step 2: 跑测试，确认失败**

Run: `python -m pytest tests/bridge/test_stream_client.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现 `bridge/stream_client.py`**

```python
# -*- coding: utf-8 -*-
"""
bridge/stream_client.py
=======================
dingtalk-stream 装配 + 消息派发 + 审计。

BridgeHandler.process 是钉钉消息入口：
  1. 解析 ChatbotMessage（text / sender_staff_id / conversation_id）
  2. safety.classify 裁决
  3. 立即 ACK（return AckMessage.STATUS_OK）—— 防钉钉重投
  4. 重活（claude）走 asyncio.create_task 异步派发，不阻塞 SDK 主循环

派发分支：
  reject  → 静默（+ 审计，不回执防探测）
  command → 执行 /new /status /help（+ 审计 + 回执）
  allow   → pool.ask（挂 alarmer 监听事件）→ reply 分段回复（+ 审计）

频控：单用户 60s 内 > rate_limit_per_min 条 → 回"太快了"，防刷 + 钉钉频控。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict, deque
from typing import Any, Callable, Optional

from bridge.alarmer import Alarmer
from bridge.claude_pool import ClaudePool
from bridge.config import BridgeConfig
from bridge.replier import reply as reply_text_chunks
from bridge.safety import classify

logger = logging.getLogger(__name__)

# dingtalk-stream SDK（Task 8 Step 0 已核对真实接口）
import dingtalk_stream
from dingtalk_stream import AckMessage, ChatbotMessage


class BridgeHandler(dingtalk_stream.ChatbotHandler):
    """钉钉消息 → 安全闸 → 派发。"""

    def __init__(
        self,
        cfg: BridgeConfig,
        pool: ClaudePool,
        alarmer: Alarmer,
        reply_fn: Optional[Callable] = None,
    ) -> None:
        super().__init__()
        self._cfg = cfg
        self._pool = pool
        self._alarmer = alarmer
        # reply_fn 可注入（测试）；默认用 replier.reply_text_chunks
        self._reply_fn = reply_fn or reply_text_chunks
        # 频控：sender_staff_id → 最近时间戳队列
        self._rate: dict[str, deque[float]] = defaultdict(deque)

    # ---- SDK 入口 ----
    async def process(self, callback):  # type: ignore[override]
        """ChatbotMessage 回调。立即 ACK + 异步派发（不阻塞 SDK）。"""
        try:
            msg = ChatbotMessage.from_dict(callback.data)
        except Exception:  # noqa: BLE001
            logger.exception("ChatbotMessage 解析失败，丢弃")
            return AckMessage.STATUS_OK, "ok"
        # 立即 ACK：重活异步化，防钉钉等不到 ACK 重投
        asyncio.create_task(self._safe_dispatch(msg))
        return AckMessage.STATUS_OK, "ok"

    async def _safe_dispatch(self, msg: Any) -> None:
        """派发包装：任何异常都不外泄（避免 asyncio 吞 traceback）。"""
        try:
            await self._dispatch(msg)
        except Exception:  # noqa: BLE001
            logger.exception("派发异常")

    async def _dispatch(self, msg: Any) -> None:
        """实际派发逻辑（测试直接调本方法，跳过 SDK ACK 细节）。"""
        # 钉钉 SDK 字段：text.content / sender_staff_id / conversation_id
        # （Task 8 Step 0 已核对；若实际字段名不同在此调整）
        text = (getattr(msg.text, "content", "") or "").strip()
        # 去掉 @机器人 前缀（钉钉 @ 消息 content 里可能含 @ 名）
        sender = getattr(msg, "sender_staff_id", "") or getattr(msg, "sender_id", "")
        conv_id = getattr(msg, "conversation_id", "") or "unknown"

        verdict = classify(sender, text, self._cfg)
        self._audit(msg, text, sender, conv_id, verdict.action)

        if verdict.action == "reject":
            # 静默：不回执（防探测者确认机器人存活），仅审计
            logger.info("拒绝非白名单消息：sender=%s", sender)
            return

        if verdict.action == "command":
            await self._handle_command(msg, conv_id, verdict.command)
            return

        # allow：频控 → 派发 claude
        if not self._rate_allow(sender):
            await self._reply_fn(self, msg, "太快了，稍候再试 ⏳")
            return
        await self._ask_claude(msg, conv_id, sender, text)

    # ---- 分支实现 ----
    async def _handle_command(self, msg: Any, conv_id: str,
                              command: Optional[str]) -> None:
        if command == "new":
            await self._pool.reset(conv_id)
            await self._reply_fn(self, msg, "✅ 会话已重置（上下文清空，下次开新会话）")
        elif command == "status":
            lines = ["🤖 桥状态："] + [
                f"- {s['conversation_id'][:12]}… alive={s['alive']} "
                f"sid={'有' if s['session_id'] else '无'}"
                for s in self._pool.status()
            ] or ["（无活跃会话）"]
            await self._reply_fn(self, msg, "\n".join(lines) or "（无活跃会话）")
        elif command == "help":
            await self._reply_fn(self, msg, _HELP_TEXT)

    async def _ask_claude(self, msg: Any, conv_id: str,
                          sender: str, text: str) -> None:
        """喂给 pool + 挂 alarmer 监听事件 → reply。"""
        def on_event(event: dict) -> None:
            # 高危工具调用实时告警（全放行纵深防御③）
            self._alarmer.check_event(event, sender_staff_id=sender)

        try:
            answer = await self._pool.ask(conv_id, text, sender, on_event=on_event)
        except Exception as e:  # noqa: BLE001
            logger.exception("claude 处理失败")
            answer = f"⚠️ claude 处理出错：{e}"
        await self._reply_fn(self, msg, answer)

    # ---- 频控 ----
    def _rate_allow(self, sender: str) -> bool:
        """滑窗：60s 内不超过 rate_limit_per_min 条。"""
        now = time.monotonic()
        q = self._rate[sender]
        while q and now - q[0] > 60.0:
            q.popleft()
        if len(q) >= self._cfg.rate_limit_per_min:
            return False
        q.append(now)
        return True

    # ---- 审计 ----
    def _audit(self, msg: Any, text: str, sender: str,
               conv_id: str, action: str) -> None:
        """追加一行 jsonl 到审计日志（全放行模式事后追溯底线）。"""
        import os
        rec = {
            "ts": time.time(),
            "msg_id": getattr(msg, "msg_id", ""),
            "sender_staff_id": sender,
            "conversation_id": conv_id,
            "text": text[:500],   # 截断防巨量日志
            "action": action,
        }
        try:
            os.makedirs(os.path.dirname(self._cfg.audit_log_path) or ".", exist_ok=True)
            with open(self._cfg.audit_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            logger.exception("审计落盘失败")


_HELP_TEXT = (
    "🤖 钉钉→claude 旁路桥\n"
    "- 直接发消息 = 与本机 claude 对话（全放行模式，等同终端）\n"
    "- /new 重置当前会话上下文\n"
    "- /status 查看桥的活跃会话\n"
    "- /help 显示本帮助\n"
    "⚠️ 全放行：claude 可读写文件/跑命令，高危操作会实时告警。"
)


def build_and_run(cfg: BridgeConfig) -> None:
    """装配 Stream 客户端 + Handler 并阻塞运行（入口 __main__ 调用）。"""
    from bridge.session_store import SessionStore

    store = SessionStore(cfg.session_store_path)
    pool = ClaudePool(cfg, store)
    alarmer = Alarmer()

    # 启动后台空闲回收
    sweeper = pool.start_idle_sweeper()

    credential = dingtalk_stream.Credential(cfg.app_key, cfg.app_secret)
    client = dingtalk_stream.DingTalkStreamClient(credential)
    handler = BridgeHandler(cfg=cfg, pool=pool, alarmer=alarmer)
    client.register_callback_handler(ChatbotMessage.TOPIC, handler)

    logger.info("钉钉桥启动，工作目录=%s", cfg.workdir)
    try:
        # start_forever 是 SDK 的阻塞协程
        asyncio.run(client.start_forever())
    finally:
        sweeper.cancel()
        asyncio.run(pool.aclose_all())
```

- [ ] **Step 4: 跑测试，确认通过**

Run: `python -m pytest tests/bridge/test_stream_client.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add bridge/stream_client.py tests/bridge/test_stream_client.py
git commit -m "feat(bridge): stream_client dingtalk-stream 装配+派发+审计

ChatbotHandler.process 立即 ACK+异步派发;classify 三分支(静默拒绝/
指令/allow→pool+alarmer);滑窗频控;全量审计 jsonl。SDK 字段以实测为准。"
```

---

## Task 9: 入口装配 + 手动 E2E + README

**Files:**
- Create: `bridge/__main__.py`
- Create: `scripts/dingtalk_claude_bridge.py`
- Modify: `README.md`（补「钉钉桥」一节）

**Interfaces:**
- Consumes: 全部前置 Task
- Produces: 可执行入口 `python -m bridge` 与 `python scripts/dingtalk_claude_bridge.py`；日志装配。

- [ ] **Step 1: 实现 `bridge/__main__.py`**

```python
# -*- coding: utf-8 -*-
"""
bridge/__main__.py
=================
钉钉桥入口：`python -m bridge`。

职责：load_dotenv → 装配日志 → Windows ProactorEventLoop（subprocess 需要）
→ BridgeConfig.from_env → stream_client.build_and_run。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

# 项目根（bridge/ → 项目根）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _setup_logging(cfg) -> None:
    """装配本地文件 + 控制台日志（复用项目 LOG_CONFIG 风格）。"""
    fmt = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt,
                        handlers=[logging.StreamHandler(sys.stdout)])
    os.makedirs(os.path.dirname(cfg.log_path) or ".", exist_ok=True)
    logging.getLogger().addHandler(
        logging.FileHandler(cfg.log_path, encoding="utf-8")
    )


def main() -> None:
    # .env 加载（凭证隔离红线：绝不硬编码）
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")

    from bridge.config import BridgeConfig
    cfg = BridgeConfig.from_env(project_root=str(PROJECT_ROOT))

    _setup_logging(cfg)
    logger = logging.getLogger("bridge")
    logger.info("钉钉桥配置完成：workdir=%s, whitelist=%d 人, 全放行模式",
                cfg.workdir, len(cfg.allowed_staff_ids))

    # Windows asyncio.subprocess 需 ProactorEventLoop（3.8+ Windows 默认即此；
    # 显式设置防某些环境被改 policy）
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    from bridge.stream_client import build_and_run
    build_and_run(cfg)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 实现 `scripts/dingtalk_claude_bridge.py`（thin 入口）**

```python
#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""钉钉桥 thin 入口（项目 scripts/ 惯例），等价 `python -m bridge`。"""
import sys
from pathlib import Path

# 把项目根加 sys.path，使 `python scripts/dingtalk_claude_bridge.py` 可直接跑
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bridge.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
```

- [ ] **Step 3: 单元冒烟（不连真钉钉，验证装配不 import 错）**

Run:
```bash
python -c "from bridge.stream_client import build_and_run, BridgeHandler; from bridge.config import BridgeConfig; print('import ok')"
DINGTALK_APP_KEY=x DINGTALK_APP_SECRET=y DINGTALK_ALLOWED_STAFF_IDS=z python -c "from bridge.config import BridgeConfig; print(BridgeConfig.from_env(project_root='.').claude_bin)"
```
Expected: `import ok`；第二行打印 `claude`（默认走 PATH）。

- [ ] **Step 4: 跑 bridge 全部测试，最终确认**

Run: `python -m pytest tests/bridge/ -v`
Expected: 全部 passed（config 4 + safety 5 + session_store 5 + claude_events 7 + claude_process 3 + claude_pool 5 + replier 4 + alarmer 4 + stream_client 4 = 41 passed）

- [ ] **Step 5: 手动 E2E（真钉钉回环，不进 CI）**

前置：`.env` 填真值（`DINGTALK_APP_KEY/SECRET`、`DINGTALK_ALLOWED_STAFF_IDS`）；钉钉开放平台机器人已发布 + Stream 权限已开。

Run:
```bash
python -m bridge
```
然后在钉钉 @机器人 发：
1. `hi` → 期望收到 claude 回复。
2. `读一下 caisen/patterns/w_bottom.py 的开头` → 期望 claude 用 Read 工具后回复文件内容；若命中 `caisen` 不告警（非高危），命中 `trading/` 则应收到实时告警。
3. `/status` → 期望收到活跃会话列表。
4. `/new` → 期望"会话已重置"。
5. 连续发 15 条 → 第 11 条起收到"太快了"（频控验证）。

记录结果到本任务评论；失败走 superpowers:systematic-debugging。

- [ ] **Step 6: README 补「钉钉桥」一节**

在 `README.md` 末尾追加：
```markdown
## 钉钉远程驱动 Claude 旁路桥

独立守护进程，用手机钉钉远程驱动本机 `claude`（全放行，等同终端）。

### 配置（`.env`）
```
DINGTALK_APP_KEY=<企业内部应用 Client ID>
DINGTALK_APP_SECRET=<Client Secret>
DINGTALK_ALLOWED_STAFF_IDS=<你的 staffId，逗号分隔>
```
凭证在钉钉开放平台「应用开发 → 企业内部应用 → 凭证与基础信息」获取；机器人需开通 Stream 模式 + 发布。

### 启动
```bash
python -m bridge            # 或 python scripts/dingtalk_claude_bridge.py
```
启动后在钉钉 @机器人 发消息即可。

### 安全须知（全放行模式）
- claude 拥有完整文件读写 + 命令执行能力（等同你在终端每个确认按 y）。
- 仅 `DINGTALK_ALLOWED_STAFF_IDS` 内用户可触发（唯一身份闸）。
- 全量审计：`logs/dingtalk_bridge_audit.jsonl`。
- 高危操作（碰 `trading/`、`.env`、`rm`、下单函数等）实时推钉钉告警。
- 降级：把 `bridge/claude_pool.py` 里 `--permission-mode bypassPermissions` 改 `acceptEdits` 即可禁命令执行。
```

- [ ] **Step 7: Commit**

```bash
git add bridge/__main__.py scripts/dingtalk_claude_bridge.py README.md
git commit -m "feat(bridge): 入口装配+手动 E2E+README

python -m bridge / scripts/dingtalk_claude_bridge.py 双入口;
Windows ProactorEventLoop 显式设置;.env 加载+日志装配。"
```

---

## 完成标准（Definition of Done）

- [ ] `tests/bridge/` 全绿（41 passed），不依赖真钉钉/真 claude。
- [ ] 手动 E2E 5 项全通过（真钉钉回环）。
- [ ] `.env.example` 含全部新配置项（值留空）；`.env` 真值未进 git。
- [ ] `requirements.txt` 仅新增 `dingtalk-stream`。
- [ ] 未改动 `server/`、`trading/`、`caisen/` 任何文件（`git diff master -- server/ trading/ caisen/` 应为空，除既有未提交改动外）。
- [ ] 全中文注释到位（What + Why）。
