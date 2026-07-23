# 模拟盘端到端演练 Phase 1 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 miniQMT 模拟盘端到端跑通 Phase 1 骨架——数据实时性检查 + eod_plan@19:00 + stop_prices 注入 + 30s 监控 + 成交回调链路（日志+钉钉+挂止盈）+ 持仓盈亏播报。

**Architecture:** 数据侧新增 `data/freshness.py`（交易日历+数据湖最新日期比对）+ 双检查点重采熔断；交易侧改造 `trading/engine.py`（cron 挪移/stop_prices 注入/30s interval/成交 handler）+ `trading/__main__.py`（connect+注册回调）+ `trading_service`（成交补写日志/盈亏计算）+ `infra/notifier`（成交通知）。止盈 Phase1 用简化版（单一固定止盈单），Phase2 升级分级。

**Tech Stack:** Python 3.10（.venv310）、xtquant、APScheduler、pandas/parquet、pytest、aiohttp（钉钉 webhook）。

**对应 spec:** `docs/superpowers/specs/2026-07-23-auto-trading-rehearsal-design.md`（§6 Phase 1 + §11 验收）。

## Global Constraints

- **全中文**：对话/文档/代码注释全部中文（CLAUDE.md 铁律）。注释要讲 Why（交易物理意图）。
- **commit 需研究员授权**：CLAUDE.md 规矩——本 plan 每任务末尾的 commit 步骤，**仅在研究员明确说"提交/commit"时执行**；未授权时跳过 commit、保留 working tree 改动待批量提交。
- **dry_run 默认**：`AUTO_TRADE_MODE=dry_run` 为红线默认；切 live 需显式设 env（Phase 1 不切 live）。
- **守 Layer2 spec §7 六铁律**：不破坏 trading 五模块单向依赖（types/compute/state/io/orchestrate）。新增纯决策函数落 `compute/`，副作用壳落 `io/`。
- **registry key ≠ parquet 文件名**：`sync_incremental --keys daily`（registry key），不是 `a_shares_daily`（parquet 名）。`daily` → `data_lake/a_shares_daily.parquet`。
- **钉钉通知两套通道**：观测播报走 `broadcast/push.py`（dws send-by-bot，凭据 `BROADCAST_GROUP_ID`+`*_BOT_ROBOT_CODE`）；风控/成交通知走 `infra/notifier.py`（webhook 加签，凭据 `DINGTALK_WEBHOOK`+`DINGTALK_SECRET`，缺一跳过）。本 plan 成交通知用后者。
- **Python 环境**：`.venv310`（xtquant 仅 3.10）；测试 `pytest`。
- **qmt_market_data 路径**：Layer2 重构后行情模块可能在 `broker.qmt_quote`（engine.py 现有 import 为 `qmt_market_data`）；Task8/10/12 涉及时确认实际导入路径，与 engine.py 顶部 import 保持一致。

---

## File Structure

| 文件 | 类型 | 职责 |
|---|---|---|
| `trading/calendar.py` | 改 | 新增 `expected_latest_trade_day(now)`（期望最新交易日） |
| `data/freshness.py` | 新 | 数据实时性检查核心（期望日 vs 数据湖最新日） |
| `scripts/run_data_check.py` | 新 | 数据检查点①②入口（重采熔断） |
| `scripts/run_data_check_t1.bat` / `_t2.bat` | 新 | schtasks 包装（17:00 查T-1 / 18:30 查T） |
| `broadcast/brief_data.py` | 改 | freshness 结果并入 data bot 播报 |
| `trading/engine.py` | 改 | eod cron→19:00、stoploss 注入 stop_prices+30s interval、新增 `_handle_order_update` |
| `trading/__main__.py` | 改 | gw connect + 注册成交回调 |
| `server/services/trading_service.py` | 改 | `record_live_trade` 成交回报补写、`get_positions` 盈亏计算 |
| `infra/notifier.py` | 改 | 新增 `notify_trade_event`（成交通知） |
| `tests/trading/test_calendar_expected.py` | 新 | TDD Task1 |
| `tests/data/test_freshness.py` | 新 | TDD Task2 |
| `tests/scripts/test_data_check.py` | 新 | TDD Task3 |
| `tests/broadcast/test_brief_data_freshness.py` | 新 | TDD Task5 |
| `tests/trading/test_engine_stoploss_inject.py` | 新 | TDD Task7 |
| `tests/infra/test_notifier_trade.py` | 新 | TDD Task9 |
| `tests/trading/test_engine_order_update_handler.py` | 新 | TDD Task10 |
| `tests/services/test_positions_pnl.py` | 新 | TDD Task12 |

---

## Task 1: 交易日历——期望最新交易日

**Files:**
- Modify: `trading/calendar.py`（新增函数，不动现有 `is_trading_day`/`is_intraday_session`）
- Test: `tests/trading/test_calendar_expected.py`

**Interfaces:**
- Consumes: `is_trading_day(date_str)`（现有，`calendar.py:62`）
- Produces: `expected_latest_trade_day(now: datetime) -> str`（YYYY-MM-DD）

- [ ] **Step 1: 写失败测试**

```python
# tests/trading/test_calendar_expected.py
"""期望最新交易日计算（数据实时性检查的基石）。"""
from datetime import datetime, time
from unittest.mock import patch
from trading.calendar import expected_latest_trade_day


def test_after_close_on_trading_day_returns_today():
    """盘后(>=15:00)且今天是交易日 → 期望今天（T 日数据应已落湖）。"""
    now = datetime(2026, 7, 23, 18, 30)  # 周四 18:30
    with patch("trading.calendar.is_trading_day", return_value=True):
        assert expected_latest_trade_day(now) == "2026-07-23"


def test_before_close_returns_previous_trade_day():
    """盘中或盘前 → 期望上一个交易日（T-1 数据应齐全）。"""
    now = datetime(2026, 7, 23, 10, 0)  # 周四盘中
    with patch("trading.calendar.is_trading_day", side_effect=lambda d: d == "2026-07-22"):
        assert expected_latest_trade_day(now) == "2026-07-22"


def test_weekend_rolls_back_to_friday():
    """周末 → 期望上周五（回溯找上一个交易日）。"""
    now = datetime(2026, 7, 25, 12, 0)  # 周六
    with patch("trading.calendar.is_trading_day", side_effect=lambda d: d == "2026-07-24"):
        assert expected_latest_trade_day(now) == "2026-07-24"


def test_non_trading_day_after_close_rolls_back():
    """节假日盘后 → 回溯到节前最后一个交易日。"""
    now = datetime(2026, 10, 2, 18, 0)  # 国庆假
    with patch("trading.calendar.is_trading_day", return_value=False):
        # 全部非交易日 → 兜底返 today（极端，长假中无交易日）
        assert expected_latest_trade_day(now) == "2026-10-02"
```

- [ ] **Step 2: 验证测试失败**

Run: `pytest tests/trading/test_calendar_expected.py -v`
Expected: FAIL `ImportError: cannot import name 'expected_latest_trade_day'`

- [ ] **Step 3: 实现**

在 `trading/calendar.py` 末尾追加：

```python
# 物理意图：数据实时性检查的期望锚点——盘后查 T 数据是否落湖，盘前查 T-1 是否齐全。
# 决策口径：now >= 15:00 且今天是交易日 → 期望今天（收盘数据清算后应落湖）；
#           否则 → 回溯最近一个交易日（最多 10 自然日，覆盖长假）。
def expected_latest_trade_day(now: datetime) -> str:
    """期望最新交易日（数据湖应含此日完整数据）。

    Args:
        now: 当前时刻。

    Returns:
        YYYY-MM-DD。盘后交易日→今天；否则→上一个交易日；全非交易日兜底 today。
    """
    from datetime import timedelta
    today = now.strftime("%Y-%m-%d")
    # 盘后（15:00 之后）且今天交易日 → 期望今天
    if now.time() >= time(15, 0) and is_trading_day(today):
        return today
    # 否则回溯找上一个交易日（最多 10 自然日，覆盖长假 + 周末）
    for i in range(1, 11):
        prev = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        if is_trading_day(prev):
            return prev
    return today  # 兜底：窗口内无交易日（极端长假），返 today 让检查自然 FAIL 告警
```

（`datetime`/`time` 若 calendar.py 顶部未导入，补 `from datetime import datetime, time`。）

- [ ] **Step 4: 验证测试通过**

Run: `pytest tests/trading/test_calendar_expected.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: commit（需研究员授权）**

```bash
git add trading/calendar.py tests/trading/test_calendar_expected.py
git commit -m "feat(calendar): 新增 expected_latest_trade_day 数据实时性检查锚点"
```

---

## Task 2: 数据实时性检查核心（data/freshness.py）

**Files:**
- Create: `data/freshness.py`
- Test: `tests/data/test_freshness.py`

**Interfaces:**
- Consumes: `trading.calendar.expected_latest_trade_day`（Task1）；pandas `read_parquet`
- Produces: `check_freshness(key, expected_date, *, lake_dir="data_lake") -> FreshnessResult`；`FreshnessResult`（dataclass）

- [ ] **Step 1: 写失败测试**

```python
# tests/data/test_freshness.py
"""数据实时性检查核心：期望日 vs 数据湖最新日比对。"""
from data.freshness import check_freshness, FreshnessResult


def _make_daily_parquet(tmp_path, last_date):
    """造一个 a_shares_daily 风格 parquet（MultiIndex date,symbol），最新日 = last_date。"""
    import pandas as pd
    dates = pd.date_range("2026-07-01", last_date, freq="B")
    df = pd.DataFrame({
        "date": dates.tolist() * 2,
        "symbol": ["000001.SZ"] * len(dates) + ["000002.SZ"] * len(dates),
        "open": 10.0, "close": 10.0, "high": 10.0, "low": 10.0, "volume": 1000, "amount": 10000.0,
    })
    df = df.set_index(["date", "symbol"])
    p = tmp_path / "a_shares_daily.parquet"
    df.to_parquet(p)
    return p


def test_freshness_pass_when_latest_meets_expected(tmp_path):
    """数据湖最新日 >= 期望日 → PASS。"""
    _make_daily_parquet(tmp_path, "2026-07-23")
    r = check_freshness("daily", "2026-07-23", lake_dir=str(tmp_path))
    assert r.ok is True
    assert r.latest_date == "2026-07-23"


def test_freshness_fail_when_latest_stale(tmp_path):
    """数据湖最新日 < 期望日 → FAIL（T 日数据未落湖）。"""
    _make_daily_parquet(tmp_path, "2026-07-22")
    r = check_freshness("daily", "2026-07-23", lake_dir=str(tmp_path))
    assert r.ok is False
    assert "2026-07-22" in r.message


def test_freshness_fail_when_parquet_missing(tmp_path):
    """parquet 不存在 → FAIL（不猜、不崩）。"""
    r = check_freshness("daily", "2026-07-23", lake_dir=str(tmp_path))
    assert r.ok is False
    assert "缺失" in r.message or "不存在" in r.message
```

- [ ] **Step 2: 验证测试失败**

Run: `pytest tests/data/test_freshness.py -v`
Expected: FAIL `ModuleNotFoundError: No module named 'data.freshness'`

- [ ] **Step 3: 实现**

```python
# data/freshness.py
"""数据实时性检查核心。

物理意图：现状 data bot 只看 parquet mtime 新不新鲜（被动），会被「刚重写但内容是旧数据」
骗过。本模块改为「比对交易日历期望日 vs 数据湖内容最新日」——真正回答「T/T-1 数据到没到」。

边界（Grill Me）：
- 绝不猜价/猜日：parquet 缺失或读失败 → FAIL + 告警，不静默返 PASS。
- 大文件 read_parquet 开销：每日检查点只跑 1-2 次，单次 ~1.75s（455MB）可接受；
  不复用内存湖（DataLakeReader 可能未载入该 key）。
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

# registry key → parquet 文件名映射（与 config/registry.py 的 lake_key 口径一致）
# 物理意图：registry 用语义 key（daily），落湖用文件名（a_shares_daily），此处对齐。
_KEY_TO_PARQUET = {
    "daily": "a_shares_daily.parquet",
    "moneyflow": "moneyflow.parquet",
    "margin": "margin.parquet",
    # 按需扩展：颈线法核心依赖以 daily 为主，其余检查点②按需追加
}


@dataclass(frozen=True)
class FreshnessResult:
    """实时性检查结果（不可变，便于聚合与断言）。"""
    key: str
    ok: bool                       # True=数据够新；False=缺失/陈旧
    latest_date: str | None        # 数据湖内容最新日（YYYY-MM-DD）；缺失则 None
    expected_date: str             # 期望日（比对基准）
    message: str                   # 人类可读结论（含告警/排查信息）


def check_freshness(
    key: str,
    expected_date: str,
    *,
    lake_dir: str = "data_lake",
) -> FreshnessResult:
    """检查某数据集最新日期是否 >= 期望交易日。

    Args:
        key:           registry 语义 key（如 "daily"），非 parquet 文件名。
        expected_date: 期望最新交易日（YYYY-MM-DD，来自 expected_latest_trade_day）。
        lake_dir:      数据湖目录（默认 data_lake；测试注入 tmp_path）。

    Returns:
        FreshnessResult：ok=True 当且仅当 latest_date >= expected_date。
    """
    fname = _KEY_TO_PARQUET.get(key, f"{key}.parquet")
    path = Path(lake_dir) / fname
    if not path.exists():
        msg = f"{key}({fname}) 缺失：{path} 不存在，期望 {expected_date} 数据未落湖"
        logger.warning(msg)
        return FreshnessResult(key, ok=False, latest_date=None,
                               expected_date=expected_date, message=msg)

    # 读最新日期：直接 read_parquet 取 date index max（检查点低频，开销可接受）
    try:
        import pandas as pd
        df = pd.read_parquet(path)
        idx = df.index
        # MultiIndex(date, symbol) 或 DatetimeIndex
        if isinstance(idx, pd.MultiIndex) and "date" in idx.names:
            dates = idx.get_level_values("date")
        else:
            dates = idx
        latest = str(pd.Timestamp(dates.max()).date())
    except Exception as exc:
        msg = f"{key} 读最新日期异常：{exc}（parquet 损坏？）"
        logger.exception(msg)
        return FreshnessResult(key, ok=False, latest_date=None,
                               expected_date=expected_date, message=msg)

    if latest >= expected_date:
        return FreshnessResult(key, ok=True, latest_date=latest,
                               expected_date=expected_date,
                               message=f"{key} 最新 {latest} >= 期望 {expected_date}，PASS")
    msg = (f"{key} 数据陈旧：最新 {latest} < 期望 {expected_date}，"
           f"T 日数据未落湖（检查 Tushare 增量采集是否成功）")
    logger.warning(msg)
    return FreshnessResult(key, ok=False, latest_date=latest,
                           expected_date=expected_date, message=msg)
```

- [ ] **Step 4: 验证测试通过**

Run: `pytest tests/data/test_freshness.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: commit（需研究员授权）**

```bash
git add data/freshness.py tests/data/test_freshness.py
git commit -m "feat(data): 新增 freshness 实时性检查（交易日历vs数据湖最新日，替换mtime被动统计）"
```

---

## Task 3: 数据检查点入口（重采熔断）

**Files:**
- Create: `scripts/run_data_check.py`
- Test: `tests/scripts/test_data_check.py`

**Interfaces:**
- Consumes: `data.freshness.check_freshness`（Task2）；`trading.calendar.expected_latest_trade_day`（Task1）；`scripts.sync_incremental.sync_one_key`
- Produces: `run_check(checkpoint: str, *, keys=("daily",), deadline_hour=20) -> dict`（CLI 入口 `main()`）

**说明：** 检查点①@17:00 查 T-1（历史应齐全，FAIL 仅告警不熔断）；检查点②@18:30 查 T（FAIL 触发重采窗口，每 15min 重采至 20:00，仍 FAIL 熔断 eod_plan）。

- [ ] **Step 1: 写失败测试**

```python
# tests/scripts/test_data_check.py
"""数据检查点：①查T-1告警 / ②查T重采熔断。"""
from unittest.mock import patch, MagicMock
from scripts.run_data_check import run_check


def test_checkpoint1_t1_pass_no_alert():
    """检查点①：T-1 齐全 → 返 OK，不熔断。"""
    from data.freshness import FreshnessResult
    with patch("scripts.run_data_check.check_freshness",
               return_value=FreshnessResult("daily", True, "2026-07-22", "2026-07-22", "PASS")):
        r = run_check("t1", keys=("daily",))
    assert r["ok"] is True
    assert r["melted"] is False  # 检查点①永不熔断（T-1 历史缺不影响 T+1）


def test_checkpoint2_t_fail_triggers_resync_until_pass():
    """检查点②：T 未到位 → 重采，重采后 PASS → 不熔断。"""
    from data.freshness import FreshnessResult
    fail = FreshnessResult("daily", False, "2026-07-22", "2026-07-23", "陈旧")
    ok = FreshnessResult("daily", True, "2026-07-23", "2026-07-23", "PASS")
    sync = MagicMock(return_value=(True, "ok"))
    with patch("scripts.run_data_check.check_freshness", side_effect=[fail, ok]), \
         patch("scripts.run_data_check.sync_one_key", sync), \
         patch("scripts.run_data_check._now", side_effect=["18:30", "18:45"]):
        r = run_check("t2", keys=("daily",), deadline_hour=20)
    assert r["ok"] is True
    assert r["melted"] is False
    assert sync.call_count == 1  # 重采一次后 PASS


def test_checkpoint2_t_fail_after_deadline_melts():
    """检查点②：超时仍 FAIL → 熔断（不交易不自欺）。"""
    from data.freshness import FreshnessResult
    fail = FreshnessResult("daily", False, "2026-07-22", "2026-07-23", "陈旧")
    with patch("scripts.run_data_check.check_freshness", return_value=fail), \
         patch("scripts.run_data_check.sync_one_key", return_value=(False, "积分不足")), \
         patch("scripts.run_data_check._now", return_value="20:30"):  # 已超 20:00
        r = run_check("t2", keys=("daily",), deadline_hour=20)
    assert r["ok"] is False
    assert r["melted"] is True
```

- [ ] **Step 2: 验证测试失败**

Run: `pytest tests/scripts/test_data_check.py -v`
Expected: FAIL `ModuleNotFoundError: No module named 'scripts.run_data_check'`

- [ ] **Step 3: 实现**

```python
# scripts/run_data_check.py
"""数据实时性检查点入口（schtasks 调度）。

两个检查点（brainstorm 决策 A 双检查点）：
  ① @17:00 查 T-1：历史数据应齐全，FAIL 仅告警（不影响 T+1 计划的 T 日数据）。
  ② @18:30 查 T：T 日数据是 T+1 计划输入，FAIL → 重采窗口（每15min重采至 deadline），
                 仍 FAIL → 熔断 eod_plan（不交易不自欺，绝不用 T-1 兜底算 T+1＝前视偏差）。

退出码：0=PASS/告警；2=熔断（eod_plan 应据此跳过，schtasks 层另处理）。
"""
from __future__ import annotations
import sys
import logging
from datetime import datetime

from data.freshness import check_freshness
from trading.calendar import expected_latest_trade_day

logger = logging.getLogger(__name__)


def _now() -> str:
    """当前 HH:MM（测试可 patch）。物理意图：判断是否超重采截止时间。"""
    return datetime.now().strftime("%H:%M")


def _resync_key(key: str) -> tuple[bool, str]:
    """重采单个数据集（调 sync_incremental.sync_one_key）。

    ⚠️ key 是 registry 语义 key（如 "daily"），不是 parquet 文件名（如 "a_shares_daily"）。
    """
    from scripts.sync_incremental import sync_one_key
    import io
    today_str = datetime.today().strftime("%Y-%m-%d")
    return sync_one_key(key, today_str, fallback_years=3, max_days=None, log=io.StringIO())


def _alert(msg: str, level: str = "WARN") -> None:
    """钉钉告警（fire_and_forget，失败软降级）。"""
    try:
        from core.notifier import NotificationManager, fire_and_forget
        fire_and_forget(NotificationManager.get_default().notify_risk_event(msg, level))
    except Exception:
        logger.exception("告警发送失败（不影响检查主流程）")


def run_check(
    checkpoint: str,
    *,
    keys: tuple[str, ...] = ("daily",),
    deadline_hour: int = 20,
) -> dict:
    """执行一个数据检查点。

    Args:
        checkpoint: "t1"（查T-1，告警不熔断）/ "t2"（查T，重采熔断）。
        keys:       检查的数据集 registry key 列表。
        deadline_hour: t2 重采截止小时（超过即熔断，默认 20 点）。

    Returns:
        {"ok":bool, "melted":bool, "details":[...]}
    """
    now = datetime.now()
    # t1=盘前期望T-1；t2=盘后期望T（expected_latest_trade_day 据 now.time 自动判定）
    expected = expected_latest_trade_day(now)
    results = [check_freshness(k, expected) for k in keys]
    all_ok = all(r.ok for r in results)

    if checkpoint == "t1":
        # 检查点①：T-1 历史缺仅告警（不熔断——T-1 缺不影响当日 T+1 计划的 T 日数据输入）
        if not all_ok:
            _alert(f"【数据检查点①T-1】部分数据集陈旧/缺失："
                   f"{[r.message for r in results if not r.ok]}，请排查历史采集", "WARN")
        return {"ok": all_ok, "melted": False,
                "details": [r.message for r in results]}

    # checkpoint == "t2"：查 T，FAIL 触发重采窗口
    if all_ok:
        return {"ok": True, "melted": False, "details": [r.message for r in results]}

    # 重采循环：每轮重采失败 key，重检，直到 PASS 或超 deadline
    while _now() < f"{deadline_hour:02d}:00":
        for r in results:
            if r.ok:
                continue
            logger.info("重采 %s（期望 %s，当前最新 %s）", r.key, r.expected_date, r.latest_date)
            ok, msg = _resync_key(r.key)
            if not ok:
                _alert(f"【数据重采】{r.key} 重采失败：{msg}", "WARN")
        # 重检
        results = [check_freshness(k, expected) for k in keys]
        if all(r.ok for r in results):
            return {"ok": True, "melted": False, "details": [r.message for r in results]}

    # 超 deadline 仍 FAIL → 熔断
    melt_msg = (f"【数据熔断】检查点②超时({deadline_hour}:00)仍缺 T 日数据："
                f"{[r.message for r in results if not r.ok]}，eod_plan 将跳过（不交易不自欺）")
    _alert(melt_msg, "ERROR")
    logger.error(melt_msg)
    return {"ok": False, "melted": True, "details": [r.message for r in results]}


def main() -> None:
    """schtasks 入口：python -m scripts.run_data_check t1|t2。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) < 2 or sys.argv[1] not in ("t1", "t2"):
        print("用法: python -m scripts.run_data_check t1|t2", file=sys.stderr)
        sys.exit(1)
    r = run_check(sys.argv[1])
    # 熔断用退出码 2 区分（eod_plan/schtasks 据此跳过）
    sys.exit(2 if r["melted"] else 0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 验证测试通过**

Run: `pytest tests/scripts/test_data_check.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: commit（需研究员授权）**

```bash
git add scripts/run_data_check.py tests/scripts/test_data_check.py
git commit -m "feat(data): 数据检查点①②入口(重采熔断,t1告警/t2熔断eod_plan)"
```

---

## Task 4: schtasks 包装（bat，配置非TDD）

**Files:**
- Create: `scripts/run_data_check_t1.bat`、`scripts/run_data_check_t2.bat`
- Create/Modify: `scripts/manage_ops_schtasks.py`（注册两个新任务）

**说明：** 沿用 QuanterDataBrief 模式（cd /d 锁根 + 调 .venv310 python）。

- [ ] **Step 1: 写 bat**

```bat
:: scripts/run_data_check_t1.bat
@echo off
cd /d C:\Users\yzzhan\Desktop\quanter
call .venv310\Scripts\activate.bat
python -m scripts.run_data_check t1
```

```bat
:: scripts/run_data_check_t2.bat
@echo off
cd /d C:\Users\yzzhan\Desktop\quanter
call .venv310\Scripts\activate.bat
python -m scripts.run_data_check t2
```

- [ ] **Step 2: 在 manage_ops_schtasks.py 注册两个任务**

在 `manage_ops_schtasks.py` 的任务映射里新增（参照 QuanterDataBrief 的 17:00 模式）：

```python
# 17:00 检查点①（查T-1）
("QuanterDataCheckT1", "17:00", "scripts\\run_data_check_t1.bat"),
# 18:30 检查点②（查T，重采熔断）
("QuanterDataCheckT2", "18:30", "scripts\\run_data_check_t2.bat"),
```

- [ ] **Step 3: 注册并验证**

Run（研究员在场时执行，需 Windows schtasks 权限）:
```bash
python scripts/manage_ops_schtasks.py
schtasks /Query /TN QuanterDataCheckT1
schtasks /Query /TN QuanterDataCheckT2
```
Expected: 两个任务都存在，下次运行时间 17:00 / 18:30。

- [ ] **Step 4: 手动触发验证（dry run）**

```bash
python -m scripts.run_data_check t1
```
Expected: 输出检查结果，退出码 0（当前数据应齐全）。

- [ ] **Step 5: commit（需研究员授权）**

```bash
git add scripts/run_data_check_t1.bat scripts/run_data_check_t2.bat scripts/manage_ops_schtasks.py
git commit -m "chore(ops): 注册数据检查点schtasks(17:00查T-1/18:30查T重采熔断)"
```

---

## Task 5: data bot 播报并入实时性结果

**Files:**
- Modify: `broadcast/brief_data.py`（`build_data_brief` 增 freshness 段）
- Test: `tests/broadcast/test_brief_data_freshness.py`

**Interfaces:**
- Consumes: `data.freshness.check_freshness`（Task2）；`trading.calendar.expected_latest_trade_day`（Task1）
- Produces: `build_data_brief(date, *, datasets, freshness=None)` 多一段渲染

- [ ] **Step 1: 写失败测试**

```python
# tests/broadcast/test_brief_data_freshness.py
"""data bot 播报并入实时性检查结果。"""
from broadcast.brief_data import build_data_brief
from data.freshness import FreshnessResult


def test_brief_includes_freshness_section_when_provided():
    """传入 freshness 结果 → 播报含实时性段。"""
    datasets = [{"key": "daily", "name": "A股日线", "status": "healthy"}]
    freshness = [FreshnessResult("daily", True, "2026-07-23", "2026-07-23", "PASS")]
    result = build_data_brief("2026-07-23", datasets=datasets, freshness=freshness)
    assert "实时性" in result.text or "T-1" in result.text or "PASS" in result.text


def test_brief_works_without_freshness_backward_compat():
    """未传 freshness → 向后兼容（原健康度播报不破坏）。"""
    datasets = [{"key": "daily", "name": "A股日线", "status": "healthy"}]
    result = build_data_brief("2026-07-23", datasets=datasets)
    assert result.text  # 仍正常产出
```

- [ ] **Step 2: 验证测试失败**

Run: `pytest tests/broadcast/test_brief_data_freshness.py -v`
Expected: FAIL `TypeError: build_data_brief() got an unexpected keyword argument 'freshness'`

- [ ] **Step 3: 实现**

修改 `broadcast/brief_data.py` 的 `build_data_brief` 签名，新增可选 `freshness` 参数，末尾追加一段渲染：

```python
def build_data_brief(date: str, *, datasets: list[dict] | None, freshness: list | None = None) -> BriefResult:
    """...（原 docstring 保留，补：freshness 为数据实时性检查结果，None 时跳过该段）。"""
    # ... 原有健康度统计逻辑不动 ...

    # 实时性段（Task5 新增）：freshness 为 None 时向后兼容（不破坏原播报）
    freshness_lines = []
    if freshness:
        freshness_lines.append("## 📊 数据实时性")
        for fr in freshness:
            mark = "✅" if fr.ok else "⚠️"
            freshness_lines.append(f"- {mark} {fr.key}: 最新 {fr.latest_date} / 期望 {fr.expected_date}")
        freshness_lines.append("")

    # 把 freshness_lines 插入最终 text（具体插入位置据现有渲染结构调整，
    # 通常在健康度统计之后、异常清单之前）
    # ... 组装 result.text += "\n".join(freshness_lines) ...
```

（实施时 Read `brief_data.py` 确认现有 `BriefResult`/text 组装位置，把 `freshness_lines` 拼进 text。`BriefResult` 若是 dataclass，可能需追加字段或直接拼 text——以现有结构为准，保持向后兼容。）

- [ ] **Step 4: 验证测试通过**

Run: `pytest tests/broadcast/test_brief_data_freshness.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: 改 `broadcast/__main__.py` 注入 freshness**

在 `__main__.py` 的 `_fetch_data_snapshot` / data bot 分支里，调 `check_freshness` 拿结果传入 `build_data_brief`：

```python
from data.freshness import check_freshness
from trading.calendar import expected_latest_trade_day
# data bot 分支：
expected = expected_latest_trade_day(datetime.now())
freshness = [check_freshness(k, expected) for k in ("daily",)]
brief = build_data_brief(date, datasets=datasets, freshness=freshness)
```

- [ ] **Step 6: commit（需研究员授权）**

```bash
git add broadcast/brief_data.py broadcast/__main__.py tests/broadcast/test_brief_data_freshness.py
git commit -m "feat(broadcast): data bot 并入数据实时性检查结果(双口径:mtime+内容最新日)"
```

---

## Task 6: eod_plan cron 15:35 → 19:00

**Files:**
- Modify: `trading/engine.py:524`（ENGINE_EOD_PLAN_CRON 默认值）
- Modify: `.env`（同步配 ENGINE_EOD_PLAN_CRON=0 19 * * 1-5）

**说明：** 修正时序 bug（现状 @15:35 跑用的是 T-1 数据，因 T 日数据 @18:00 才落湖）。挪到 19:00 等数据落湖 + 检查点②通过。

- [ ] **Step 1: 改 cron 默认值**

`trading/engine.py:523-525`：

```python
self.sched.add_job(
    self._eod, CronTrigger.from_crontab(
        os.getenv("ENGINE_EOD_PLAN_CRON", "0 19 * * 1-5")),  # 15:35 → 19:00（等18:00增量采集+18:30检查点②）
    id="eod_plan",
)
```

注释更新：`T 日盘后 19:00 扫 T 日新突破信号（@18:00 增量采集后，避免用 T-1 数据算 T+1 计划）`。

- [ ] **Step 2: 同步 .env**

`.env` 加（或改）：`ENGINE_EOD_PLAN_CRON=0 19 * * 1-5`（`.env.example` 同步注释说明 Why 挪 19:00）。

- [ ] **Step 3: 验证 cron 注册**

```bash
python -c "from trading.engine import TradingEngine; e=TradingEngine(); jobs={j.id:str(j.trigger) for j in e.sched.get_jobs()}; print(jobs['eod_plan'])"
```
Expected: 输出含 `19:00:00`（或 `hour=19`）。

- [ ] **Step 4: commit（需研究员授权）**

```bash
git add trading/engine.py .env .env.example
git commit -m "fix(engine): eod_plan cron 15:35→19:00(等增量采集落湖,修用T-1数据算T+1计划的时序bug)"
```

---

## Task 7: stop_prices 注入（修监控空转）

**Files:**
- Modify: `trading/engine.py:641`（`_stoploss` 包装方法）
- Test: `tests/trading/test_engine_stoploss_inject.py`

**Interfaces:**
- Consumes: `trading_plan.load_plan(date)`（现有，返 `{orders:[{order:{symbol}, stop_price}]}`）；`datetime.now()`
- Produces: `_stoploss` 从活跃计划读 `{symbol: stop_price}` 注入 `stop_loss_monitor`

- [ ] **Step 1: 写失败测试**

```python
# tests/trading/test_engine_stoploss_inject.py
"""_stoploss 从活跃计划注入 stop_prices（修现状 None 空转）。"""
from unittest.mock import patch, AsyncMock
from trading.engine import TradingEngine


def test_stoploss_injects_stop_prices_from_plan():
    """有活跃计划 → _stoploss 把 symbol→stop_price 注入 monitor。"""
    eng = TradingEngine()
    plan = {"confirmed": True, "orders": [
        {"order": {"symbol": "300001.SZ", "qty": 100, "side": "buy", "price": 10.0},
         "stop_price": 9.5, "take_profit": 12.0},
    ]}
    with patch("trading.engine.trading_plan.load_plan", return_value=plan), \
         patch("trading.engine.calendar") as cal, \
         patch("trading.engine.stop_loss_monitor", new=AsyncMock()) as mon:
        import asyncio
        asyncio.get_event_loop().run_until_complete(eng._stoploss())
    # 断言 stop_prices 被注入（非 None）
    _, kwargs = mon.call_args
    assert kwargs.get("stop_prices") == {"300001.SZ": 9.5}


def test_stoploss_no_plan_injects_none():
    """无计划 → 注入空 dict（monitor 内部返 no-op，不崩）。"""
    eng = TradingEngine()
    with patch("trading.engine.trading_plan.load_plan", return_value=None), \
         patch("trading.engine.stop_loss_monitor", new=AsyncMock()) as mon:
        import asyncio
        asyncio.get_event_loop().run_until_complete(eng._stoploss())
    _, kwargs = mon.call_args
    assert kwargs.get("stop_prices") in (None, {})
```

- [ ] **Step 2: 验证测试失败**

Run: `pytest tests/trading/test_engine_stoploss_inject.py -v`
Expected: FAIL（现状 `_stoploss` 传 `stop_prices=None`，断言 `=={"300001.SZ":9.5}` 不过）

- [ ] **Step 3: 实现**

修改 `trading/engine.py:641` 的 `_stoploss`：

```python
async def _stoploss(self) -> None:
    """cron 包装：止损监控（盘中时段判定在 stop_loss_monitor 内）。

    注入 stop_prices（修现状 None 空转）：从当日活跃计划读 {symbol: stop_price}。
    无计划/未确认 → 注入空，monitor 内部返「无止损价配置」no-op（保守，不盲卖）。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    plan = trading_plan.load_plan(today)
    stop_prices = {}
    if plan and plan.get("confirmed"):
        for o in plan.get("orders", []):
            sym = o.get("order", {}).get("symbol")
            sp = o.get("stop_price")
            if sym and sp is not None:
                stop_prices[sym] = sp
    await stop_loss_monitor(stop_prices=stop_prices or None)
```

- [ ] **Step 4: 验证测试通过**

Run: `pytest tests/trading/test_engine_stoploss_inject.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: commit（需研究员授权）**

```bash
git add trading/engine.py tests/trading/test_engine_stoploss_inject.py
git commit -m "fix(engine): _stoploss 注入活跃计划stop_prices(修监控恒None空转)"
```

---

## Task 8: 监控周期 5min → 30s（IntervalTrigger + 限频实测）

**Files:**
- Modify: `trading/engine.py:532-535`（stop_loss job 注册）
- Modify: `.env`（新增 ENGINE_STOPLOSS_INTERVAL_SECONDS=30）

**说明：** cron 最小粒度是分钟，30s 必须用 `IntervalTrigger`。**限频待实测**（spec §10）：模拟盘实测 30s 连续 `get_quotes`+`query_stock_positions` 是否触发柜台限流；若限流上调到 60s。

- [ ] **Step 1: 改 job 注册为 IntervalTrigger**

`trading/engine.py` `__init__` 内 stop_loss job（原 `*/5 9-14` cron）改为：

```python
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

# stop_loss：盘中每 N 秒监控（30s 目标；env 可调，限频实测后定终值）
# Why IntervalTrigger：cron 最小粒度分钟，30s 必须 interval。时段约束交给
# stop_loss_monitor 内 calendar.is_intraday_session（9:30-11:30/13:00-15:00）。
stoploss_seconds = int(os.getenv("ENGINE_STOPLOSS_INTERVAL_SECONDS", "30"))
self.sched.add_job(
    self._stoploss,
    IntervalTrigger(seconds=stoploss_seconds),
    id="stop_loss",
)
```

- [ ] **Step 2: 限频实测（spec §10 待核实点，必做）**

在模拟盘连接状态下，跑一个临时脚本测连续调用限频：

```python
# scripts/probe_qmt_ratelimit.py（临时，测完可删）
import asyncio, time
from trading.engine import get_gateway
async def main():
    gw = get_gateway(); await gw.connect()
    from trading import qmt_market_data  # 或 broker.qmt_quote
    for i in range(20):  # 连续 20 次，每次间隔 30s，模拟 10 分钟
        t0 = time.time()
        await gw._fetch_broker_positions()
        # await qmt_market_data.get_quotes([...])  # 持仓 symbols
        print(f"[{i}] {time.time()-t0:.2f}s ok")
        await asyncio.sleep(30)
asyncio.run(main())
```

Expected: 观察是否出现限流报错（`too many`/`频率`/超时激增）。
- 若 20 次全 ok → 30s 可用，`ENGINE_STOPLOSS_INTERVAL_SECONDS=30`。
- 若出现限流 → 上调到 60，重测，定终值并在 `.env.example` 注释实测结论。

- [ ] **Step 3: 验证 interval 注册**

```bash
python -c "from trading.engine import TradingEngine; e=TradingEngine(); print({j.id:str(j.trigger) for j in e.sched.get_jobs() if j.id=='stop_loss'})"
```
Expected: 输出含 `interval` / `30`。

- [ ] **Step 4: commit（需研究员授权）**

```bash
git add trading/engine.py .env .env.example scripts/probe_qmt_ratelimit.py
git commit -m "feat(engine): stop_loss 5min→30s interval(限频实测后定终值,cron不支持秒级)"
```

---

## Task 9: 钉钉成交通知（notify_trade_event）

**Files:**
- Modify: `infra/notifier.py`（NotificationManager 新增方法）
- Test: `tests/infra/test_notifier_trade.py`

**Interfaces:**
- Consumes: `NotificationManager`（现有）；`DingTalkChannel`（现有 webhook 加签）
- Produces: `NotificationManager.notify_trade_event(symbol, direction, qty, price, *, extra="") -> list`（async）

- [ ] **Step 1: 写失败测试**

```python
# tests/infra/test_notifier_trade.py
"""成交通知（区别于风控告警 notify_risk_event）。"""
from unittest.mock import patch, AsyncMock
from infra.notifier import NotificationManager


async def test_notify_trade_event_formats_trade_info():
    """成交通知含标的/方向/量/价 + 成交前缀。"""
    mgr = NotificationManager()
    mgr._channels = []  # 不依赖真实 webhook
    with patch.object(mgr, "_broadcast", new=AsyncMock(return_value=[])) as bc:
        await mgr.notify_trade_event("300001.SZ", "BUY", 100, 10.5, extra="tp=12.0")
    msg = bc.call_args.args[0]
    assert "300001.SZ" in msg and "BUY" in msg and "100" in msg and "10.5" in msg
    assert "成交" in msg  # 前缀区别于风控告警
```

- [ ] **Step 2: 验证测试失败**

Run: `pytest tests/infra/test_notifier_trade.py -v`
Expected: FAIL `AttributeError: 'NotificationManager' object has no attribute 'notify_trade_event'`

- [ ] **Step 3: 实现**

在 `infra/notifier.py` 的 `NotificationManager` 类内新增（参照现有 `notify_risk_event` 模式）：

```python
async def notify_trade_event(
    self,
    symbol: str,
    direction: str,
    qty: float,
    price: float,
    *,
    extra: str = "",
) -> list:
    """成交通知（每笔成交推钉钉）。

    与 notify_risk_event 区分：风控告警用 ⚠️/❌/🚨 前缀；成交通知用 💰【成交】前缀，
    便于研究员在群里一眼区分「业务流水」与「风险告警」。

    Args:
        symbol/direction/qty/price: 成交核心四要素（来自 on_stock_trade 回调）。
        extra: 附加信息（如止盈价/止损价/实验归因）。
    """
    arrow = "买入" if direction.upper() in ("BUY", "BUY") else "卖出"
    msg = (f"💰【成交】{arrow} {symbol} {qty}股 @ {price}\n"
           f"方向: {direction}{(' | ' + extra) if extra else ''}")
    # 复用 notify_risk_event 的并发广播逻辑（_broadcast 或 gather channels）
    return await self._broadcast(msg, "INFO")
```

（实施时 Read `infra/notifier.py:124-138` 确认 `notify_risk_event` 的内部广播调用方式——若是 `asyncio.gather(channels)`，抽一个 `_broadcast(msg, level)` 复用；若逻辑简单直接内联。保持 DRY。）

- [ ] **Step 4: 验证测试通过**

Run: `pytest tests/infra/test_notifier_trade.py -v`
Expected: PASS（1 passed）

- [ ] **Step 5: commit（需研究员授权）**

```bash
git add infra/notifier.py tests/infra/test_notifier_trade.py
git commit -m "feat(notifier): 新增 notify_trade_event 成交通知(💰前缀,区别风控告警)"
```

---

## Task 10: 成交回报 handler（日志+钉钉+挂止盈）

**Files:**
- Modify: `trading/engine.py`（TradingEngine 新增 `_handle_order_update`）
- Test: `tests/trading/test_engine_order_update_handler.py`

**Interfaces:**
- Consumes: `record_live_trade`（trading_service）；`NotificationManager.notify_trade_event`（Task9）；`trading_plan.load_plan`；`_submit`；`gw._orders`（查 order side）
- Produces: `async def _handle_order_update(self, update: Mapping) -> None`

**逻辑：** on_stock_trade 回调推送 `update`（含 `kind=="trade"`）→
1. 补写交易日志（成交价/量/时间）；
2. 推钉钉成交通知；
3. 若是买单成交（查 gw._orders 拿 side）且该 symbol 未挂止盈（幂等）→ 挂限价止盈卖单（Phase1 简化版，全额）。

- [ ] **Step 1: 写失败测试**

```python
# tests/trading/test_engine_order_update_handler.py
"""成交回报 handler：日志+钉钉+挂止盈三连。"""
from unittest.mock import patch, AsyncMock, MagicMock
from trading.engine import TradingEngine


async def test_trade_update_writes_log_and_notifies():
    """成交回报 → 补写日志 + 推钉钉。"""
    eng = TradingEngine()
    update = {"kind": "trade", "order_id": "123", "stock_code": "300001.SZ",
              "traded_volume": 100, "traded_price": 10.5, "traded_amount": 1050.0,
              "traded_time": 20260723, "state": "FILLED"}
    eng._tp_placed = set()  # 幂等标记
    with patch("trading.engine.record_live_trade") as rec, \
         patch("trading.engine.NotificationManager") as NM:
        await eng._handle_order_update(update)
    rec.assert_called_once()  # 成交日志补写
    assert "300001.SZ" in str(rec.call_args)


async def test_buy_fill_places_take_profit_once_idempotent():
    """买单成交 → 挂止盈；重复回报幂等不重挂。"""
    eng = TradingEngine()
    eng._tp_placed = set()
    update = {"kind": "trade", "order_id": "123", "stock_code": "300001.SZ",
              "traded_volume": 100, "traded_price": 10.5, "state": "FILLED"}
    plan = {"confirmed": True, "orders": [
        {"order": {"symbol": "300001.SZ", "qty": 100, "side": "buy", "price": 10.0},
         "stop_price": 9.5, "take_profit": 12.0}]}
    gw = MagicMock()
    gw._orders = {"123": {"order_type": 23}}  # 23=STOCK_BUY（买单标记）
    eng._gw = gw
    with patch("trading.engine.trading_plan.load_plan", return_value=plan), \
         patch("trading.engine.record_live_trade"), \
         patch("trading.engine.NotificationManager"), \
         patch.object(eng, "_place_take_profit", new=AsyncMock()) as tp:
        await eng._handle_order_update(update)  # 首次
        await eng._handle_order_update(update)  # 重复回报
    tp.assert_called_once()  # 幂等：只挂一次
```

- [ ] **Step 2: 验证测试失败**

Run: `pytest tests/trading/test_engine_order_update_handler.py -v`
Expected: FAIL `AttributeError: 'TradingEngine' object has no attribute '_handle_order_update'`

- [ ] **Step 3: 实现**

在 `trading/engine.py` 的 `TradingEngine` 类内新增（并 `__init__` 初始化 `self._tp_placed: set[str] = set()` 与 `self._gw = None`）：

```python
async def _handle_order_update(self, update: Mapping[str, Any]) -> None:
    """成交回报 handler（由 _process_order_update 经 create_task 调度，主线程执行）。

    三连（spec §6.2 C1）：
      a. record_live_trade 补写成交回报（on_stock_trade 推送的真实成交价/量，非下单预估价）；
      b. notify_trade_event 推钉钉成交通知；
      c. 买单成交 + 未挂止盈 → _place_take_profit 挂限价止盈卖单（Phase1 简化版全额）。

    幂等：_tp_placed 记录已挂止盈的 symbol，部分成交多次回报不重挂。

    线程安全：本方法 async，由主线程 create_task 调度（call_soon_threadsafe 投递），
    钉钉走 fire_and_forget 异步不阻塞回调链。
    """
    kind = update.get("kind")
    if kind != "trade":
        return  # 仅处理成交回报（order/order_error 由风控层负责）
    symbol = update.get("stock_code", "")
    qty = update.get("traded_volume", 0)
    price = update.get("traded_price", 0.0)
    order_id = str(update.get("order_id", ""))
    if not symbol or qty <= 0:
        return

    # a. 成交日志补写（direction 据订单 side 判定）
    direction = self._order_direction(order_id)  # BUY/SELL/未知
    try:
        from server.services.trading_service import record_live_trade
        record_live_trade(symbol, direction or "TRADE", float(qty), float(price),
                          strategy="neckline", rationale=f"成交回报@{update.get('traded_time')}")
    except Exception:
        logger.exception("成交日志补写失败 symbol=%s（不影响后续通知/挂止盈）", symbol)

    # b. 钉钉成交通知（fire_and_forget，失败软降级）
    try:
        from core.notifier import NotificationManager, fire_and_forget
        fire_and_forget(NotificationManager.get_default().notify_trade_event(
            symbol, direction or "TRADE", float(qty), float(price)))
    except Exception:
        logger.exception("成交通知发送失败 symbol=%s", symbol)

    # c. 买单成交 → 挂止盈（幂等）
    if direction == "BUY" and symbol not in self._tp_placed:
        try:
            await self._place_take_profit(symbol, qty, price, order_id)
        except Exception:
            logger.exception("挂止盈失败 symbol=%s（手动补挂）", symbol)

def _order_direction(self, order_id: str) -> str | None:
    """从 gw._orders 查订单方向（BUY/SELL）。

    order_type: xtconstant.STOCK_BUY=23 / STOCK_SELL=24。
    查不到（主推缺失/seq 未映射）→ 返 None（保守，不误挂止盈）。
    """
    orders = getattr(self._gw, "_orders", {}) if self._gw else {}
    rec = orders.get(order_id, {})
    # order_type 用 xtconstant 常量比较（勿硬编码魔法数字）；测试环境无 xtquant 时兜底
    try:
        from xtconstant import STOCK_BUY, STOCK_SELL
    except ImportError:
        STOCK_BUY, STOCK_SELL = 23, 24
    ot = rec.get("order_type")
    if ot == STOCK_BUY:
        return "BUY"
    if ot == STOCK_SELL:
        return "SELL"
    return None

async def _place_take_profit(self, symbol: str, filled_qty: float, fill_price: float, order_id: str) -> None:
    """挂限价止盈卖单（Phase1 简化版：单一固定止盈价，全额）。

    Phase2 升级为分级状态机（tp1 部分量 + tp2 剩余量，复刻 simulate_exit）。
    止盈价来自活跃计划的 take_profit；filled_qty 用实际成交量（非计划全量）。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    plan = trading_plan.load_plan(today)
    if not plan:
        return
    tp = None
    for o in plan.get("orders", []):
        if o.get("order", {}).get("symbol") == symbol:
            tp = o.get("take_profit")
            break
    if tp is None or tp <= 0:
        logger.warning("无止盈价配置 symbol=%s，跳过挂止盈", symbol)
        return
    from trading.compute.types import OrderRequest
    result = await _submit(OrderRequest(symbol=symbol, qty=int(filled_qty), side="sell", price=tp),
                           confirm=True)
    if result.get("state") not in ("REJECTED", "FAILED"):
        self._tp_placed.add(symbol)  # 幂等标记
        logger.info("【止盈单已挂】%s %s股 @%s（成交价%s）", symbol, int(filled_qty), tp, fill_price)
    else:
        logger.warning("止盈单挂失败 symbol=%s state=%s msg=%s",
                       symbol, result.get("state"), result.get("message"))
```

（`Mapping`/`Any` 若 engine.py 顶部未导入，补 `from typing import Any, Mapping`。）

- [ ] **Step 4: 验证测试通过**

Run: `pytest tests/trading/test_engine_order_update_handler.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: commit（需研究员授权）**

```bash
git add trading/engine.py tests/trading/test_engine_order_update_handler.py
git commit -m "feat(engine): 成交回报handler(日志+钉钉+挂止盈,幂等防重挂)"
```

---

## Task 11: gw connect + 注册回调时序（__main__ 改造）

**Files:**
- Modify: `trading/__main__.py`（启动时 connect + set_order_update_callback）

**说明：** 修 G5 根因——当前 `__main__` 既不 connect 也不注册回调。在 `eng.start()` 前 `await gw.connect()` + `gw.set_order_update_callback(eng._handle_order_update)`。

- [ ] **Step 1: 改 `__main__._run_forever`**

在 `trading/__main__.py` 的 `_run_forever()`（或等价启动协程）内，`eng.start()` 之前插入连接+注册：

```python
async def _run_forever():
    from trading.engine import TradingEngine, get_gateway
    eng = TradingEngine()

    # 连接网关 + 注册成交回报回调（修 G5：原 __main__ 既不 connect 也不注册回调）
    gw = get_gateway()
    if gw is not None:
        try:
            await gw.connect()  # async，内部 run_in_executor 包 C++ 阻塞调用
            gw.set_order_update_callback(eng._handle_order_update)  # sync 注入
            eng._gw = gw  # 供 handler 查 _orders 判 side
            logger.info("网关已连接 + 成交回调已注册")
        except Exception:
            logger.exception("网关连接失败（cron 仍启动，触发点内部 get_gateway 兜底）")
    else:
        logger.warning("未装配网关（AUTO_TRADE_MODE=dry_run 影子模式，回调链路不生效）")

    eng.start()
    # ... 原有阻塞逻辑 ...
```

- [ ] **Step 2: smoke 验证（dry_run，不真下单）**

```bash
# 确保 .env AUTO_TRADE_MODE=dry_run
python -m trading
```
Expected: 日志含「网关已连接 + 成交回调已注册」（模拟盘 gw 可连）；若 dry_run 无 gw，日志含「未装配网关」warning，进程不崩。

- [ ] **Step 3: 验证回调链路（模拟盘成交触发）**

在模拟盘手动下一笔买单（通过 Cockpit 或 qmt smoke 脚本），观察：
- `logs/live_trades.csv` 是否补写成交回报行（direction=BUY，price=实际成交价）；
- 钉钉群是否收到 💰【成交】通知；
- 是否自动挂出止盈限价卖单（查 QMT 委托）。

若任一断点，回到 Task10/11 排查。

- [ ] **Step 4: commit（需研究员授权）**

```bash
git add trading/__main__.py
git commit -m "fix(engine): __main__ 启动connect网关+注册成交回调(修G5根因:原不connect不注册)"
```

---

## Task 12: 持仓盈亏计算 + 19:00 播报

**Files:**
- Modify: `server/services/trading_service.py:105`（`get_positions` 算浮盈）
- Modify: `trading/engine.py`（`_eod` 末尾播报持仓盈亏）
- Test: `tests/services/test_positions_pnl.py`

**Interfaces:**
- Consumes: `gw._fetch_broker_positions`（avg_price）；`qmt_market_data.get_quotes`（现价）；`gw.query_asset`（总资产）
- Produces: `get_positions` 返回每仓含 `market_value`/`pnl`；`_broadcast_positions_pnl` 播报

- [ ] **Step 1: 写失败测试**

```python
# tests/services/test_positions_pnl.py
"""持仓盈亏计算（修 pnl=None）。"""
from unittest.mock import patch, AsyncMock
from server.services import trading_service


async def test_get_positions_computes_pnl_from_avg_and_last():
    """avg_price + 现价 → 浮盈。"""
    positions = {"300001.SZ": {"volume": 100.0, "avg_price": 10.0,
                               "open_price": 10.0, "yesterday_volume": 100}}
    quotes = {"300001.SZ": {"last_price": 11.0}}
    gw = AsyncMock()
    gw._fetch_broker_positions = AsyncMock(return_value=positions)
    with patch("server.services.trading_service.get_gateway", return_value=gw), \
         patch("server.services.trading_service.qmt_market_data.get_quotes",
               new=AsyncMock(return_value=quotes)):
        result = await trading_service.get_positions()
    pos = result[0]
    assert pos["symbol"] == "300001.SZ"
    assert pos["qty"] == 100.0
    assert pos["market_value"] == 1100.0   # 11.0 × 100
    assert pos["pnl"] == 100.0             # (11.0 - 10.0) × 100


async def test_get_positions_no_quote_pnl_none():
    """现价缺失 → pnl/market_value=None（不猜价）。"""
    positions = {"300001.SZ": {"volume": 100.0, "avg_price": 10.0,
                               "open_price": 10.0, "yesterday_volume": 100}}
    quotes = {"300001.SZ": {"last_price": None}}
    gw = AsyncMock()
    gw._fetch_broker_positions = AsyncMock(return_value=positions)
    with patch("server.services.trading_service.get_gateway", return_value=gw), \
         patch("server.services.trading_service.qmt_market_data.get_quotes",
               new=AsyncMock(return_value=quotes)):
        result = await trading_service.get_positions()
    assert result[0]["pnl"] is None  # 盲价防御
```

- [ ] **Step 2: 验证测试失败**

Run: `pytest tests/services/test_positions_pnl.py -v`
Expected: FAIL（现状 pnl 恒 None，断言 `==100.0` 不过）

- [ ] **Step 3: 实现 get_positions 盈亏**

修改 `server/services/trading_service.py:105` 的 `get_positions`，持仓查询后批量取现价算浮盈：

```python
async def get_positions() -> list:
    """...（原 docstring，补：现价可用时计算 market_value/pnl 浮盈）。"""
    gw = get_gateway()
    if gw is None:
        return []
    positions = await gw._fetch_broker_positions()  # {sym: {volume, avg_price, ...}}

    # 批量取现价算浮盈（Task12：修 pnl=None）
    syms = list(positions.keys())
    quotes = {}
    if syms:
        try:
            from trading import qmt_market_data  # 或 broker.qmt_quote
            quotes = await qmt_market_data.get_quotes(syms)
        except Exception:
            logger.exception("取现价失败，pnl/market_value 将为 None（不猜价）")

    result = []
    for sym, pos in positions.items():
        qty = pos["volume"] if isinstance(pos, dict) else pos
        avg = pos.get("avg_price") if isinstance(pos, dict) else None
        quote = quotes.get(sym, {})
        last = quote.get("last_price") if quote else None
        # 盲价防御：last 缺失/NaN → pnl/market_value=None（现状契约向后兼容）
        if last is None or last != last or avg is None:
            market_value, pnl = None, None
        else:
            market_value = float(last) * qty
            pnl = (float(last) - float(avg)) * qty
        result.append({
            "symbol": sym, "qty": qty,
            "market_value": market_value, "pnl": pnl,
            "strategy": ..., "entry_rationale": ...,  # 原 _position_attribution 富化逻辑保留
        })
    return result
```

（实施时 Read `trading_service.py:105-137` 保留原 `_position_attribution` 富化逻辑，仅补现价查询与 pnl 计算。）

- [ ] **Step 4: 19:00 eod_plan 末尾播报持仓盈亏**

在 `trading/engine.py` `_eod` 末尾（eod_plan 落盘+推钉钉后）追加：

```python
# 持仓盈亏播报（spec §6.2 C4 / 子诉求 1<2>）
await self._broadcast_positions_pnl()
```

新增方法：

```python
async def _broadcast_positions_pnl(self) -> None:
    """播报当前持仓 + 盈亏（总资产/逐仓浮盈/盈亏比）。"""
    try:
        from server.services.trading_service import get_positions
        from core.notifier import NotificationManager, fire_and_forget
        gw = get_gateway()
        positions = await get_positions()
        asset = await gw.query_asset() if gw else {}
        total = asset.get("total_asset", 0.0)
        lines = [f"## 💼 持仓盈亏（总资产 {total:.0f}）"]
        for p in positions:
            pnl = p.get("pnl")
            mark = f"{pnl:+.0f}" if pnl is not None else "N/A"
            lines.append(f"- {p['symbol']} {p['qty']:.0f}股 浮盈{mark}")
        if not positions:
            lines.append("- 空仓")
        msg = "\n".join(lines)
        fire_and_forget(NotificationManager.get_default().notify_risk_event(msg, "INFO"))
    except Exception:
        logger.exception("持仓盈亏播报失败（不影响 eod_plan 主流程）")
```

- [ ] **Step 5: 验证测试通过**

Run: `pytest tests/services/test_positions_pnl.py -v`
Expected: PASS（2 passed）

- [ ] **Step 6: commit（需研究员授权）**

```bash
git add server/services/trading_service.py trading/engine.py tests/services/test_positions_pnl.py
git commit -m "feat(trading): 持仓盈亏计算(avg×现价)+19:00播报(修pnl=None)"
```

---

## 全量回归验证（Phase 1 收尾）

- [ ] **Step 1: 全量测试**

```bash
pytest -x -q
```
Expected: 全绿（基线 719p，新增约 15p；唯一已知失败 = universe*ST 预存基线，零新增失败）。

- [ ] **Step 2: 模拟盘单日端到端 smoke**

模拟一日完整链路（dry_run 或模拟盘真连）：
- 17:00 检查点① → 18:00 增量采集 → 18:30 检查点② → 19:00 eod_plan+盈亏播报 → 次日 09:22 pre_open → 开盘成交回调（日志+钉钉+止盈）→ 30s 止损监控 → 15:30 post_close 对账。

Expected（验收标尺，spec §11）：**零漏单零重单，成交钉钉+日志可追溯，盈亏播报含数值**。

- [ ] **Step 3: commit（需研究员授权）**

```bash
git add -A
git commit -m "test: Phase 1 全量回归通过(模拟盘端到端骨架跑通)"
```

---

## Phase 2-4（后续 plan，不在本 plan 范围）

- **Phase 2**：止盈升级 `simulate_exit` 分级状态机（tp1 部分量 + tp2 剩余 + 撤单），消除 Phase1 简化版执行偏差。
- **Phase 3**：`compute_stop_price`(grace/step/floor) 盘中动态更新注入 stop_loss_monitor。
- **Phase 4**：post_close 熔断连线（query_asset.total_asset → check_daily_loss_limit → cancel_all → emergency_halt）+ 断线重连验证。

## 关键待核实点（spec §10，本 plan 已嵌入 Task8/Task11）

| 待核实 | 任务 | 影响 |
|---|---|---|
| miniQMT 30s 监控限频 | Task8 Step2 实测 | 定 ENGINE_STOPLOSS_INTERVAL_SECONDS 终值（30/60） |
| on_stock_trade 回调字段 | Task10 已用 order_id/stock_code/traded_volume/traded_price/traded_amount/traded_time（核实自 broker/qmt.py:1026） | 成交日志/钉钉字段 |
| _on_order_update 注册时序 | Task11 connect 后 set_order_update_callback | 避免回调漏注册 |
| Tushare daily 可用时间 | Task4 schtasks 18:30 检查点② | 检查点②最早触发（18:00 采集后） |
