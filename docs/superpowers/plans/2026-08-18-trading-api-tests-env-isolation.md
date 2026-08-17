# 实施计划：test_trading_api 9 失败根治——鉴权环境变量隔离补面（QUANTER_API_TOKEN）

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 `tests/test_trading_api.py` 全部 9 个用例在**本机带真实 `.env`** 时恒 401 失败的问题（master `2881d0bc` 既有，2026-08-16 起；无 `.env` 的干净环境不复现）。修复面一行级：既有 autouse 隔离 fixture 补 `QUANTER_API_TOKEN` 一项。不改动任何生产代码。

**Architecture:** 测试进程污染链 = `.env:158 QUANTER_API_TOKEN` → `config/__init__.py:18` 模块级 `load_dotenv()`（import 即触发，tests/conftest.py:147 注释已有定论）→ `presentation/server/http/auth.py::require_write` 读到已配 token → 受保护路由（`presentation/server/main.py:700` trading_router 挂 `require_write`）对无 Bearer 的 TestClient 一律 401（auth.py「token 已配 + Bearer 缺失 → 401」分支，与 live/dry_run 无关——`_isolate_trade_env` 已强制 dry_run，但那只挡住「live fail-closed」分支）。治理沿用既有先例：`tests/conftest.py::_isolate_trade_env`（autouse，治 eod_plan confirmed=True 误判的同款思路）扩面而非在 test_trading_api.py 里逐测试打补丁——污染是全局的，隔离也必须是全局的。

**Tech Stack:** pytest monkeypatch（fixture 内 delenv）、FastAPI TestClient。零新增依赖、零生产代码改动。

## Global Constraints

- **只动 tests/conftest.py 一个文件**；生产代码（config/auth/main）零改动。
- **不破坏显式鉴权用例**：`tests/test_auth.py`、`tests/server/test_auth_read_cookie.py`、`tests/server/test_auth_fail_closed.py`、`tests/server/test_logs_sse_auth.py`、`tests/trading/test_main.py` 均在测试函数内显式 `monkeypatch.setenv("QUANTER_API_TOKEN", ...)`——同一 monkeypatch 实例后序生效，覆盖 autouse 默认（conftest.py:154-155 注释已声明该语义，本计划依赖它）。
- 全中文注释，Why 像素级（沿用该 fixture 现有注释块风格扩写）。
- 分支 `fix/test-env-token-isolation-0818`（自 master 2881d0bc 切出，与 `debt/compute-unit-retirement-0818` 互不依赖、可独立合入）；file:line 基准 2026-08-18，实施前以符号名 re-verify。

## 诊断证据（2026-08-18 实证，复现于 master 与退役分支）

1. `python -m pytest tests/test_trading_api.py -q` → 9 failed：6 个 `assert 401 == 200/503`（status/submit_order×2/orders+asset/connect/jobs_default_date）+ 3 个 `KeyError: 'catchup'/'date'`（jobs 系列对 401 body `{"detail":...}` 取键——同一根因的两种断言形态）。
2. 本机 `.env` 第 124 行 `AUTO_TRADE_MODE`、第 158 行 `QUANTER_API_TOKEN` 均已设置（token 注释标注「2026-08-16 AI 代生成」——失败起点与之吻合）。
3. 污染路径实测：`python -c "import presentation.server.main"` 后 `os.environ` 出现 `QUANTER_API_TOKEN`（shell 原生环境无此变量；`config/__init__.py:18` 模块级 `load_dotenv()` 注入）。
4. 隔离缺口实测：`_isolate_trade_env` 已 setenv `AUTO_TRADE_MODE=dry_run`（挡住 live fail-closed 分支）但未 delenv `QUANTER_API_TOKEN`（token 已配分支仍拒）。

## File Structure

| 文件 | 职责 | 改动 |
|---|---|---|
| `tests/conftest.py` | 全局测试隔离 | `_isolate_trade_env` fixture 补一行 delenv + 注释块扩写（Task 1） |

## Tasks

### Task 1：`_isolate_trade_env` 扩面 QUANTER_API_TOKEN

- [ ] `tests/conftest.py` `_isolate_trade_env`（现 ~L157-160）体内追加：

```python
    monkeypatch.delenv("QUANTER_API_TOKEN", raising=False)
```

- [ ] 同步扩写该 fixture 头部注释块：标题从「治 eod_plan confirmed=True 误判」扩为兼治「test_trading_api 恒 401」；Why 补三行——`.env` 08-16 起配置 `QUANTER_API_TOKEN` → require_write「token 已配 + 无 Bearer → 401」分支拒 TestClient；本 delenv 强制测试态「token 未配置 + dry_run → 放行 + WARNING」（auth.py 开发态语义）；显式 setenv 同名变量的鉴权用例后序覆盖不受影响。
- [ ] **验证门 A（对症）**：`python -m pytest tests/test_trading_api.py -q` → 9 failed → **9 passed**。
- [ ] **验证门 B（不伤邻）**：`python -m pytest tests/test_auth.py tests/server/test_auth_read_cookie.py tests/server/test_auth_fail_closed.py tests/server/test_logs_sse_auth.py tests/trading/test_main.py -q` → 全绿（显式 token 用例语义不变）。
- [ ] **验证门 C（全量）**：`python -m pytest tests/ -q` → 期望 **2013 passed / 0 failed / 1 skipped**（基线：修复前 2004 passed / 9 failed / 1 skipped，2026-08-18 master 实测）。
- [ ] 提交：`fix(tests): conftest 隔离扩面 QUANTER_API_TOKEN——治 test_trading_api 9 用例本机带 .env 恒 401（config 模块级 load_dotenv 污染链，require_write 已配 token 分支拒无 Bearer TestClient）；显式 setenv 鉴权用例不受影响`

## 明确不做（Out of Scope，候选后续工单）

- **`config/__init__.py` 模块级 `load_dotenv()` 的结构性根治**（env 注入收敛到入口点，如 trading/__main__ 已自带 `load_dotenv(override=True)`）：这是污染链的真根，但 broadcast/钉钉桥等消费方注释声明「同读 .env 自动对齐」，改动波及生产语义，须独立设计裁决（候选：技术债总纲或 06-tech-debt 登记），不混入本一行修复。
- test_trading_api.py 逐用例加 Bearer header 的写法：掩住「测试不该继承开发 .env」的原则，且给未来每个新 API 用例埋同类雷——已被 conftest 级隔离方案否决。
- 9 用例断言本身（401/KeyError 形态分裂）不改写：它们当前锁定的是各自路由契约，形态差异只是失败表象。

## 验收口径

带真实 `.env` 的本机跑全量套件 0 失败即收官；无 `.env` 环境行为不变（delenv 对不存在的变量是 no-op）。
