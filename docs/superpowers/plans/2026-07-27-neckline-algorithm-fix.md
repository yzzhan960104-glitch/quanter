# 颈线法算法修复（4 缺口）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复颈线法 scan_live vs scan_at 语义脱节（R1 cancel_on 预判 + R2 窗口已突破过滤）+ rr 守卫脱节（R3 rr 改实际口径 + 计划展示）。

**Architecture:** 改 detect_neckline_method（R2 过滤 + R3 stop_price/rr 实际口径）→ 透传 rr 到 Signal/PlannedOrder/order_dict/push md（R3 展示链路）→ scan_live 加 cancel_on 预判（R1）→ 全市场回测对比 confirm 冠军 outer calmar 不退化。

**Tech Stack:** Python 标准库 + pandas，pytest（asyncio.run，不加 mark.asyncio），TDD。

## Global Constraints

- **全中文注释**（CLAUDE.md）：What + Why（交易物理意图/风控红线）。
- **pytest-asyncio strict**：async 测试用 `asyncio.run(...)`，不加 `@pytest.mark.asyncio`。
- **测试用 `.venv310/Scripts/python.exe`**（系统 python 缺 pandas）。
- **不碰数据完整性**（lake 缺口，另 session 处理，本计划只改算法）。
- **保持 scan_at 回测一致**：detect 改动后 `tests/test_neckline_core.py` + `tests/test_neckline_recognition.py` 既有 golden 不破（或预期内更新，需说明）。
- **R3 不改 H 几何标尺**：H 仍用于 tp1/tp2 定位（形态目标），只加 stop_price（实际止损）+ rr 改实际口径。

---

## Task 1: detect 增强（R2 窗口已突破过滤 + R3 rr 实际口径）

**Files:**
- Modify: `strategies/neckline/method_v0.py:181-266`（detect_neckline_method）
- Test: `tests/test_neckline_recognition.py`（扩展 3 个测试）

**Interfaces:**
- Consumes: cfg["stop_atr_mult"]（既有，默认 1.0）算 stop_price
- Produces:
  - detect 返 None 的新路径：窗口内除末根外有 close > c_star（R2）
  - detect 返回 dict 加 `"stop_price": round(stop_price, 3)`、`"rr"` 改实际口径 `(tp2-entry)/(entry-stop_price)`（R3）

- [ ] **Step 1: 写失败测试（追加到 tests/test_neckline_recognition.py）**

```python
def test_detect_rejects_prior_breakout_in_window():
    """R2：窗口内除末根外有 close > 颈线 → 颈线已失效，返 None。

    物理意图：颈线=未被突破的阻力。窗口内若已有 close 站稳颈线（非末根），
    末根的"突破"是二次突破，不产首次信号（300214.SZ 6月 close 10.57>8.08 案例）。
    """
    import pandas as pd
    from strategies.neckline.method_v0 import detect_neckline_method, DEFAULTS
    dates = pd.date_range("2026-01-01", periods=70, freq="D")
    df = pd.DataFrame({
        "open": 9, "high": 10, "low": 8.5, "close": 9.5, "volume": 1000,
    }, index=dates)
    df.iloc[30:41, df.columns.get_loc("close")] = 12  # 中期突破颈线
    df.iloc[30:41, df.columns.get_loc("high")] = 12.5
    df.iloc[60:, df.columns.get_loc("close")] = 13
    df.iloc[60:, df.columns.get_loc("high")] = 13.5
    df.iloc[-1, df.columns.get_loc("volume")] = 5000  # 末根带量
    res = detect_neckline_method(df, {**DEFAULTS, "window": 60, "breakout_vol_mult": 1.0})
    assert res is None, "窗口内已突破（中期 close12>颈线10）应返 None"


def test_detect_rr_uses_actual_stop_price():
    """R3：rr 改实际口径 (tp2-entry)/(entry-stop_price)，stop_price=颈线-stop_atr_mult×ATR。

    几何 rr=2H/H=2.0（谷底止损）→ 实际 rr 用颈线-N×ATR 止损，不等于 2.0。
    """
    import pandas as pd
    from strategies.neckline.method_v0 import detect_neckline_method, DEFAULTS
    dates = pd.date_range("2026-01-01", periods=70, freq="D")
    df = pd.DataFrame({
        "open": 9, "high": 10, "low": 8.5, "close": 9.5, "volume": 1000,
    }, index=dates)
    df.iloc[-1, df.columns.get_loc("close")] = 11  # 末根突破颈线 10
    df.iloc[-1, df.columns.get_loc("high")] = 11.5
    df.iloc[-1, df.columns.get_loc("volume")] = 5000  # 带量
    cfg = {**DEFAULTS, "window": 60, "breakout_vol_mult": 1.0, "stop_atr_mult": 1.0, "tp_h_mult": 2.0}
    res = detect_neckline_method(df, cfg)
    assert res is not None
    atr_val = res["atr"]
    # R3：stop_price = 颈线 - stop_atr_mult×ATR（实际止损，非谷底）
    assert res["stop_price"] == round(res["neckline"] - 1.0 * atr_val, 3)
    # rr 实际口径 = (tp2-entry)/(entry-stop_price)
    expected_rr = (res["take_profit_2"] - res["entry"]) / (res["entry"] - res["stop_price"])
    assert abs(res["rr"] - round(expected_rr, 3)) < 0.01


def test_detect_min_rr_uses_actual():
    """R3：min_rr 守卫验实际 rr（非几何 2.0）。"""
    import pandas as pd
    from strategies.neckline.method_v0 import detect_neckline_method, DEFAULTS
    dates = pd.date_range("2026-01-01", periods=70, freq="D")
    df = pd.DataFrame({
        "open": 9, "high": 10, "low": 8.5, "close": 9.5, "volume": 1000,
    }, index=dates)
    df.iloc[-1, df.columns.get_loc("close")] = 11
    df.iloc[-1, df.columns.get_loc("high")] = 11.5
    df.iloc[-1, df.columns.get_loc("volume")] = 5000
    cfg = {**DEFAULTS, "window": 60, "breakout_vol_mult": 1.0, "min_rr": 0.5}
    res = detect_neckline_method(df, cfg)
    assert res is not None
    assert res["rr"] > 0.5
```

- [ ] **Step 2: 跑测试验证失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_neckline_recognition.py::test_detect_rejects_prior_breakout_in_window tests/test_neckline_recognition.py::test_detect_rr_uses_actual_stop_price -v`
Expected: FAIL（窗口已突破未过滤 / stop_price 字段不存在）

- [ ] **Step 3: 改 detect_neckline_method（method_v0.py:181-266）**

**改 1（R2 窗口已突破过滤，加在 close_T>c_star 通过后、带量判定处）**：
```python
    # —— 3. 突破（收盘越过颈线 + 带量）——
    close_T = float(W["close"].iloc[-1])
    if close_T <= c_star:
        return None  # 未突破颈线
    # R2 窗口内已突破过滤（2026-07-27 缺口2）：窗口除末根外有 close>c_star → 颈线已失效。
    # 物理意图：颈线=未被突破的阻力；窗口内若已有 close 站稳颈线（非末根），末根是二次突破，
    # 不产首次信号（300214.SZ 6月已突破8.08，7月再涨被当首次=误识别）。
    if (W["close"].iloc[:-1] > c_star).any():
        return None  # 窗口内已突破过，颈线失效
    vol_T = float(W["volume"].iloc[-1])
    vol5 = float(W["volume"].tail(5).mean())
    if vol5 > 0 and vol_T < cfg["breakout_vol_mult"] * vol5:
        return None  # 突破未带量
```

**改 2（R3 stop_price + rr 实际口径，替换交易要素 H 计算后段）**：
```python
    # —— 4. 交易要素（颈线 + 最低点 → 进场/止损/止盈/rr）——
    entry = c_star                                  # 挂单等回踩颈线
    H = c_star - min_price                          # 形态几何深度（tp 定位标尺）
    if H <= 0:
        return None
    h_over_atr = H / atr_val
    if h_over_atr > cfg.get("max_h_atr", 4.0):
        return None
    take_profit_1 = c_star + H                      # 第一波满足（几何，颈线+1H）
    take_profit_2 = c_star + cfg["tp_h_mult"] * H   # 第二波满足（几何，颈线+N×H）
    # R3 实际止损 + 实际盈亏比（2026-07-27 缺口3）：止损用 ATR 波动口径（执行层一致），
    # 非 detect 旧版谷底。rr = (tp2-entry)/(entry-stop_price) 实际口径，min_rr 验真实盈亏比。
    # Why：旧版 rr=2H/H=2.0 几何 sanity，跟执行层 base_stop=颈线-N×ATR 脱节，
    # min_rr 没把住真实风险收益。止盈保留 H 几何标尺（形态目标），止损用 ATR 波动标尺（风控）。
    stop_price = c_star - cfg["stop_atr_mult"] * atr_val
    risk_dist = entry - stop_price
    if risk_dist <= 0:
        return None
    rr = (take_profit_2 - entry) / risk_dist
    if rr < cfg["min_rr"]:
        return None
```

**改 3（返回 dict 加 stop_price，rr 值已是实际口径）**：
```python
    return {
        "formed_at": W.index[-1],
        "neckline": round(c_star, 3),
        "suppression": round(suppression, 3),
        "bottom": round(min_price, 3),
        "n_bottoms": len(bottom_set),
        "entry": round(entry, 3),
        "stop": round(min_price, 3),                # 谷底（保留，H 计算基准）
        "stop_price": round(stop_price, 3),         # R3 实际交易止损（颈线-N×ATR，执行层口径）
        "take_profit_1": round(take_profit_1, 3),
        "take_profit_2": round(take_profit_2, 3),
        "H": round(H, 3),
        "H_over_ATR": round(h_over_atr, 2),
        "rr": round(rr, 3),                         # R3 实际口径盈亏比（替换旧几何 2H/H）
        "atr": round(atr_val, 3),
    }
```

- [ ] **Step 4: 跑测试验证通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_neckline_recognition.py -v`
Expected: 新 3 个 PASS。既有用例若因 rr 口径变更（2.0→实际）FAIL，核对是 golden 需更新（加注释说明 rr 改实际口径）而非真 bug。

- [ ] **Step 5: 跑既有颈线核心测试确认回归**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_neckline_core.py tests/test_neckline_recognition.py -v`
Expected: 全 PASS（R2 过滤可能让某些既有用例从 hit→None，核对构造形态是否"无窗口已突破"）

- [ ] **Step 6: Commit**

```bash
git add strategies/neckline/method_v0.py tests/test_neckline_recognition.py
git commit -m "feat(neckline): detect 加 R2 窗口已突破过滤 + R3 rr 实际口径

- R2: 窗口除末根外有 close>颈线 → 返 None（颈线已失效，挡二次突破）
- R3: stop_price=颈线-stop_atr_mult×ATR（实际止损），rr=(tp2-entry)/(entry-stop_price)
- min_rr 守卫验实际盈亏比（替换旧几何 2H/H sanity）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2: R3 rr 透传链路（Signal scan_live 填 + PlannedOrder + order_dict + push md）

**Files:**
- Modify: `strategies/neckline_method.py:191-200`（scan_live 填 rr）
- Modify: `trading/compute/plan.py:37-116`（PlannedOrder 加 rr + build 透传）
- Modify: `trading/engine.py:223-237`（eod_plan order_dict 加 rr）
- Modify: `trading/trading_plan.py`（push_plan_to_dingtalk md 加 rr）
- Test: `tests/trading/test_signal_runner.py` + `tests/trading/test_trading_plan.py`

**Interfaces:**
- Consumes: Task 1 的 detect 返回 `res["rr"]`（实际口径）
- Produces: Signal.rr（scan_live 填）→ PlannedOrder.rr → order_dict["rr"] → push md 显示 rr

- [ ] **Step 1: 写失败测试**

`tests/trading/test_signal_runner.py` 加：
```python
def test_scan_live_signal_carries_rr():
    """R3：scan_live 返回的 Signal 携带 rr（实际口径，从 detect res['rr'] 读）。"""
    import pandas as pd
    from strategies.neckline_method import NecklineMethodStrategy
    dates = pd.date_range("2026-01-01", periods=70, freq="D")
    df = pd.DataFrame({"open": 9, "high": 10, "low": 8.5, "close": 9.5, "volume": 1000}, index=dates)
    df.iloc[-1, df.columns.get_loc("close")] = 11
    df.iloc[-1, df.columns.get_loc("high")] = 11.5
    df.iloc[-1, df.columns.get_loc("volume")] = 5000
    strat = NecklineMethodStrategy(cfg_override={"window": 60, "breakout_vol_mult": 1.0})
    sigs = strat.scan_live("TEST.SZ", df, str(dates[-1].date()))
    assert len(sigs) == 1
    assert sigs[0].rr is not None and sigs[0].rr > 0
```

`tests/trading/test_trading_plan.py` 加：
```python
def test_push_plan_md_includes_rr(monkeypatch):
    """R3：push_plan_to_dingtalk md 含实际盈亏比 rr。"""
    from trading import trading_plan
    captured = {}
    monkeypatch.setattr(trading_plan, "push_brief", lambda title, md, **kw: captured.update(md=md) or True)
    orders = [{"order": {"symbol": "T.SZ", "side": "buy", "qty": 100, "price": 10.0},
               "stop_price": 9.0, "take_profit": 12.0, "rr": 2.5}]
    trading_plan.push_plan_to_dingtalk("2026-07-27", orders)
    assert "2.5" in captured["md"]  # md 显示 rr
```

- [ ] **Step 2: 跑测试验证失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_signal_runner.py::test_scan_live_signal_carries_rr tests/trading/test_trading_plan.py::test_push_plan_md_includes_rr -v`
Expected: FAIL（scan_live 没填 rr / push md 没 rr）

- [ ] **Step 3: scan_live 填 rr（neckline_method.py:191-200）**

Signal 构造加 `rr=res.get("rr")`：
```python
    return [Signal(
        symbol=symbol,
        signal_type="neckline",
        formed_at=res.get("formed_at"),
        breakout_date=res.get("formed_at"),
        neckline=res.get("neckline"),
        bottom=res.get("bottom"),
        entry_price=res.get("entry") if res.get("entry") is not None else res.get("neckline"),
        atr=float(atr_full.iloc[-1]) if not pd.isna(atr_full.iloc[-1]) else res.get("atr"),
        rr=res.get("rr"),  # R3 实际口径盈亏比（从 detect 透传）
    )]
```

- [ ] **Step 4: PlannedOrder 加 rr + build_orders 透传（plan.py:37-116）**

PlannedOrder 加 rr 字段：
```python
@dataclass
class PlannedOrder:
    order: OrderRequest
    stop_price: float
    take_profit: float
    neckline: float
    experiment_id: str = ""
    experiment_weight: float = 1.0
    rr: float = 0.0  # R3 实际口径盈亏比（从 Signal 透传，push 展示用）
```

build_orders_from_signals 的 PlannedOrder 构造加 `rr=s.rr`：
```python
        out.append(PlannedOrder(
            order=OrderRequest(symbol=sym, qty=float(qty), side="buy", price=float(entry)),
            stop_price=stop_price, take_profit=take_profit, neckline=float(neckline),
            experiment_id=s.experiment_id,
            experiment_weight=weight,
            rr=s.rr if s.rr is not None else 0.0,  # R3 透传实际 rr
        ))
```

- [ ] **Step 5: eod_plan order_dict 加 rr（engine.py:223-237）**

order_dict 加 `"rr": round(o.rr, 2)`：
```python
    order_dicts = [
        {
            "order": {"symbol": o.order.symbol, "qty": o.order.qty, "side": o.order.side, "price": o.order.price},
            "stop_price": o.stop_price,
            "take_profit": o.take_profit,
            "experiment_id": o.experiment_id,
            "experiment_weight": o.experiment_weight,
            "rr": round(o.rr, 2),  # R3 实际盈亏比（push 展示 + plan 落盘）
        }
        for o in orders
    ]
```

- [ ] **Step 6: push_plan_to_dingtalk md 加 rr（trading_plan.py）**

lines 每单加 rr：
```python
        lines = []
        for o in orders:
            sym = o['order']['symbol']
            nm = name_map.get(sym, "")
            prefix = f"{nm} " if nm else ""
            rr = o.get("rr")
            rr_str = f" 盈亏比{rr:.1f}" if rr else ""
            lines.append(
                f"- {prefix}{sym} {o['order']['side']} {o['order']['qty']}股"
                f"@{o['order']['price']}（止损{o['stop_price']}/止盈{o['take_profit']}）{rr_str}"
            )
```

- [ ] **Step 7: 跑测试验证通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/trading/test_signal_runner.py tests/trading/test_trading_plan.py -v`
Expected: 新 2 个 PASS + 既有不回归

- [ ] **Step 8: Commit**

```bash
git add strategies/neckline_method.py trading/compute/plan.py trading/engine.py trading/trading_plan.py tests/trading/test_signal_runner.py tests/trading/test_trading_plan.py
git commit -m "feat(neckline): R3 rr 透传链路 — Signal→PlannedOrder→order_dict→push md

scan_live 填 Signal.rr；PlannedOrder 加 rr；eod_plan order_dict 加 rr；
push_plan_to_dingtalk md 显示「盈亏比N.N」。研究员人审看真实盈亏比。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3: R1 scan_live 加 cancel_on 预判

**Files:**
- Modify: `strategies/neckline_method.py:130-200`（scan_live，detect 后加 cancel_on 守卫）
- Test: `tests/test_neckline_recognition.py`

**Interfaces:**
- Consumes: detect 返回 res["neckline"]/res["bottom"]（算 H）+ exec_cfg["cancel_thresh_mult"]
- Produces: scan_live 在 close ≥ 颈线+cancel_thresh_mult×H 时返 []（涨幅已兑现，不产废单信号）

- [ ] **Step 1: 写失败测试**

```python
def test_scan_live_rejects_when_close_above_cancel_on():
    """R1：close ≥ 颈线 + cancel_thresh_mult×H → 返 []（涨幅已兑现，回踩是退潮，挂废单）。

    300214.SZ 案例：close 11.86 ≥ cancel_on 10.86（颈线8.08+2×H1.39）→ 不产信号。
    """
    import pandas as pd
    from strategies.neckline_method import NecklineMethodStrategy
    dates = pd.date_range("2026-01-01", periods=70, freq="D")
    df = pd.DataFrame({"open": 9, "high": 10, "low": 8.5, "close": 9.5, "volume": 1000}, index=dates)
    df.iloc[-1, df.columns.get_loc("close")] = 15  # 远超颈线，触发 cancel_on
    df.iloc[-1, df.columns.get_loc("high")] = 15.5
    df.iloc[-1, df.columns.get_loc("volume")] = 5000
    strat = NecklineMethodStrategy(cfg_override={
        "window": 60, "breakout_vol_mult": 1.0, "cancel_thresh_mult": 1.0})
    sigs = strat.scan_live("TEST.SZ", df, str(dates[-1].date()))
    assert sigs == [], "close 远超颈线+H（涨幅已兑现）应返 []，不产废单信号"
```

- [ ] **Step 2: 跑测试验证失败**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_neckline_recognition.py::test_scan_live_rejects_when_close_above_cancel_on -v`
Expected: FAIL（scan_live 没 cancel_on 守卫）

- [ ] **Step 3: scan_live 加 cancel_on 预判（neckline_method.py:130-200）**

**定位**：scan_live 内 `res = detect_neckline_method(...)` 之后、`if pd.Timestamp(breakout_date)...` 之前。加：
```python
        res = detect_neckline_method(df_upto, self.id_cfg, atr_series=atr_full)
        if res is None:
            return []

        # R1 cancel_on 预判（2026-07-27 缺口1+4合并）：当前 close ≥ 颈线+cancel_thresh_mult×H
        # → 涨幅已兑现，回踩是退潮，挂颈线买单是废单 → 不产信号。
        # Why：scan_live 不调 simulate_exit（保持实盘无前视），故把 execute 层的 cancel_on
        # 撤单逻辑前移为识别期预判。挡两类废单：① close 偏离颈线过大挂单不成交（缺口1）
        # ② 涨幅达 cancel_on 回踩是退潮（缺口4）。300214.SZ close11.86≥cancel_on10.86→挡掉。
        cancel_thresh = self.exec_cfg.get("cancel_thresh_mult")
        if cancel_thresh is not None:
            H = res["neckline"] - res["bottom"]
            cancel_on = res["neckline"] + cancel_thresh * H
            close_T = float(df_upto["close"].iloc[-1])
            if close_T >= cancel_on:
                return []  # 涨幅已兑现，不产回踩挂单信号

        # 当日突破过滤（防御层）：只挂当日新信号。
```

- [ ] **Step 4: 跑测试验证通过**

Run: `.venv310/Scripts/python.exe -m pytest tests/test_neckline_recognition.py -v`
Expected: 新测试 PASS + 既有不回归

- [ ] **Step 5: Commit**

```bash
git add strategies/neckline_method.py tests/test_neckline_recognition.py
git commit -m "feat(neckline): R1 scan_live 加 cancel_on 预判（缺口1+4合并）

close ≥ 颈线+cancel_thresh_mult×H → 返 []（涨幅已兑现，回踩是退潮，挂废单）。
复用 exec_cfg cancel_thresh_mult（默认1.0=颈线+H，冠军2.0）。挡缺口1+4。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 4: 全市场回测对比（confirm 冠军 outer calmar 不退化）

**Files:**
- Run: 临时回测脚本（不落库，验证用）

**Interfaces:**
- Consumes: Task 1-3 改后的 detect + scan_at（scan_at 经 detect 自动获 R2/R3）

- [ ] **Step 1: 跑冠军实验 top200 标的回测（改后算法）**

Run:
```bash
.venv310/Scripts/python.exe -c "
import pandas as pd
from dotenv import load_dotenv; load_dotenv()
from experiment.resolver import resolve_active
from strategies.neckline.backtest import scan_symbol, kelly_metrics
lake = pd.read_parquet('data_lake/a_shares_daily.parquet')
exp = resolve_active()[0]; params = exp.params
universe = [s for s in lake.index.get_level_values('symbol').unique()
            if s.split('.')[0].startswith(('300','301','688','689'))]
all_pnls, all_dates = [], []
for sym in universe[:200]:  # top200 加速
    try:
        df = lake.xs(sym, level='symbol').sort_index()
        filled, _, _ = scan_symbol(df, params['window'], exec=params, id_cfg=params)
        for r in filled:
            all_pnls.append(r['avg_pnl_pct']); all_dates.append(pd.to_datetime(r['signal_date']))
    except Exception: pass
kelly, curve, ann = kelly_metrics(all_pnls, all_dates)
print(f'top200: {len(all_pnls)}笔 年化={ann*100:.1f}% kelly={kelly*100:.1f}%')
"
```
Expected: 输出年化 + 笔数。记录改后数值。

- [ ] **Step 2: 判定**

- 冠军 outer calmar 7.24（memory discovery-engine-status）。改后 top200 年化 + 回撤算 calmar 对比。
- **不退化（或提升）**：R1/R2/R3 纯增益（挡废单/失效信号），merge。
- **退化 > 20%**：R2（窗口已突破过滤）改变信号分布（挡掉回测里"二次突破"信号，可能误打误撞盈利）。需重做 discovery 或调 R2 严格度。

- [ ] **Step 3: 记录回测结果到 progress ledger**

回测是验证（无代码改动），不强制 commit。若调参数，commit。

---

## Self-Review 已完成

**Spec 覆盖**：R1→Task 3 ✓ / R2→Task 1 ✓ / R3→Task 1(detect) + Task 2(透传) ✓ / 回测→Task 4 ✓ / YAGNI（完整性/simulate_exit/trailing/数据缺口）显式不做 ✓

**Placeholder 扫描**：无 TBD/TODO（每步含完整代码 + 命令 + 期望输出）。

**类型一致性**：detect 返回 `stop_price`/`rr`（Task 1）→ scan_live 读 `res["rr"]`（Task 2）→ PlannedOrder.rr（Task 2）→ order_dict["rr"]（Task 2）→ push md（Task 2），命名一致。cancel_on 用 `exec_cfg["cancel_thresh_mult"]`（Task 3），与 EXEC_DEFAULTS 一致。

**注意**：Task 1 的 R3 改动会让既有 golden 测试的 rr 断言从 2.0 变实际值——若 FAIL，implementer 核对是 rr 口径变更（合理，更新 golden + 注释）还是真 bug。
