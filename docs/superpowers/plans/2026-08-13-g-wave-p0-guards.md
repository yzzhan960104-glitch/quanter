# G 波（保护链 P0-Guards）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 2026-08-13 全项目审计发现的 7 项保护链静默失效（CI 断链 / 鉴权 fail-open / 熔断 fail-open / SDK 无超时 / 非原子写 / sqlite 无 WAL / 告警盲区+FSM 漂移），使其全部转为 fail-closed 或可观测。

**Architecture:** 7 个独立工单 G1-G7，同分支 `fix/p0-guards`（物理隔离 `opt/p1-vectorization`）。TDD 驱动（先写失败测试 → 最小实现 → 绿 → commit），配置类工单（G1 CI）用「改 → 验证 CI 绿」替代纯 TDD。每工单独立 commit + 独立验收门，可随时暂停/revert。

**Tech Stack:** Python 3.10 / pytest / FastAPI / sqlite3 (WAL) / APScheduler / GitHub Actions / uvicorn

## Global Constraints

- **全中文 + 像素级注释（含 Why）**——CLAUDE.md 强制；每处改动注释说明物理意图。
- **Karpathy 极简**：零新增依赖（本波不引新包，pyarrow/optuna/yfinance 是 requirements.txt 已有，仅本机补装）。
- **行为等价红线**：G3 熔断改 fail-closed 是**语义变更**（非 bugfix），commit message 须 ADR 式说明「为何从 fail-open 翻转」；其余工单不改已测路径等价语义。
- **渐进式**：每 Task 独立 commit，可暂停可 revert。
- **分支 `fix/p0-guards`**，与 `opt/p1-vectorization` 物理隔离；恢复执行前 `git status` 核对。
- **基准 HEAD `47561049`**（2026-08-13）；所有 file:line 以此为准，改动前先读确认签名未漂移。
- **TDD 顺序**：能写 pytest 的工单一律「红 → 绿」；改完跑 `python ops/run_checks.py` 5 gate 不退化（G1 修复后此命令可跑）。

---

### Task G1: CI 复活（前置 N1：本机补依赖）

**Files:**
- Modify: `.github/workflows/ci.yml:43,51,54`
- Modify: `.github/workflows/ci.yml`（新增自检 step）
- Read-only verify: `ops/run_checks.py`、`requirements.txt`

**Interfaces:**
- Consumes: N1 根因（.venv310 缺 pyarrow/optuna/yfinance）
- Produces: push/PR 的 4 gate（端口/契约/单测/类型）实跑生效——后续所有 Task 的回归网。

- [ ] **Step 1: 本机补装缺失依赖（N1 前置）**

Run: `.venv310/Scripts/python.exe -m pip install -r requirements.txt`
（确认 pyarrow/optuna/yfinance 装入 .venv310）

- [ ] **Step 2: 验证本机 run_checks 当前红**

Run: `.venv310/Scripts/python.exe ops/run_checks.py`
Expected: gate③（后端单测）因 `pytest --collect-only` 14 文件 ModuleNotFoundError 而红——确认这就是「CI 复活即红」的根因。

- [ ] **Step 3: 补依赖后验证本机 run_checks 转绿**

Run: `.venv310/Scripts/python.exe ops/run_checks.py`
Expected: 5 gate 全绿。若仍有红，先修红（非本 Task 范围则记录）。

- [ ] **Step 4: 改 ci.yml 前端 cache 路径**

Modify `.github/workflows/ci.yml:43`：
```yaml
# 旧
cache-dependency-path: web/package-lock.json
# 新
cache-dependency-path: presentation/web/package-lock.json
```

- [ ] **Step 5: 改 ci.yml npm ci 前缀**

Modify `.github/workflows/ci.yml:51`：
```yaml
# 旧
run: npm ci --prefix web
# 新
run: npm ci --prefix presentation/web
```

- [ ] **Step 6: 改 ci.yml run_checks 路径**

Modify `.github/workflows/ci.yml:53-54`（name 与 run 两处）：
```yaml
# 旧
- name: 全栈护栏（与本地 python scripts/run_checks.py 同源）
  run: python scripts/run_checks.py
# 新
- name: 全栈护栏（与本地 python ops/run_checks.py 同源）
  run: python ops/run_checks.py
```

- [ ] **Step 7: 加自检 step 防路径再次漂移**

在 `run: python ops/run_checks.py` 前插入新 step：
```yaml
      - name: 自检·run_checks 路径未漂移
        run: |
          test ! -f scripts/run_checks.py || { echo "ci.yml 应指向 ops/run_checks.py，但 scripts/ 仍存在残留"; exit 1; }
          test -f ops/run_checks.py || { echo "ops/run_checks.py 不存在"; exit 1; }
```

- [ ] **Step 8: 同步 ci.yml 顶部注释**

Modify `.github/workflows/ci.yml:3`：注释里 `scripts/run_checks.py` → `ops/run_checks.py`。

- [ ] **Step 9: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "fix(ci): 复活保护链——前端路径 web→presentation/web + run_checks scripts→ops（自 2c49ee57 迁移起断链）

N1 根因：本机 .venv310 缺 pyarrow/optuna/yfinance 致 gate③ 收集红；
CI 路径双错（web/、scripts/）自 2026-07-25 起未跑过。加自检 step 防漂移。"
```

- [ ] **Step 10: push 验证 CI 实跑绿**

Push `fix/p0-guards` → GitHub Actions 实跑 4 gate → 全绿。若红，读日志定位（不再可能是路径错）。

---

### Task G2: 鉴权 fail-closed + SSE cookie 鉴权

**Files:**
- Modify: `presentation/server/http/auth.py:39-79`（`_configured_token` / `require_write`）
- Modify: `presentation/server/main.py:644`（logs_router 挂鉴权）、`:674`（/health 去 version）、`run_server` host 默认
- Modify: `presentation/server/api/v1/logs.py:164`（SSE 读 cookie）
- Test: `tests/server/test_auth_fail_closed.py`（新建）、`tests/server/test_logs_sse_auth.py`（新建）
- Modify: `scripts/start_server.bat`（live 无 token 醒目报错）

**Interfaces:**
- Consumes: `AUTO_TRADE_MODE` env（判 live/dry_run）
- Produces: `require_write` fail-closed 语义；SSE cookie 鉴权契约 `Cookie: quanter_ro=<token>`。

- [ ] **Step 1: 读现状确认签名**

Read `presentation/server/http/auth.py` 全文，确认 `_configured_token`、`require_write`、IP 白名单结构与 `QUANTER_API_TOKEN`/`AUTO_TRADE_MODE` 读取点。

- [ ] **Step 2: 写失败测试——live 无 token 拒绝**

```python
# tests/server/test_auth_fail_closed.py
import pytest
from presentation.server.http.auth import require_write

class _FakeReq:
    def __init__(self, headers=None, client_host="127.0.0.1"):
        self.headers = headers or {}
        self.client = type("C", (), {"host": client_host})()

def test_live_mode_no_token_rejected(monkeypatch):
    """live 模式 token 未配 → fail-closed（返 401，不放行）。DG-G3 同源铁律。"""
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    monkeypatch.delenv("QUANTER_API_TOKEN", raising=False)
    with pytest.raises(Exception):  # HTTPException(401)
        require_write(_FakeReq())

def test_dry_run_no_token_allowed(monkeypatch):
    """dry_run 模式允许无 token（开发态不阻断）。"""
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
    monkeypatch.delenv("QUANTER_API_TOKEN", raising=False)
    assert require_write(_FakeReq()) is None  # 放行
```

- [ ] **Step 3: 运行测试确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/server/test_auth_fail_closed.py -v`
Expected: `test_live_mode_no_token_rejected` FAIL（当前 require_write 对无 token 一律放行）。

- [ ] **Step 4: 改 require_write 为 fail-closed**

Modify `presentation/server/http/auth.py`（`require_write` 内，原 `if not tok: ... return None` 分支）：
```python
tok = _configured_token()
if not tok:
    # DG-G2 fail-closed：live 模式无 token = 拒绝（防默认裸奔）；
    # dry_run 模式允许（开发/CI 不阻断）。
    if os.getenv("AUTO_TRADE_MODE", "dry_run") == "live":
        raise HTTPException(status_code=401, detail="live 模式必须配置 QUANTER_API_TOKEN")
    return None  # dry_run 放行
```

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/server/test_auth_fail_closed.py -v`
Expected: 2 PASS。

- [ ] **Step 6: 写失败测试——SSE 无 cookie 拒绝**

```python
# tests/server/test_logs_sse_auth.py
import pytest
from fastapi.testclient import TestClient

def test_logs_stream_without_cookie_rejected(monkeypatch):
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    monkeypatch.setenv("QUANTER_API_TOKEN", "secret")
    from presentation.server.main import app
    client = TestClient(app)
    r = client.get("/api/v1/logs/stream")
    assert r.status_code == 401

def test_logs_stream_with_cookie_accepted(monkeypatch):
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    monkeypatch.setenv("QUANTER_API_TOKEN", "secret")
    from presentation.server.main import app
    client = TestClient(app)
    r = client.get("/api/v1/logs/stream", cookies={"quanter_ro": "secret"})
    assert r.status_code != 401  # 鉴权通过（连接成功或正常业务态）
```

- [ ] **Step 7: 运行确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/server/test_logs_sse_auth.py -v`
Expected: 无 cookie 也 200（当前 logs_router 无鉴权）。

- [ ] **Step 8: 给 logs_router 挂 cookie 只读鉴权**

Modify `presentation/server/main.py:644`（logs_router include 处），加一个 cookie 依赖（EventSource 无法加 header，故走 cookie 同源自动携带）：
```python
async def _require_read_cookie(request: Request):
    """SSE 只读鉴权：EventSource 不能加 header，走 cookie 同源携带。
    token 与 require_write 同源（QUANTER_API_TOKEN）；live 模式无 cookie 拒绝。"""
    tok = os.getenv("QUANTER_API_TOKEN")
    if not tok:
        if os.getenv("AUTO_TRADE_MODE", "dry_run") == "live":
            raise HTTPException(401, "live 模式日志流需 cookie 鉴权")
        return None
    if request.cookies.get("quanter_ro") != tok:
        raise HTTPException(401, "日志流 cookie 无效")
    return None

app.include_router(logs_router, prefix="/api/v1", dependencies=[Depends(_require_read_cookie)])
```

- [ ] **Step 9: 运行确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/server/test_logs_sse_auth.py -v`
Expected: 2 PASS。

- [ ] **Step 10: 默认 host 改 127.0.0.1 + /health 去 version**

Modify `presentation/server/main.py` 的 `run_server`：`host` 默认 `0.0.0.0` → `127.0.0.1`（外网需显式 `SERVER_HOST=0.0.0.0`）。
Modify `/health` 端点：返回去掉 `version` 字段（仅 `{status: "ok"}`）防指纹。

- [ ] **Step 11: start_server.bat live 无 token 醒目报错**

Read `scripts/start_server.bat`，在启动前加检测：
```bat
REM DG-G2: live 模式必须配 QUANTER_API_TOKEN，否则 fail-closed 拒起
if "%AUTO_TRADE_MODE%"=="live" if "%QUANTER_API_TOKEN%"=="" (
    echo [FATAL] live 模式未配 QUANTER_API_TOKEN，鉴权将 fail-closed 拒起。请配置后重试。
    exit /b 1
)
```

- [ ] **Step 12: 全量 run_checks + commit**

Run: `.venv310/Scripts/python.exe ops/run_checks.py` → 5 gate 绿。
```bash
git add presentation/server/http/auth.py presentation/server/main.py scripts/start_server.bat tests/server/test_auth_fail_closed.py tests/server/test_logs_sse_auth.py
git commit -m "fix(security): 鉴权 fail-closed + SSE cookie 鉴权 + 默认 host 127.0.0.1（DG-G2）

require_write live 无 token 拒绝；/logs/stream cookie 鉴权（EventSource 不能加 header）；
/health 去版本防指纹。cookie 方案避免 token 进 access log。"
```

---

### Task G3: 熔断 fail-closed + account_daily 基线兜底

**Files:**
- Modify: `trading/compute/breaker.py:50`（`check_daily_loss_limit` 基线 NULL 分支）
- Modify: `trading/phases/pre_open.py`（start 基线抓取 + T-1 兜底写入 account_daily）
- Test: `tests/trading/test_breaker_fail_closed.py`（新建）、`tests/trading/test_baseline_backfill.py`（新建）

**Interfaces:**
- Consumes: `_mode()`（判 live/dry_run）、`_CriticalHalt`、`_alert_critical`、`state_store.snapshot/get_start_equity`
- Produces: 基线缺失时 fail-closed（模拟盘停手+告警 / 实盘 halt）契约。

- [ ] **Step 1: 读 breaker 与基线抓取现状**

Read `trading/compute/breaker.py:40-70`（`check_daily_loss_limit` 全签名 + `if start_equity <= 0: return False`）。
Read `trading/phases/pre_open.py` 的 start_total_asset 抓取段（grep `start_total_asset` / `account_daily`）。

- [ ] **Step 2: 写失败测试——基线 NULL fail-closed**

```python
# tests/trading/test_breaker_fail_closed.py
import pytest
from trading.compute import breaker

def test_live_baseline_null_raises_halt(monkeypatch):
    """实盘基线 NULL → _CriticalHalt（不停手 = 单日回撤无上限 = 致命）。DG-G3。"""
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    # 基线缺失：start_equity 为 None/0
    with pytest.raises(breaker._CriticalHalt if hasattr(breaker, "_CriticalHalt") else Exception):
        breaker.check_daily_loss_limit(start_equity=0.0, current_equity=100.0)

def test_dry_run_baseline_null_halt_and_alert(monkeypatch, caplog):
    """模拟盘基线 NULL → 停手 + CRITICAL 告警（不真 halt 进程）。"""
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
    triggered = breaker.check_daily_loss_limit(start_equity=0.0, current_equity=100.0)
    # 模拟盘：返回「应停手」语义 + 告警
    assert triggered is True or "基线" in caplog.text
```

- [ ] **Step 3: 运行确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_breaker_fail_closed.py -v`
Expected: FAIL（当前 `start_equity<=0 → return False` 放行）。

- [ ] **Step 4: 改 breaker 为 fail-closed**

Modify `trading/compute/breaker.py:50`：
```python
if start_equity is None or start_equity <= 0:
    # DG-G3 fail-closed：基线缺失（account_daily 漏采）绝不放行。
    # 模拟盘：触发停手 + CRITICAL 告警；实盘：raise _CriticalHalt（L1 停调度）。
    _alert_critical("熔断基线缺失（account_daily.start NULL），fail-closed 停手")
    if _mode() == "live":
        raise _CriticalHalt("熔断基线缺失，live 拒绝继续下注")
    return True  # 模拟盘停手（current_equity 视为已破阈值）
```
（`_alert_critical`/`_CriticalHalt`/`_mode` 从 trading.critical import；若 breaker 未 import，补顶部 import 并像素级注释 Why。）

- [ ] **Step 5: 运行确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_breaker_fail_closed.py -v`
Expected: 2 PASS。

- [ ] **Step 6: 写失败测试——基线 T-1 兜底回填**

```python
# tests/trading/test_baseline_backfill.py
def test_pre_open_backfills_missing_baseline(monkeypatch, tmp_path):
    """pre_open 抓 start 基线时 account_daily 无当日 → 用 T-1 收盘快照回填，
    避免 fail-closed 退化成「每天开盘熔断」。"""
    # 造 T-1 account_daily 有 end_total_asset，当日无 → 回填后 get_start_equity 非 NULL
    ...  # 用 state_store fixture 造数据（参考 tests/trading/test_state_store.py 范式）
    from trading.phases import pre_open
    # 断言回填后 start_equity 来自 T-1 end
```

- [ ] **Step 7: 运行确认失败 → 改 pre_open 基线抓取加 T-1 兜底**

Read `trading/phases/pre_open.py` 抓 start 基线段，在「当日 account_daily 无 start」分支加：
```python
# T-1 兜底：当日漏采 → 用最近已收盘日的 end_total_asset 作为 start 基线回填。
# Why：account_daily.start 漏采（非盘前启动）会让 G3 fail-closed 每天误熔断。
```
（精确实现：查 `SELECT end_total_asset FROM account_daily WHERE trade_date < today ORDER BY trade_date DESC LIMIT 1`，写入当日 start。）

- [ ] **Step 8: 运行确认通过 + run_checks + commit**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_breaker_fail_closed.py tests/trading/test_baseline_backfill.py -v` → PASS。
Run: `.venv310/Scripts/python.exe ops/run_checks.py` → 5 gate 绿。
```bash
git add trading/compute/breaker.py trading/phases/pre_open.py tests/trading/test_breaker_fail_closed.py tests/trading/test_baseline_backfill.py
git commit -m "fix(risk): 熔断 fail-closed + account_daily 基线 T-1 兜底（DG-G3·语义变更）

breaker.py:50 基线 NULL 由 fail-open(放行) 翻转为 fail-closed：
模拟盘停手+CRITICAL、实盘 _CriticalHalt。基线 T-1 回填避免每天误熔断。
ADR：08-11 只治了静默（告警+T-1 兜底），fail-open 语义残留至此收尾。"
```

---

### Task G4: 外部 SDK 超时注入（韧性链复活）

**Files:**
- Modify: `data/_tushare_compat.py:78`（`get_pro`）
- Modify: `data/fetcher.py:614`（FRED `get_series`）
- Modify: `broker/qmt_quote.py:148`（`get_full_tick` 的 run_in_executor）
- Modify: `data/calendar.py:44`（`pro.trade_cal`）
- Test: `tests/test_sdk_timeout.py`（新建）

**Interfaces:**
- Consumes: `concurrent.futures.ThreadPoolExecutor`、`asyncio.wait_for`
- Produces: SDK 调用挂起时抛 `TimeoutError` → 触发 `CircuitBreaker.record_failure`。

- [ ] **Step 1: 写失败测试——get_pro 超时**

```python
# tests/test_sdk_timeout.py
import pytest, socket
from unittest.mock import patch

def test_tushare_pro_call_timeout_raises(monkeypatch):
    """pro.daily 调用 TCP 挂起 → 30s 内抛 TimeoutError（触发熔断 record_failure）。"""
    def _hang(*a, **kw):
        # 模拟 socket 挂起不返回
        import time as _t; _t.sleep(60)
    from data import _tushare_compat
    pro = _tushare_compat.get_pro()
    with patch.object(pro, "daily", _hang):
        from data.tushare_sync import _fetch_with_guard  # 或直接包 get_pro 调用
        # 断言：带 timeout 包裹后，30s 抛 TimeoutError（测试用 monkeypatch 把 timeout 降到 0.5s）
        monkeypatch.setattr("data._tushare_compat._CALL_TIMEOUT", 0.5)
        with pytest.raises(TimeoutError):
            list(_tushare_compat._call_with_timeout(pro.daily, ts_code="000001.SZ"))
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_sdk_timeout.py -v`
Expected: FAIL（无 `_call_with_timeout` / `_CALL_TIMEOUT`）。

- [ ] **Step 3: 加 `_call_with_timeout` 包裹器到 _tushare_compat**

Modify `data/_tushare_compat.py`，新增：
```python
import concurrent.futures
_CALL_TIMEOUT = float(os.getenv("TUSHARE_CALL_TIMEOUT", "30"))

def _call_with_timeout(fn, *args, **kwargs):
    """线程池包裹 SDK 调用 + 超时。
    Why：CircuitBreaker/退避重试依赖异常抛出才生效；TCP 挂起不抛异常时
    整条韧性链被旁路。本包裹让挂起在 _CALL_TIMEOUT 秒后抛 TimeoutError，
    触发上层 record_failure。对齐 akshare _call_ak(30s)/alpha_vantage(15s) 范式。"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(fn, *args, **kwargs)
        try:
            return fut.result(timeout=_CALL_TIMEOUT)
        except concurrent.futures.TimeoutError as e:
            raise TimeoutError(f"tushare 调用超时(>{_CALL_TIMEOUT}s)") from e
```
并在 `get_pro()` 返回的 pro 实例的关键方法（daily/daily_basic 等）接入——若不便逐方法包，则在 `tushare_sync._fetch_with_guard` 的 `pro` 调用点用 `_call_with_timeout` 包裹。

- [ ] **Step 4: 运行确认通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_sdk_timeout.py -v` → PASS。

- [ ] **Step 5: get_full_tick 加 asyncio.wait_for**

Modify `broker/qmt_quote.py:148`（两个 `run_in_executor` 裸 await）：
```python
# 旧
tick = await loop.run_in_executor(None, xtdata.get_full_tick, symbols)
# 新（DG-G4：客户端僵死时 5s 超时，防永久占线程池 worker）
try:
    tick = await asyncio.wait_for(loop.run_in_executor(None, xtdata.get_full_tick, symbols), timeout=5.0)
except asyncio.TimeoutError:
    logger.warning("xtdata.get_full_tick 超时>5s，行情降级跳过（与缺失语义一致）")
    return {s: None for s in symbols}
```

- [ ] **Step 6: calendar trade_cal 加超时**

Modify `data/calendar.py:44`（启动期 `pro.trade_cal` 同步调用）改线程池 `_call_with_timeout` 包裹。

- [ ] **Step 7: run_checks + commit**

Run: `.venv310/Scripts/python.exe ops/run_checks.py` → 5 gate 绿。
```bash
git add data/_tushare_compat.py broker/qmt_quote.py data/calendar.py tests/test_sdk_timeout.py
git commit -m "fix(resilience): SDK 超时注入——让 TCP 挂起能抛 TimeoutError 触发熔断（DG-G4）

get_pro/trade_cal 线程池 30s 包裹；get_full_tick asyncio.wait_for 5s。
对齐 akshare/alpha_vantage 已有范式，消除三重标准。"
```

---

### Task G5: 数据原子写 + schema 迁移安全

**Files:**
- Modify: `data/integrity.py:196`（`safe_overwrite` 升级 tmp+rename）
- Modify: `data/tools/sync_daily_incremental.py:258`、`data/tools/repair_gaps.py`（接入原子写）
- Modify: `trading/state_store.py:170`（DROP 重建 → 备份式）
- Test: `tests/test_atomic_overwrite.py`（新建）、`tests/trading/test_schema_migration_preserves.py`（新建）

**Interfaces:**
- Consumes: `os.replace`
- Produces: `safe_overwrite` 原子语义（tmp → os.replace）。

- [ ] **Step 1: 读 safe_overwrite 现状**

Read `data/integrity.py:190-210`（`safe_overwrite` 当前行数守卫逻辑）。

- [ ] **Step 2: 写失败测试——写中途异常不损目标**

```python
# tests/test_atomic_overwrite.py
import pytest, os, pandas as pd
from data.integrity import safe_overwrite

def test_atomic_overwrite_preserves_original_on_failure(tmp_path, monkeypatch):
    """写中途异常 → 目标文件保留旧完整内容（不留半截损坏）。"""
    target = tmp_path / "lake.parquet"
    pd.DataFrame({"a": [1, 2]}).to_parquet(target)
    original_bytes = target.read_bytes()
    def _boom(*a, **kw):
        raise RuntimeError("模拟 OOM")
    monkeypatch.setattr("pandas.DataFrame.to_parquet", _boom)
    with pytest.raises(RuntimeError):
        safe_overwrite(str(target), pd.DataFrame({"a": [3]}), writer="to_parquet")
    assert target.read_bytes() == original_bytes  # 旧文件无损
    assert not (tmp_path / "lake.parquet.tmp").exists()  # tmp 清理
```

- [ ] **Step 3: 运行确认失败 → 改 safe_overwrite 为原子**

Modify `data/integrity.py:196` `safe_overwrite`：先写 `path+".tmp"` → `os.replace(tmp, path)`（同卷原子）。行数守卫保留在 tmp 写入阶段。

- [ ] **Step 4: 运行确认通过 → sync_daily_incremental/repair_gaps 接入**

确认 `sync_daily_incremental.py:258` 与 repair_gaps 已用 `safe_overwrite`（非裸 to_parquet）；若裸写则改调 safe_overwrite。

- [ ] **Step 5: 写 schema 迁移守恒测试**

```python
# tests/trading/test_schema_migration_preserves.py
def test_state_store_migration_preserves_rows(tmp_path):
    """旧 fill/position 表迁移（补列）→ 行数与数据守恒，非 DROP 丢数据。"""
    # 造旧 schema 表 → 调 init_store 迁移 → 断言行数不变、旧列值保留
```

- [ ] **Step 6: 运行确认失败 → state_store DROP 改备份式**

Modify `trading/state_store.py:170`（`DROP TABLE fill` 分支）：改「导出旧行 → 重建带新列 → 回灌」，破坏「live-前-无-数据」假设前先备份。像素级注释 Why（连错库即清零风险）。

- [ ] **Step 7: run_checks + commit**

```bash
git add data/integrity.py data/tools/sync_daily_incremental.py data/tools/repair_gaps.py trading/state_store.py tests/test_atomic_overwrite.py tests/trading/test_schema_migration_preserves.py
git commit -m "fix(data): 原子写(tmp+rename) + schema 迁移备份式（DG-G5）

safe_overwrite 升级原子语义防半截损坏 parquet；state_store DROP 重建改导出→重建→回灌。"
```

---

### Task G6: SQLite 协调点 WAL + timeout 基线

**Files:**
- Modify: `trading/job_ledger.py:57`（`_connect`）
- Modify: `trading/state_store.py:60`（`_connect`）
- Test: `tests/trading/test_sqlite_wal_baseline.py`（新建）

**Interfaces:**
- Produces: `_connect` 统一 `timeout=30` + WAL。

- [ ] **Step 1: 写失败测试——并发写无 BUSY**

```python
# tests/trading/test_sqlite_wal_baseline.py
import pytest, threading
from trading import job_ledger

def test_job_ledger_concurrent_writes_no_busy(tmp_path, monkeypatch):
    """多线程并发 begin_run/finish_run → 不抛 SQLITE_BUSY（WAL 串行化 + timeout=30）。"""
    monkeypatch.setenv("TRADING_JOB_LEDGER_DB", str(tmp_path / "jl.db"))
    job_ledger.init_db()
    errors = []
    def _worker(i):
        try:
            job_ledger.begin_run(f"job_{i}", "2026-08-13")
            job_ledger.finish_run(f"job_{i}", "2026-08-13", "done")
        except Exception as e:
            errors.append(str(e))
    ts = [threading.Thread(target=_worker, args=(i,)) for i in range(10)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert not errors, f"并发写抛错: {errors}"
```

- [ ] **Step 2: 运行确认失败（或慢/偶发 BUSY）**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_sqlite_wal_baseline.py -v`

- [ ] **Step 3: job_ledger._connect 加 WAL + timeout**

Modify `trading/job_ledger.py:57`，对齐 `backtest/tasks_db.py:45` 范式：
```python
con = sqlite3.connect(db, timeout=30)  # DG-G6：默认 5s → 30s，防并发 busy
con.execute("PRAGMA journal_mode=WAL")
con.execute("PRAGMA busy_timeout=30000")
```
job_ledger 领取类（begin_run）加 `BEGIN IMMEDIATE` 原子领取（参考 tasks_db.claim_next_pending）。

- [ ] **Step 4: state_store._connect 同步加 timeout + WAL**

Modify `trading/state_store.py:60` `_connect` 加 `timeout=30` + WAL；docstring 钉死「仅事件循环线程可写」红线。

- [ ] **Step 5: 运行确认通过 + run_checks + commit**

```bash
git add trading/job_ledger.py trading/state_store.py tests/trading/test_sqlite_wal_baseline.py
git commit -m "fix(concurrency): sqlite 协调点统一 timeout=30+WAL（DG-G6）

job_ledger/state_store _connect 对齐 tasks_db/discovery_store 范式；
begin_run 加 BEGIN IMMEDIATE 原子领取。"
```

---

### Task G7: 告警可观测 + FSM 收口

**Files:**
- Modify: `broker/qmt.py:224,1351,1386,1394,1400`（告警 `except: pass` 加 log）
- Modify: `discovery/worker.py:63`、`daemon.py:137,142`、`snapshot.py:105`、`objective.py:46`（降级加 log）
- Modify: `trading/state_store.py:419`（`update_order_state` 过 FSM 校验）、`:939,960`（终态集改引用 order_state 单源）
- Test: `tests/trading/test_fsm_guard_on_write.py`（新建）、`tests/discovery/test_degradation_logged.py`（新建）

**Interfaces:**
- Consumes: `trading.order_state.OrderStateMachine._is_valid_transition`、`OrderState`
- Produces: 状态写入经 FSM 校验；告警/降级失败可观测。

- [ ] **Step 1: 写失败测试——非法状态迁移被拒**

```python
# tests/trading/test_fsm_guard_on_write.py
import pytest
from trading import state_store

def test_illegal_state_transition_rejected(tmp_path, monkeypatch):
    """state_store.update_order_state 非法迁移（如 FILLED→PENDING）→ 拒绝/告警，不静默写。"""
    monkeypatch.setenv("TRADING_STATE_DB", str(tmp_path / "s.db"))
    state_store.init_store()
    # 造一个 FILLED 订单，试图改回 PENDING → 应被 FSM 拒
    ...  # 断言：抛 ValueError 或返 False + log
```

- [ ] **Step 2: 运行确认失败 → state_store 写状态过 FSM**

Modify `trading/state_store.py:419` `update_order_state`：写入前调 `OrderStateMachine._is_valid_transition(old, new)`，非法则告警 + 拒写。`_TERMINAL_ACTIONS`(:939)/`_PENDING_ORDER_STATES`(:960) 改引用 `order_state` 单源（消三套集合漂移）。

- [ ] **Step 3: 写测试——降级失败有 log**

```python
# tests/discovery/test_degradation_logged.py
def test_worker_exception_logged(caplog):
    """discovery worker 异常降级 → logger.warning/exception 有记录（非哑降级）。"""
    import logging
    caplog.set_level(logging.WARNING)
    ...  # 触发 scan_symbol 异常 → 断言 caplog 有该 sym 的 warning
```

- [ ] **Step 4: 运行确认失败 → 加 log**

Modify `broker/qmt.py` 5 处告警 `except Exception: pass` → `except Exception: logger.debug("告警通道软降级", exc_info=True)`（import 失败需留痕，对齐 critical.py:65 范式）。
Modify `discovery/{worker,daemon,snapshot,objective}.py` 对应 `except: pass/continue` → 加 `logger.warning(..., exc_info=True)`（不改控制流）。

- [ ] **Step 5: 运行确认通过 + run_checks + commit**

Run: `.venv310/Scripts/python.exe ops/run_checks.py` → 5 gate 绿。
```bash
git add broker/qmt.py discovery/worker.py discovery/daemon.py discovery/snapshot.py discovery/objective.py trading/state_store.py tests/trading/test_fsm_guard_on_write.py tests/discovery/test_degradation_logged.py
git commit -m "fix(observability+consistency): 告警降级加 log + FSM 写入校验 + 终态集单源（DG-G7）

告警 fire_and_forget 外层 pass→debug log（消「监控监控器」盲区）；
state_store 写状态过 OrderStateMachine 校验，终态集引用 order_state 单源。"
```

---

## Self-Review

**1. Spec 覆盖**：G1(CI,N1前置)✓ G2(鉴权+SSE,DG-G2 cookie)✓ G3(熔断+基线,DG-G3)✓ G4(SDK超时)✓ G5(原子写+schema)✓ G6(WAL)✓ G7(告警+FSM)✓。§8 文档订正（tech-debt 补2条/闭1项/N2N3清理）为实施期同步项，不进 G 波 Task（避免散改），随各 Task commit 附带或收尾单独 commit。

**2. Placeholder 扫描**：test_fsm/test_degradation 的 `...` 是「补 fixture 造数据」占位（参考既有 test_state_store/test_discovery 范式），实施时需填实——已在 step 注明参考文件。其余 step 均有真实代码/diff。

**3. 类型一致**：`_CriticalHalt`/`_alert_critical`/`_mode` 来自 trading.critical（engine.py re-export 确认）；`require_write`/`_configured_token` 来自 http/auth.py；`OrderStateMachine._is_valid_transition` 来自 order_state.py——签名以 Step 1「读现状」核对为准（防 HEAD 漂移）。

**4. 顺序依赖**：G1 必须最先（复活 CI 才有回归网）；G3 的 breaker fail-closed 与基线 T-1 兜底**必须同 commit**（否则退化每天熔断）；G2/G4/G5/G6/G7 互相独立可并行。

**5. G7 迁移机制偏离 spec（已知情接受 · 2026-08-14 补记）**：基准 spec §G7 处方 `ALTER TABLE RENAME TO fill_legacy_<日期>`（可回滚、零数据丢失）；实施（`state_store.py:168-250 _migrate_with_backup`）采「导出旧行→DROP→CREATE 新 schema→共享列回灌 + column_copies 行级拷贝 + sidecar JSON 双保险」，撞约束跳过不阻断。**取舍**：数据安全（行守恒 + sidecar 可恢复，连错库不清零）但旧表无 legacy 名可一键回滚。fill 表首版曾 sidecar-only 不回灌，`5df74c03` 改回灌（traded_time←applied_at）。决策（用户 2026-08-14）：维持现状 + 本注记，**不重做 RENAME**——重写已测迁移有回归风险，live 前数据安全已满足，回滚通道非当前刚需。

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-13-g-wave-p0-guards.md`.

**两种执行方式：**

**1. Subagent-Driven（推荐）** — 每个 Task 派一个全新 subagent，Task 间两阶段 review，快速迭代。

**2. Inline Execution** — 本会话内 executing-plans 批量执行 + checkpoint review。

**选哪种？** （G1 必须先执行以复活 CI 保护链，建议无论哪种都 G1 优先。）
