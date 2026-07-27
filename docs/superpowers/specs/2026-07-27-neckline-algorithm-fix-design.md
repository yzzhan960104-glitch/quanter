# 颈线法算法修复设计（4 缺口）

- **日期**：2026-07-27
- **分支**：master
- **状态**：待审
- **关联**：memory `neckline-algorithm-gaps.md`（4 缺口诊断）、`data-lake-integrity-gap.md`（数据缺口，另 session 处理，本 spec 不碰）
- **范围**：修复颈线法 scan_live（实盘识别）vs scan_at（回测执行）的语义脱节 + rr 守卫脱节。

---

## 1. 背景

2026-07-27 用 300214.SZ 实盘计划倒逼算法审查，发现 4 个缺口（详见 memory `neckline-algorithm-gaps`）：

| # | 缺口 | 根因 |
|---|---|---|
| 1 | scan_live 缺成交可行性过滤（挂单价 vs 现价偏离） | scan_live vs scan_at 脱节 |
| 2 | scan_live 缺窗口内已突破过滤（6月已破7月再产） | scan_live vs scan_at 脱节 |
| 4 | scan_live 丢失 cancel_on 规则（涨幅已兑现不撤） | scan_live vs scan_at 脱节 |
| 3 | 两套 stop 口径 + rr 守卫脱节（几何 H vs 波动 ATR） | 标尺混用（独立） |

**共性**：1/2/4 都是 scan_live（实盘纯识别，只跑 detect）丢失了 scan_at 经 simulate_exit 做的三道过滤；3 是独立的 stop/rr 标尺混用。

---

## 2. 三个修复（决策已对齐）

### R1（缺口 1+4 合并）：scan_live 加 cancel_on 预判

**位置**：`strategies/neckline_method.py:130` scan_live，detect 后、Signal 构造前。

**逻辑**：
```python
# detect 返回 res 后，读 exec_cfg["cancel_thresh_mult"]
# cancel_on = 颈线 + cancel_thresh_mult × H（H = 颈线 − 底部，已在 res 里）
# 当前 close（df_upto 末根 close）≥ cancel_on → 返 []（涨幅已兑现，回踩是退潮）
H = res["neckline"] - res["bottom"]
cancel_on = res["neckline"] + exec_cfg["cancel_thresh_mult"] * H
close_T = float(df_upto["close"].iloc[-1])
if close_T >= cancel_on:
    return []  # 涨幅达 cancel_on，挂颈线买单是废单，不产信号
```

**复用参数**：`cancel_thresh_mult`（EXEC_DEFAULTS，默认 1.0=颈线+H，冠军 2.0=颈线+2H），不新增参数。

**物理意图**：突破后涨幅达 cancel_on（涨幅已兑现），回踩是退潮 → 挂颈线买单等回踩是废单 → 不产信号。同时挡住缺口 1（close 偏离过大挂单不成交）+ 缺口 4（涨幅兑现不追）。

**300214.SZ 验证**：cancel_on = 8.08 + 2.0×1.39 = 10.86，07-24 close 11.86 ≥ 10.86 → 挡掉 ✓

### R2（缺口 2）：detect 加窗口内已突破过滤

**位置**：`strategies/neckline/method_v0.py:224` detect_neckline_method，突破判定（close_T > c_star）后、形态深度前。

**逻辑**：
```python
# 窗口 W 除末根外，有 close > c_star → 颈线已失效（非首次突破），返 None
prior_closes = W["close"].iloc[:-1]  # 窗口除末根
if (prior_closes > c_star).any():
    return None  # 窗口内已突破过，颈线不再是有效阻力
```

**判定口径**：close（收盘站稳 = 真突破，过滤上影假突破）。

**物理意图**：颈线 = 未被突破的阻力。窗口内若已有 close 站稳颈线（非末根），颈线已失效，末根的"突破"是二次突破/再次穿越，不产首次突破信号。

**300214.SZ 验证**：6 月 close 10.57 > 8.08 → 挡掉 ✓

**注意**：与数据完整性缺口交互——若 lake 缺窗口内某段（如 300214.SZ 缺 07-14~07-21），R2 可能漏判（看不到那段已突破）。R2 需配合完整性 gate（数据 session 处理），但 R2 本身逻辑正确。

### R3（缺口 3）：rr 改实际口径 + 计划展示

**位置**：detect_neckline_method（method_v0.py:248）+ Signal（strategies/signal.py）+ build_orders_from_signals（trading/compute/plan.py）+ eod_plan order_dict（engine.py）+ push_plan_to_dingtalk（trading_plan.py）。

**逻辑**：
```python
# detect 内：rr 改实际口径
stop_price = c_star - cfg["stop_atr_mult"] * atr_val   # 实际交易止损（执行层口径）
rr_actual = (take_profit_2 - entry) / (entry - stop_price) if (entry - stop_price) > 0 else 0.0
# 返回 dict 加 "rr_actual": round(rr_actual, 3)，"stop_price": round(stop_price, 3)
# min_rr 守卫改用 rr_actual
```

**透传链路**：
- Signal 加 `rr` 字段（实际口径，scan_live 填）
- build_orders_from_signals 读 signal.atr 算 stop_price（已有，plan.py:106）+ 算 rr_actual，PlannedOrder 加 `rr`
- eod_plan order_dict 加 `"rr": o.rr`
- push_plan_to_dingtalk md 加 rr（每单显示实际盈亏比）

**物理意图**：min_rr 守卫验真实盈亏比（实际 entry/stop 口径），研究员在计划 md 看到真实盈亏比（人审关键信息）。止盈用 H（几何目标）、止损用 ATR（波动风控）的混用设计保留，但 rr 必须用实际口径。

**300214.SZ**：实际 rr = (11.56−8.31)/(8.31−7.61) ≈ 4.6（冠军 tp_h_mult=2.5）。计划 md 显示真实盈亏比。

---

## 3. 测试策略

### 单测
- **R1**：scan_live 加 cancel_on 预判单测——close ≥ cancel_on 返 []，close < cancel_on 正常返信号
- **R2**：detect 加窗口已突破单测——窗口除末根有 close>颈线 返 None，无则正常
- **R3**：detect rr 实际口径单测（actual_rr 正确）+ Signal 透传 + plan md 展示 rr
- **回归**：既有 test_neckline_recognition.py / test_neckline_core.py 不破

### 全市场回测对比（关键 gate）
- 用 R1/R2/R3 改后的 detect + scan_at 跑全市场回测
- 对比冠军 neckline_disc_20260725_25c602 的 outer calmar（memory 记 7.24）
- **若 outer calmar 退化 > 20%**：说明修复改变了信号分布，需重做 discovery（参数搜索）
- **若不退化或提升**：修复纯增益（挡掉废单/失效信号），merge

---

## 4. 非目标（YAGNI）

- **完整性 gate**（数据问题，另 session 处理，本 spec 不碰）
- **simulate_exit 接入 scan_live**（保持实盘无前视，用 cancel_on 预判替代）
- **trailing stop**（冠军已关 trailing_grace=0/step=0，不动）
- **数据缺口修复**（300214.SZ lake 缺 07-14~07-21，另 session 处理）

---

## 5. 实现步骤（高层，详细计划见 writing-plans）

1. R2：detect 加窗口已突破过滤（method_v0.py）+ 单测
2. R3：detect rr 改实际口径（method_v0.py）+ Signal/plan/engine/push 透传链路 + 单测
3. R1：scan_live 加 cancel_on 预判（neckline_method.py）+ 单测
4. 全市场回测对比（confirm outer calmar 不退化）
5. commit + 总结
