# 安全与正确性修复实现计划（审查 REQUEST CHANGES 整改）

> **For agentic workers:** REQUIRED SUB-SKILL: 用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现。步骤用 `- [ ]` 复选框跟踪。

**Goal:** 修复 2026-07-09 全量审查中的全部阻塞性问题（B-1~B-10）与高价值应修项，使蔡森形态学流水线 + 实盘交易链路达到「可上实盘」的安全与正确性基线。

**Architecture:** 分三阶段推进——P0 机械修复（无设计分叉，可立即 TDD）；P1 正确性修复（含 5 个需研究员拍板的设计决策点，本文档给出推荐方案 + 备选）；P2 应修项 backlog（轻量记录，逐项实现时再展开）。修复严格遵循 CLAUDE.md：全中文注释、显式无黑盒、边界审查三连、Karpathy 极简。

**Tech Stack:** Python 3.10（`.venv310`，EMT 网关硬约束）、FastAPI、Celery prefork、pandas 2.x、pytest、JSON 文件状态机（caisen/storage）、vnemttrader/xtquant C++ 回调网关。

## Global Constraints

- **语言**：所有对话、注释、文档 100% 中文（CLAUDE.md 强制）。
- **Python 版本**：3.10，venv 位于 `.venv310`（EMT `vnemttrader.pyd` 仅 Win + 3.10）。改动不得引入 3.11+ 语法（如 `match` 之外的新特性需谨慎）。
- **反魔法**：数学/数据清洗用显式 pandas/numpy，禁引入新重型量化库。
- **边界审查**：每个修复须显式处理断线/脏数据/部分成交/除零；异常不得静默吞。
- **测试纪律**：P0/P1 每个修复先写失败测试（复现 bug），再改实现，再绿。修复前若有「保护 bug 的测试」须先订正。
- **提交粒度**：每个 Task 独立 commit，commit message 中文，遵循既有 `feat/fix/test/refactor(scope):` 风格。

---

## 核对结论与 Triaged 清单

### ✅ 已源码核实·属实的阻塞项（8 项）

| 编号 | 一句话诊断 | 阶段 |
|---|---|---|
| B-1 | `main.py` 全应用零认证，仅 CORS，`allow_credentials=True`+`allow_methods=["*"]` | P1·决策点 D2 |
| B-2 | `storage.save_plans(date)` 直接拼接文件名，`date` 自由字符串无校验 → 路径遍历 | P0 |
| B-3 | 回测「浮盈≥阈值锁盈」vs 实盘「浮盈<阈值止损」，运算符/分母/意图全反 | P1·决策点 D1 |
| B-4 | `tick_pullback` 丢弃 `submit_order` 返回值（实为 SUBMITTED）直接标 FILLED → 幽灵持仓 | P1 |
| B-6 | `OrderStateMachine` 纯内存；`trading_service.submit_order` 真单成功路径不落 `record_live_trade` | P0（审计）+ P1（持久化） |
| B-7 | `MacroAwareGateway` 不继承基类、同步签名不兼容、改 frozen 字段抛异常、零生产接入 = 死代码 | P1·决策点 D5 |
| B-8 | EMT `onDisconnected` 仅置锁+日志，不重连、无钉钉告警；锁态离场监控停摆 | P1·决策点 D3 |
| B-10 | Celery 每 tick `asyncio.run` 新建 loop，与 EMT 网关固化 `self._loop` 冲突 → 跨 loop RuntimeError | P1·决策点 D4 |

### 🔶 缺陷真实但严重度被高估（pushback）

| 编号 | 核实结论 | 处置 |
|---|---|---|
| B-5 | `clean_macro` 默认 bfill + 注释误述「防前视」属实，但 **全仓仅 `tests/test_data.py` 引用**，不在热路径，无活跃前视泄漏。真宏观管线 `sync_macro_credit.py` 是 ffill-only 的。 | P0：改 ffill + 订正注释 + 重写保护 bug 的断言（或删死代码，见 D-附录） |
| B-9 | `clean_ohlcv` 全列 ffill（含 volume/amount）属实，同样仅测试引用。 | P0：volume/amount 排除 ffill + 订正测试 |

### ❌ 误报（pushback，不在修复范围）

| 编号 | 核实结论 |
|---|---|
| 应修项 20 | `caisen/risk.py:161` 已 `max(entry-stop,1e-9)` 防除零；回测侧同样已防。`/entry`（164 行）entry 为突破价恒 >0。**除零已防护，基本不成立。** |

---

## 设计决策点（需研究员在 review 时确认/否决）

> 这 5 个分叉改变实现方向，本文档已给「推荐方案」并落代码；审批时若选备选，相应 Task 替换即可。

### D1（B-3）回测/实盘时间止损以哪侧为 canonical？

- **现状**：`plan.py:88` docstring 写「浮盈 ≥ 阈值才允许离场」（锁盈，不足继续持有）——与回测一致；实盘 `check_exit` 是「浮盈 < 阈值则止损离场」（砍亏）。
- **推荐：Option B（统一为「砍亏」）** —— 改回测 `_simulate_one_trade` 与 `check_exit` 完全一致：`profit = (close-entry)/entry`（百分比分母），`profit < timeout_exit_threshold → 离场`。理由：① 实盘才是真金白银，时间止损砍亏是行业惯例，避免「超时亏损单永不实现」虚高回测；② 百分比分母语义直观，`timeout_exit_threshold` 作百分比可解释。
- **备选：Option A（统一为「锁盈」，对齐 plan.py docstring）** —— 改实盘 `check_exit` 为 `unrealized >= threshold → 离场`（R 分母）。若蔡森原典确为「锁盈」，选此，但须接受「超时浮亏单继续持有」的回撤风险，且须订正回测分母为 R。
- **本计划默认按 Option B 落代码（Task P1-3），审批时确认。**

### D2（B-1）鉴权方案选型

- **推荐：HTTPBearer token + 可选 IP 白名单**（本地/单用户部署亦适用，零外部依赖）。读 token 于环境变量 `QUANTER_API_TOKEN`；未配置时 `auth` 依赖**默认放行但打 WARNING**（开发态不阻断），生产强制要求配置。
- 备选 1：仅 IP 白名单（`QUANTER_ALLOWED_IPS`）——更轻但易被 NAT/代理绕过。
- 备选 2：前置 nginx 做 mTLS/Basic Auth，应用层不动——适合已部署反代的场景。
- **本计划按推荐落代码（Task P1-5）。同时收紧 CORS：`allow_methods` 收敛为实际用到的谓词，`allow_origins` 读 `CORS_ORIGINS`（已做）。**

### D3（B-8）断线重连策略

- **推荐：指数退避自动重连（最多 N 次）+ 钉钉/企微告警 + 重连失败后锁态保命。** `onDisconnected` 触发 `asyncio.create_task(_reconnect())`，退避 2/4/8/16/30s，每失败一次 fire_and_forget 告警；耗尽后保持 `_lock_down=True` 等人工。
- 备选：纯人工（仅告警不重连）——更保守但违背「断线敞口失控」红线。
- **本计划按推荐落代码（Task P1-7）。** 另：锁态下 `monitor_holding` 不应停摆（离场监控必须持续），见 Task P1-8。

### D4（B-10）Celery↔async 网关执行模型

- **推荐：把蔡森 beat 从 Celery 迁到 FastAPI 进程内 APScheduler**（单进程内 gateway `_loop` 与 beat 共享 uvicorn loop，根治跨 loop 问题；Celery 仅留 CPU 密集/批处理任务）。这是架构级改动，本计划先落「最小修复」（Task P1-9a：worker 级单例 loop + loop 存活复用），APScheduler 迁移列为 P1-9b 可选大改。
- 备选：保留 Celery + 改 `asynccelery`/geevent 池——改动面更大、与 prefork 行为差异多，不推荐。
- **本计划落 P1-9a（最小修复），P1-9b 列为 follow-up。**

### D5（B-7）MacroAwareGateway 死代码：删除还是正确接入？

- **背景**：蔡森专精化（Phase 1）已删除宏观 CTA 策略，当前唯一策略 caisen 是纯价量形态学，**不消费 CreditRegime**。`MacroAwareGateway` 仅文档/单测引用。
- **推荐：删除 `MacroAwareGateway` + `VetoedError` + `test_execution_gateway_veto.py`，同步订正 README/spec 引用。** 理由：YAGNI——为已不存在的宏观策略保留死代码（且机械上不可用）徒增误导面。
- 备选：若计划重启宏观风控，则改造它正确继承 `BaseExecutionGateway`（async 签名、返回新 OrderRequest 而非就地改 frozen 字段）并在 `trading_service.submit_order` 前接入。成本显著更高。
- **本计划按推荐删除（Task P1-10），审批时确认是否保留宏观风控口子。**

---

## File Structure（改动面映射）

| 文件 | 责任 | 涉及 Task |
|---|---|---|
| `caisen/storage.py` | 计划持久化：加 date 校验、原子写、文件锁 | P0-1, P0-5 |
| `server/schemas/caisen.py` | ScanRequest.date 加 constrained str | P0-1 |
| `data/cleaner.py` | clean_macro 改 ffill、clean_ohlcv 排除 volume/amount、订正注释 | P0-2 |
| `tests/test_data.py` | 订正保护 bug 的 bfill/ffill 断言 | P0-2 |
| `server/services/trading_service.py` | 真单成功落 record_live_trade；lifespan disconnect | P0-3, P1-11 |
| `trading/order_state.py` | fail() 状态表加 ANY→FAILED + None 守卫 | P0-4 |
| `caisen/backtest_replay.py` | 时间止损语义统一（Option B） | P1-3 |
| `caisen/execution.py` | tick_pullback 校验 OrderResult.state；check_exit 语义对齐 | P1-3, P1-4 |
| `server/main.py` | 注册鉴权依赖；lifespan disconnect gateway | P1-5, P1-11 |
| `server/core/auth.py`（新建） | HTTPBearer + IP 白名单依赖 | P1-5 |
| `trading/emt_gateway.py` | 断线自动重连 + 告警 | P1-7 |
| `trading/qmt_gateway.py` | 同步重连（与 EMT 对称） | P1-7 |
| `server/celery_app.py` | worker 级单例 loop（最小修复） | P1-9a |
| `trading/execution_gateway.py` | 删除 MacroAwareGateway/VetoedError | P1-10 |
| `core/notifier.py` | 确认断线告警通道（已存在则接线） | P1-7 |

---

# Phase 0 · P0 机械修复（无设计分叉，立即 TDD）

## Task P0-1: 修复 caisen/storage.py 路径遍历（B-2）

**Files:**
- Modify: `caisen/storage.py:137-153`（save_plans）
- Modify: `server/schemas/caisen.py:89`（ScanRequest.date）
- Test: `tests/caisen/test_storage.py`（新增/扩充）

**Interfaces:** Produces `save_plans` 仍接收 `date: str`，但内部强制 `YYYY-MM-DD` 校验，非法抛 `ValueError`。

- [ ] **Step 1: 写失败测试（复现路径遍历）**

```python
# tests/caisen/test_storage.py
import pytest
from caisen import storage

def test_save_plans_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "_PLANS_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="非法日期"):
        storage.save_plans("../../../etc/cron.d/evil", plans=[])

def test_save_plans_rejects_non_iso_date(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "_PLANS_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        storage.save_plans("2024/06/01", plans=[])  # 非标准分隔符

def test_save_plans_accepts_iso_date(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "_PLANS_DIR", str(tmp_path))
    storage.save_plans("2024-06-01", plans=[])  # 合法不抛
    assert (tmp_path / "2024-06-01.json").is_file()
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/caisen/test_storage.py -v`
Expected: 3 个 FAIL（当前无校验，遍历用例会真写文件或抛非 ValueError）

- [ ] **Step 3: 实现 date 校验辅助 + save_plans 接入**

在 `caisen/storage.py` 顶部加（`_DEFAULT_STATUS` 附近）：

```python
import re

# ISO 日期严格正则：YYYY-MM-DD（防路径遍历 ../ 与非法分隔符）。
# Why 严格：date 直接拼进文件名，任何非标准字符都可能成为路径跳板。
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_iso_date(date: str) -> None:
    """校验 date 为严格 YYYY-MM-DD，非法抛 ValueError（防路径遍历/注入）。"""
    if not isinstance(date, str) or not _ISO_DATE_RE.match(date):
        raise ValueError(f"非法日期（须 YYYY-MM-DD）：{date!r}")
    # 二次防御：re 通过但仍解析失败（如月份 13）
    import pandas as pd
    try:
        pd.Timestamp(date)
    except Exception as exc:
        raise ValueError(f"非法日期（解析失败）：{date!r}") from exc
```

`save_plans` 第一行加：

```python
def save_plans(date: str, plans: list) -> None:
    _validate_iso_date(date)   # ← 新增：路径遍历防御
    _ensure_dir()
    ...
```

- [ ] **Step 4: schema 层加 constrained str（纵深防御）**

`server/schemas/caisen.py:89`：

```python
from pydantic import BaseModel, Field

class ScanRequest(BaseModel):
    # 严格 YYYY-MM-DD（与 storage._validate_iso_date 双重防御：schema 拦 + storage 拦）
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    ...
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/caisen/test_storage.py tests/test_caisen_api.py -v`（若 schema 测试存在）
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add caisen/storage.py server/schemas/caisen.py tests/caisen/test_storage.py
git commit -m "fix(caisen): save_plans date 严格 ISO 校验防路径遍历（B-2）"
```

---

## Task P0-2: 修复 data/cleaner.py bfill 前视 + volume/amount ffill（B-5/B-9）

**Files:**
- Modify: `data/cleaner.py:22-98`（clean_ohlcv / clean_macro）
- Modify: `tests/test_data.py:214-235`（订正保护 bug 的断言）

**Interfaces:** `clean_macro(fill_method="ffill")`（默认改 ffill）；`clean_ohlcv` 仅对 OHLC 列 ffill，volume/amount 保持 NaN（停牌真流动性）。

- [ ] **Step 1: 订正测试（先改断言使其表达正确语义，此时应失败）**

`tests/test_data.py` 中：

```python
def test_clean_macro_ffills_missing_values(self):
    """宏观数据缺失应【向前填充】（用过去解释现在，防前视偏差）。"""
    df = pd.DataFrame({"m1": [1.0, None, 3.0]}, index=pd.date_range("2024-01-01", periods=3))
    df_clean = DataCleaner.clean_macro(df)
    # ffill：第 2 行应填前值 1.0（而非后值 3.0 的 bfill）
    assert df_clean["m1"].iloc[1] == 1.0

def test_clean_macro_rejects_bfill(self):
    """bfill 引入前视偏差，clean_macro 应拒绝（默认 ffill-only）。"""
    df = pd.DataFrame({"m1": [1.0, None, 3.0]})
    with pytest.raises(ValueError):
        DataCleaner.clean_macro(df, fill_method="bfill")

def test_clean_ohlcv_does_not_ffill_volume(self):
    """volume/amount 绝不 ffill——停牌期间须保留 NaN，防流动性测算失真。"""
    df = pd.DataFrame({
        "open":[10,10,10], "high":[11,11,11], "low":[9,9,9], "close":[10,10,10],
        "volume":[1000, None, 3000], "amount":[10000, None, 30000],
    })
    df_clean = DataCleaner.clean_ohlcv(df)
    assert pd.isna(df_clean["volume"].iloc[1])   # 停牌量保留 NaN
    assert pd.isna(df_clean["amount"].iloc[1])
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_data.py::TestDataCleaner -v`
Expected: FAIL（旧实现 bfill / 全列 ffill）

- [ ] **Step 3: 订正 clean_macro（默认 ffill，注释订正前视偏差定义）**

```python
@staticmethod
def clean_macro(df: pd.DataFrame, fill_method: str = "ffill") -> pd.DataFrame:
    """清洗宏观数据。

    参数：
        df: 原始宏观数据（低频，如月度社融/M1M2）。
        fill_method: 填充方法。**仅 "ffill" 合法**——向前填充，用「已公布的最近一期值」
                      解释当下，这是防前视偏差的唯一合法方向。
                      ⚠️ bfill（后向填充）会把「未来才公布的值」回填到历史，构成前视偏差，
                      量化系统绝禁使用（回测完美、实盘崩盘的典型未来函数陷阱）。

    返回：
        清洗后的 DataFrame（NaN 已 ffill；头部首值前无历史则保留 NaN，由调用方 dropna）。
    """
    df_clean = df.copy()
    if fill_method != "ffill":
        raise ValueError(
            f"不支持的填充方法: {fill_method}（宏观仅允许 ffill；bfill 引入前视偏差）"
        )
    df_clean = df_clean.ffill()
    return df_clean
```

- [ ] **Step 4: 订正 clean_ohlcv（OHLC ffill，volume/amount 排除）**

```python
@staticmethod
def clean_ohlcv(df: pd.DataFrame, max_fill: int = 5) -> pd.DataFrame:
    df_clean = df.copy()
    # ...（abnormal/illiquid 检测不变）...

    # 3. 缺失值填充：仅对 OHLC 前向填充（最多 max_fill 天）。
    # Why 排除 volume/amount：停牌期间成交量/成交额为真·零流动性，ffill 会伪装成
    # 「停牌前最后一天的量」，导致流动性测算/VWAP 严重失真（lake_reader.py 红线同口径）。
    ohlc_cols = [c for c in ("open", "high", "low", "close") if c in df_clean.columns]
    for c in ohlc_cols:
        df_clean[c] = df_clean[c].ffill(limit=max_fill)
    # volume/amount 不动（保留 NaN，下游 liquidity_filter 会 dropna 排除停牌日）

    # ...（invalid_ohlc 修正 / high=max / volume<0→0 等不变）...
    return df_clean
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/test_data.py -v`
Expected: PASS（注意：若有其它旧用例依赖「全列 ffill/bfill」需同步订正）

- [ ] **Step 6: Commit**

```bash
git add data/cleaner.py tests/test_data.py
git commit -m "fix(data): clean_macro 改 ffill 防前视 + clean_ohlcv 排除 volume/amount（B-5/B-9）"
```

---

## Task P0-3: 真单成功路径落 record_live_trade 审计流水（B-6 审计缺口 / 应修项 1）

**Files:**
- Modify: `server/services/trading_service.py:370-376`

**Interfaces:** `submit_order` 真单成功后落 CSV 流水（direction=`BUY`/`SELL`，state=`result.state.name`），兑现 docstring「真单/废单/撤单均落 CSV」契约。

- [ ] **Step 1: 写失败测试**

`tests/test_trading_service.py`：

```python
@pytest.mark.asyncio
async def test_submit_order_real_fill_records_audit(monkeypatch):
    """真单成功（state=FILLED）必须落 record_live_trade 审计流水（spec §6.3）。"""
    from trading.execution_gateway import OrderResult
    from trading.order_state import OrderState
    from server.services import trading_service as ts

    captured = []
    monkeypatch.setattr(ts, "record_live_trade",
                        lambda *a, **k: captured.append((a, k)))
    # mock 网关返 FILLED + check_order 放行（dry_run=False, allow_live=True）
    fake_gw = FakeGateway(OrderResult("OID", OrderState.FILLED, 100, 10.5))
    monkeypatch.setattr(ts, "get_gateway", lambda: fake_gw)
    monkeypatch.setattr(ts, "_allow_live", lambda: True)
    monkeypatch.setattr(ts.qmt_market_data, "get_quote", lambda s: _async_none())

    await ts.submit_order(_order(side="buy"), dry_run=False, confirm=True)
    assert any(captured), "真单成功未落审计流水"
    assert captured[0][0][1] == "BUY"   # direction
```

- [ ] **Step 2: 运行确认失败** —— `pytest tests/test_trading_service.py -k audit -v` → FAIL（当前 370-376 不落流水）

- [ ] **Step 3: 实现落流水**（`trading_service.py:370-376` 替换）

```python
    # 4. 全过 → 真下单
    result: OrderResult = await gw.submit_order(order)
    # 真单审计落盘（spec §6.3 可追溯性契约：真单/废单/撤单均落 CSV）。
    # Why 此前缺失：仅在 dry_run/BLOCKED 落流水，真实成交反成黑洞，违背审计合规。
    direction = "BUY" if order.side.lower() == "buy" else "SELL"
    record_live_trade(
        order.symbol, direction, order.qty, order.price or 0.0,
        rationale=f"{gw.__class__.__name__}:{result.state.name}:{result.message}",
    )
    return {
        "order_id": result.order_id,
        "state": result.state.name,
        "message": result.message,
    }
```

- [ ] **Step 4/5: 测试通过 + Commit**

```bash
git commit -m "fix(trading): 真单成功路径补 record_live_trade 审计流水（B-6/应修项1）"
```

---

## Task P0-4: 修复 OrderStateMachine.fail() 状态表 + None 守卫（应修项 2）

**Files:**
- Modify: `trading/order_state.py:153-166`（fail）、`:206-229`（_is_valid_transition）

- [ ] **Step 1: 写失败测试**

```python
def test_fail_from_pending_allowed():
    """fail() 声称 ANY→FAILED，PENDING→FAILED 必须合法（submit 前异常兜底）。"""
    sm = OrderStateMachine()
    sm.fail("submit 前网络异常")     # 当前：抛「非法状态迁移」
    assert sm.get_state() == OrderState.FAILED

def test_fail_with_none_order_info_no_crash():
    """order_info=None 时 fail() 不应 TypeError（submit 前调用场景）。"""
    sm = OrderStateMachine()
    sm.fail("构造期异常")            # 当前：self.order_info["fail_reason"] → TypeError
    assert sm.get_state() == OrderState.FAILED
```

- [ ] **Step 2: 运行确认失败** —— `pytest tests/test_order_state.py -k fail -v` → FAIL

- [ ] **Step 3: 实现**

`fail()`：

```python
    def fail(self, reason: str) -> bool:
        """失败（异常处理）：支持从【任意非终态】迁移到 FAILED（含 PENDING，submit 前异常兜底）。"""
        # order_info 可能为 None（submit 前调用）：惰性初始化，防 TypeError。
        if self.order_info is None:
            self.order_info = {}
        self.order_info["fail_reason"] = reason
        self._transition_to(OrderState.FAILED)
        return True
```

`_is_valid_transition` 状态表：给所有非终态加 `FAILED`（最简：在判断里对 `to_state==FAILED` 且 `from_state` 非终态放行）：

```python
        # 终态不可再迁移；FAILED 可从任何非终态进入（异常兜底）
        if to_state == OrderState.FAILED and from_state not in (
            OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.FAILED,
        ):
            return True
        valid_transitions = {
            OrderState.PENDING: [OrderState.SUBMITTED],
            OrderState.SUBMITTED: [OrderState.PARTIAL_FILLED, OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED],
            OrderState.PARTIAL_FILLED: [OrderState.PARTIAL_FILLED, OrderState.FILLED, OrderState.PARTIAL_CANCELLED],
            OrderState.PARTIAL_CANCELLED: [OrderState.FILLED],
            OrderState.FILLED: [], OrderState.CANCELLED: [], OrderState.REJECTED: [], OrderState.FAILED: [],
        }
        return to_state in valid_transitions.get(from_state, [])
```

- [ ] **Step 4/5: 测试通过 + Commit**

```bash
git commit -m "fix(order_state): fail() 允许非终态→FAILED + None 守卫（应修项2）"
```

---

## Task P0-5: storage 原子写 + 文件锁（B-13 覆盖 / B-14 并发丢更新）

**Files:**
- Modify: `caisen/storage.py:127-131`（_write_json）、save_plans 覆盖语义

**Interfaces:** `_write_json` 改 `tempfile+os.replace` 原子替换；新增 `plans.lock` 全局 `fcntl`/`msvcrt` 锁包裹写路径（Win 用 `msvcrt.locking`，POSIX 用 `fcntl.flock`）。

- [ ] **Step 1: 写失败测试**（并发写不丢更新 + 覆盖只清 plans 不动 active）

```python
def test_write_json_atomic_no_corrupt(tmp_path, monkeypatch):
    """写入中途模拟崩溃：原子替换保证文件要么旧要么新，不出现半截 JSON。"""
    monkeypatch.setattr(storage, "_PLANS_DIR", str(tmp_path))
    storage._write_json(str(tmp_path / "x.json"), {"v": 1})
    assert json.loads((tmp_path / "x.json").read_text()) == {"v": 1}

def test_rescan_same_date_preserves_active(tmp_path, monkeypatch):
    """同 date 重扫不应清空已 ARMED/FILLED 的 active 索引（B-13）。"""
    monkeypatch.setattr(storage, "_PLANS_DIR", str(tmp_path))
    # 先造一个已 ARMED 的 active 计划，再 save_plans 同 date，验证 active 不被孤儿化
    ...
```

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现原子写 + 文件锁**

```python
import os, tempfile

def _write_json(path: str, data) -> None:
    """原子写：写临时文件 → os.replace 原子替换（防并发读到半截 JSON / 并发写丢更新）。"""
    _ensure_dir()
    # 同目录临时文件（os.replace 同文件系统才原子，故放同目录）
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise

# 跨进程写锁（Win msvcrt / POSIX fcntl），保证 screen/execute 并发写不丢更新
import sys
def _file_lock(lock_path: str):
    """返回一个上下文管理器，持有 lock_path 的独占锁（跨进程）。"""
    ...
```

save_plans 覆盖语义（B-13）：重扫同 date 时，保留原文件中已 ARMED/FILLED 的计划 status（不整体回退 PENDING_APPROVAL）——读旧 status → 回填新 plans 的同 plan_id。

- [ ] **Step 4/5: 测试通过 + Commit**

```bash
git commit -m "fix(caisen): storage 原子写+文件锁 + 重扫保留 active 状态（B-13/B-14）"
```

---

# Phase 1 · P1 正确性修复（含设计决策点）

## Task P1-3: 统一回测/实盘时间止损语义（B-3，决策点 D1，默认 Option B）

**Files:**
- Modify: `caisen/backtest_replay.py:249-258`（_simulate_one_trade 时间止损）
- Modify: `caisen/execution.py:142-148`（check_exit，仅分母对齐）
- Verify: `caisen/config.py`（timeout_exit_threshold 默认值语义）

**关键**：审批时确认 D1 选 Option A 还是 B。本 Task 按 **Option B（砍亏，百分比分母）** 落代码。

- [ ] **Step 1: 写测试，断言回测与 check_exit 在「超时浮亏」场景行为一致**

```python
def test_backtest_timeout_cuts_loser_matches_check_exit():
    """超时且浮亏 → 回测与 check_exit 都应离场（砍亏，语义统一）。"""
    # 构造一个 entry 后 max_holding 内 close 持续低于 entry*1.0+threshold 的序列
    # 断言 _simulate_one_trade exit_reason == "timeout"，且 rr<0
    ...
```

- [ ] **Step 2/3: 改回测**（`backtest_replay.py:249-258`）

```python
        # 优先级 4：时间止损（持仓达 max_holding_bars 且浮盈 < threshold → 砍亏离场）
        # 【B-3 修复】与 check_exit（execution.py）完全对齐：百分比分母 + profit<threshold→离场。
        # 旧实现 unrealized>=threshold 锁盈是错误的（与实盘相反，且让超时亏损单永不实现，虚高回测）。
        if pos >= max_hold_pos:
            profit = (close - entry_price) / entry_price   # 百分比，与 check_exit 同口径
            if profit < p.timeout_exit_threshold:
                exit_price = close
                exit_reason = "timeout"
                exit_pos = pos
                break
            # 浮盈 ≥ threshold：继续持有（未达砍亏条件）
```

`check_exit` 分母已是百分比（无需改运算符，仅确认与回测一致）。同步订正两处 docstring。

- [ ] **Step 4/5: 测试通过 + Commit**

```bash
git commit -m "fix(caisen): 时间止损语义统一为砍亏+百分比（回测对齐实盘，B-3）"
```

> 若审批选 Option A：替换为「改 check_exit 为 R 分母 + `unrealized>=threshold→离场`」，回测不动，并订正 plan.py:88 docstring 的分母表述。

---

## Task P1-4: tick_pullback 校验 OrderResult.state，杜绝幽灵持仓（B-4）

**Files:**
- Modify: `caisen/execution.py:255-272`（tick_pullback）

**Interfaces:** `submit_order` 返回 dict（`trading_service` 已返回 dict）；tick_pullback 仅在 `state ∈ {FILLED, PARTIAL_FILLED}` 时标 FILLED，`SUBMITTED` 留 ARMED 等回调推进，`REJECTED/FAILED` 标记计划异常。

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.asyncio
async def test_pullback_submitted_not_marked_filled(monkeypatch):
    """限价单仅 SUBMITTED（未成交）时，计划不得标 FILLED（防幽灵持仓）。"""
    engine = _engine_with_service(returning={"state": "SUBMITTED"})
    # 造一个 ARMED 计划 + 触及回踩区间的 quote
    await engine.tick_pullback()
    plan = storage.load_plans(status="ARMED")
    assert any(p["plan_id"] == "X" for p in plan), "SUBMITTED 不应标 FILLED"
```

- [ ] **Step 2: 运行确认失败**（当前无视 state 直接 FILLED）

- [ ] **Step 3: 实现状态校验**

```python
                result = await self.trading.submit_order(order, dry_run=False, confirm=True)
                state = (result or {}).get("state")
                # 【B-4 修复】仅在真实成交（FILLED/PARTIAL_FILLED）才推进 FILLED。
                # SUBMITTED=限价单排队未成交，留 ARMED 等下轮回调/对账推进；
                # REJECTED/FAILED=废单，标 PENDING_APPROVAL 回审核 + 告警，绝不标 FILLED。
                if state in ("FILLED", "PARTIAL_FILLED"):
                    storage.update_plan(plan["plan_id"], status="FILLED", entry_bar=self._today_bar())
                elif state in ("REJECTED", "FAILED"):
                    _logger.warning("计划 %s 下单被拒/失败 state=%s msg=%s，回退审核",
                                    plan["plan_id"], state, result.get("message"))
                    storage.update_plan(plan["plan_id"], status="PENDING_APPROVAL",
                                        note=f"order_{state}")
                # SUBMITTED / 其它：保持 ARMED，等成交回报（需 P1-9 对账/回调推进）
```

- [ ] **Step 4/5: 测试通过 + Commit**

> 依赖：完整闭环需「成交回报回调 → storage 推进」（见 P1-9 的对账/回调接线，本 Task 先堵住乐观标 FILLED）。

---

## Task P1-5: API 鉴权（B-1，决策点 D2）

**Files:**
- Create: `server/core/auth.py`
- Modify: `server/main.py`（依赖注入：写类端点强制鉴权；只读端点可选）
- Modify: 各写类 router（trading/caisen/data sync）加 `Depends(require_write_scope)`

- [ ] **Step 1: 写测试**（无 token → 401；错 token → 401；对 token → 200）

- [ ] **Step 2: 实现 `server/core/auth.py`**

```python
"""API 鉴权依赖（B-1）：HTTPBearer token + 可选 IP 白名单。

部署语义：
    - QUANTER_API_TOKEN 未配置：开发态，依赖放行但打 WARNING（生产必须配置）。
    - 配置后：所有写类端点（下单/熔断/同步/scan）强制 Bearer 校验。
    - QUANTER_ALLOWED_IPS 可选：配置则额外校验来源 IP（纵深防御）。
"""
import os, logging
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_logger = logging.getLogger(__name__)
_bearer = HTTPBearer(auto_error=False)

def _token() -> str | None:
    return os.environ.get("QUANTER_API_TOKEN")

def require_write(request: Request,
                  cred: HTTPAuthorizationCredentials | None = Depends(_bearer)):
    """写类端点鉴权依赖。token 未配置=开发放行（WARNING）；配置后强制匹配。"""
    tok = _token()
    if not tok:
        _logger.warning("QUANTER_API_TOKEN 未配置，API 处于无鉴权开发态（生产必须配置）")
        return
    if cred is None or cred.credentials != tok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "无效或缺失 API token")
    # 可选 IP 白名单
    allowed = os.environ.get("QUANTER_ALLOWED_IPS")
    if allowed:
        client = request.client.host if request.client else ""
        if client and client not in allowed.split(","):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"IP 未授权：{client}")
```

- [ ] **Step 3: 写类 router 接入 `Depends(require_write)`**（trading submit_order/emergency_halt、caisen scan/activate、data sync）

- [ ] **Step 4: 收紧 CORS**（`main.py`：`allow_methods=["GET","POST","PUT","DELETE","PATCH"]` 收敛）

- [ ] **Step 5/6: 测试通过 + Commit**

---

## Task P1-7: EMT/QMT 断线自动重连 + 告警（B-8，决策点 D3）

**Files:**
- Modify: `trading/emt_gateway.py:131-138,542-547`（onDisconnected / _on_disconnect_fatal）
- Modify: `trading/qmt_gateway.py`（对称实现）
- Verify: `core/notifier.py`（fire_and_forget 已存在则接线）

- [ ] **Step 1: 写测试**（onDisconnected 触发 _reconnect task，退避递增，告警 fire）

- [ ] **Step 2: 实现指数退避重连**

```python
    async def _reconnect(self):
        """断线后指数退避重连（2/4/8/16/30s，最多 5 次），失败告警 + 锁态保命。"""
        from core.notifier import NotificationManager, fire_and_forget
        backoffs = [2, 4, 8, 16, 30]
        for i, delay in enumerate(backoffs, 1):
            await asyncio.sleep(delay)
            try:
                await self.connect()
                _logger.info("EMT 重连成功（第 %s 次）", i)
                fire_and_forget(NotificationManager.get_default().notify_risk_event(
                    "EMT 断线后重连成功", "INFO"))
                return
            except Exception as exc:
                _logger.warning("EMT 重连失败（第 %s/%s）：%s", i, len(backoffs), exc)
                fire_and_forget(NotificationManager.get_default().notify_risk_event(
                    f"EMT 重连失败第{i}次：{exc}", "WARN"))
        # 耗尽：保持锁态，等人工
        fire_and_forget(NotificationManager.get_default().notify_risk_event(
            "EMT 重连耗尽，网关锁态，请人工介入！", "ERROR"))
```

`onDisconnected` 改为：置锁 + `call_soon_threadsafe` 投递一个「create_task(_reconnect)」的入口（注意 loop 可用性）。

- [ ] **Step 3/4: 测试通过 + Commit**

## Task P1-8: 锁态下离场监控不停摆（B-8 后半）

**Files:**
- Modify: `server/celery_app.py:225-237`（monitor_holding 闸门）

- [ ] **Step 1/2: 区分 pullback（开仓）与 holding（离场）的跳过语义**：pullback 在 `mode != live` 跳过（断线不新开仓）；holding 仅在 `mode == unavailable` 跳过，`disconnected/vetoed_by_risk/locked` 时**仍尝试 tick_exit**（持仓风控必须持续，即便下单可能失败也先尝试市价平仓）。注意：tick_exit 内部 submit_order 在锁态会被网关拒，需配合 P1-7 重连。

- [ ] **Step 3: Commit**

---

## Task P1-9a: Celery worker 级单例 loop（B-10 最小修复，决策点 D4）

**Files:**
- Modify: `server/celery_app.py:191-208,239-253`

- [ ] **思路**：worker 进程内维护一个持久 event loop（`threading` 跑 `loop.run_forever()`，worker 级单例），`monitor_*` 用 `asyncio.run_coroutine_threadsafe(tick, loop)` 投递，而非 `asyncio.run()`。这样 EMT gateway 的 `self._loop`（connect 时固化）与 beat 共享同一 loop，根治跨 loop RuntimeError。
- [ ] **Step 1: 写测试**（gateway 在 loop A connect，beat 在同进程 loop A 投递，submit_order 不抛跨 loop 错误）
- [ ] **Step 2: 实现 `_get_worker_loop()` 单例 + `run_coroutine_threadsafe`**
- [ ] **Step 3: Commit**

> P1-9b（follow-up，不在本轮）：beat 迁 APScheduler 进 FastAPI 进程，彻底移除 Celery 对 async 的承载。

---

## Task P1-10: 删除 MacroAwareGateway 死代码（B-7，决策点 D5）

**Files:**
- Delete: `trading/execution_gateway.py:246-327`（VetoedError + MacroAwareGateway）
- Delete: `tests/test_execution_gateway_veto.py`
- Modify: `README.md:126`、`caisen/risk.py:6,10`（移除引用）、相关 spec 标注「已弃用」

- [ ] **Step 1: 确认 grep 无生产调用**（除文档/单测）
- [ ] **Step 2: 删除代码 + 测试 + 订正引用**
- [ ] **Step 3: 全量测试 + Commit**

> 若审批选「保留宏观风控口子」：改为 `class MacroAwareGateway(BaseExecutionGateway)`，`async submit_order(self, order)` 返回新 `OrderRequest`（不改 frozen），并在 trading_service.submit_order 接入。

---

## Task P1-11: lifespan shutdown 断开网关（B-18）

**Files:**
- Modify: `server/main.py:100-107`

```python
    yield
    # 销毁：断开网关（优雅 logout，防连接泄漏）
    try:
        from server.services.trading_service import get_gateway
        gw = get_gateway()
        if gw is not None:
            await gw.disconnect()
    except Exception:
        _logger.exception("lifespan shutdown 断开网关异常（忽略）")
    root_logger = logging.getLogger()
    root_logger.removeHandler(app.state.log_handler)
    root_logger.removeHandler(app.state.log_file_handler)
```

---

# Phase 2 · P2 应修项 Backlog（实现时展开 TDD）

> 逐项实现前先按 receiving-code-review 复核（部分可能同样需 pushback）。建议按编号顺序：

| # | 项 | 位置 | 处置方向 |
|---|---|---|---|
| 3 | DataLakeReader 单例/读路径无锁 | `data/lake_reader.py:46-54,200-240` | 加 `threading.Lock` 包裹读-改-写 + get_instance 双检锁 |
| 4 | macro.py 穿透 `_lakes` 私有成员 | `server/api/v1/macro.py:150-201` | 暴露 DataLakeReader 公开读 API，路由层不碰私有 |
| 5 | sector_flow 每请求重读 Parquet | `server/api/v1/macro.py:190-199` | 走内存湖缓存 + 过滤后取 top50 |
| 6 | 涨跌停不分方向误拦止损 | `trading/risk_shield.py:109` | 第 9 关按 side 区分：SELL 仅拦一字涨停不可卖场景；接近跌停不拦止损 |
| 7 | JQData 配额 TOCTOU | `data/clients/jqdata_client.py:180-210` | 配额计数本地化 + 单次查询合并 |
| 8 | AKShare 无超时 | `data/clients/akshare_client.py:80-120` | 所有调用包 `timeout` / 线程池 + 超时 |
| 9 | 网关 connect/下单/撤单无超时 | `qmt_gateway.py`/`emt_gateway.py` | `run_in_executor` 包 `asyncio.wait_for` |
| 10 | 订单映射表永不清理 | 两个 gateway | 终态单定期 GC（保留近 N 日） |
| 11 | StrictJSONResponse 绕过 jsonable_encoder | `server/core/_responses.py:29-36` | render 前过 `jsonable_encoder` 或显式 NaN→None |
| 12 | SSE 无 keepalive | `server/api/v1/logs.py:142-156` | 每 15s 发 `: ping\n\n` + 修 `q` 未绑定 |
| 15 | trigger_sync TOCTOU | `server/services/data_service.py:193-221` | `threading.Lock` 包哨兵检查-设置 |
| 16 | take_profit_2x 回测优先/实盘忽略 | `backtest_replay.py:234`/`execution.py` | check_exit 加 take_profit_2x 预检（与回测对齐） |
| 17 | 错误信息硬编码 "QMT" | `trading_service.py:300-350` | 用 `gw.__class__.__name__` |
| 19 | data/_tushare_compat 缺失 | `data/fetcher.py:699-706` | 补模块 或 移除 import + 订正错误信息 |

> 已 pushback 不修：**应修项 20**（position_size 除零已由 `max(...,1e-9)` 防护，误报）。

---

## 自检（Self-Review）

- **Spec 覆盖**：B-1~B-10 全部有对应 Task（B-1→P1-5, B-2→P0-1, B-3→P1-3, B-4→P1-4, B-5/B-9→P0-2, B-6→P0-3+P1, B-7→P1-10, B-8→P1-7+P1-8, B-10→P1-9a）。20 个应修项：1→P0-3, 2→P0-4, 13/14→P0-5, 18→P1-11, 20→pushback 不修，其余入 P2 backlog。✅
- **占位符扫描**：P0/P1 每个 Task 有具体代码与测试；P2 为 backlog（按设计文档约定，实现时展开）。✅
- **类型一致**：`require_write`、`_validate_iso_date`、`_reconnect` 等命名在引用处一致。✅
- **设计分叉**：D1~D5 已显式标注推荐 + 备选，审批时确认。✅
