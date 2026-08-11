---
title: account_daily.start 漏采修复——开盘前基线告警 + T-1 close 补基线兜底
type: design/spec
status: draft
date: 2026-08-11
related: [docs/architecture/06-tech-debt.md, trading/compute/breaker.py, plans/wayfinder/T13-blueprint.md]
---

# account_daily.start 漏采修复

## 背景（根因链 · 代码实证）

C-1 日内 -3% 熔断的基线 = `account_daily.start_total_asset`（pre_open 写 / post_close 读，B5/W4 已收口同表）。

**漏采链**：
1. `_pre_open_impl`（`trading/phases/pre_open.py:312-331`）抓基线：`query_asset` 返空（:326）/ `gw=None`（:330）→ **只 `logger.warning`，未推 CRITICAL 告警**（对比同函数 gate 未过 :210 推 `_alert_critical` 钉钉）
2. → `start_total_asset` 未写
3. → `post_close`（`trading/phases/post_close.py:280`）`get_start_equity` 返 None → `breaker_skipped=True` 跳过熔断（:284）+ WARN
4. → **日内 -3% 回撤熔断失效，穿仓风险**

非盘前启动 / 网关未连 / 模拟盘 → 基线缺失**静默延续到 post_close（15:30 盘后才发现，盘中已过无法补）**。`check_daily_loss_limit`（`trading/compute/breaker.py:50`）`start<=0 → False` 的边界是第二道防线（防拿 0 误判），但根本问题是**基线抓取失败时无人知情**。

## 目标

1. **pre_open 抓基线失败时 live 模式推 CRITICAL 钉钉**——开盘前就叫醒用户（可手动 `trigger_pre_open_once` 补），不再静默到盘后
2. **补基线兜底**：post_close 发现 start 缺失时，用 **T-1 收盘总资产**（`account_daily.close_total_asset`）作 start 近似——熔断仍能工作，不裸奔

## 方案

### ① pre_open 抓基线失败补 CRITICAL（pre_open.py:326 / :330）

三处 `logger.warning`（query_asset 返空 :326 / gw=None :330 / 异常 :328）→ 追加 live 模式 `_alert_critical`，与 gate 未过（:210）同通道：

```python
# :326 query_asset 返空 / :330 gw=None
msg = f"pre_open 跳过熔断基线快照：query_asset 返空 date={today_eq}"
logger.warning(msg)
if _mode() == "live":
    _alert_critical(msg + "（开盘前基线缺失，C-1 熔断将失效，人工 trigger_pre_open_once 补）")
```

### ② post_close T-1 close 补基线兜底（post_close.py:280）

`start_equity is None` 时，不立即 `breaker_skipped`，而是尝试 T-1 close 补基线：

```python
if start_equity is None or start_equity <= 0:
    # 补基线兜底：T-1 收盘总资产作 start 近似（隔夜无交易，T-1 close ≈ T open）
    prev_close = _state_store.get_prev_close_equity(account_id, today_eq)
    if prev_close is not None and prev_close > 0:
        start_equity = prev_close
        logger.warning("post_close 用 T-1 close=%s 作 start 基线近似 date=%s（pre_open 未抓到）",
                       prev_close, today_eq)
        if _mode() == "live":
            _alert_critical(f"post_close 熔断基线用 T-1 close={prev_close} 近似 date={today_eq}"
                            "（pre_open query_asset 失败，开盘前未抓到精确基线）")
    else:
        breaker_skipped = True
        logger.warning("post_close 跳过日内熔断：无 start 且无 T-1 close date=%s", today_eq)
```

**新增 helper**：`state_store.get_prev_close_equity(account_id, date) -> float | None`
- 读 `account_daily` T-1（上一交易日，`clock.pretrade_date`）的 `close_total_asset`
- T-1 行不存在 / close 为 NULL → 返 None

## 物理依据（T-1 close 作 start 近似的合理性）

- **隔夜无交易**：T 收盘 → T+1 开盘期间持仓不变，仅隔夜利息/分红/送转微调
- **偏差量级**：隔夜跳空通常 <3%（熔断阈值 -3% 有充足余量区分「正常隔夜跳空」vs「日内异常回撤」）；极端跳空（如重大利空）本就该触发风控关注
- **daily_pnl 语义**：`close_T - close_{T-1}` = 当日盈亏（相对前日收盘），是标准的「日内回撤」口径，与 `start=open_asset` 语义一致

## 接入点

| 改动 | 文件:行 |
|---|---|
| pre_open query_asset 返空补 `_alert_critical` | `trading/phases/pre_open.py:326` |
| pre_open gw=None 补 `_alert_critical` | `trading/phases/pre_open.py:330` |
| post_close start 缺失 → T-1 close 补基线 | `trading/phases/post_close.py:280-287` |
| 新增 `get_prev_close_equity` helper | `trading/state_store.py`（get_start_equity :825 旁） |

## 降级 / 风险

- **T-1 close 近似精度**：隔夜分红/利息致 T 开盘 ≠ T-1 收盘，但偏差 <1%（-3% 阈值充足余量），作 fallback 可接受
- **T-1 close 也缺**（连续异常日 / 首个交易日）→ 走原 `breaker_skipped`（不拿 0 触发，防 check(0,X) 永远 False 反永不熔断）
- **告警不熔断**：pre_open CRITICAL 只通知不强停（避免冷启动误停，与 breaker.py:43「冷启动首日让引擎继续」语义一致）
- **post_close 补基线告警**：告知用户基线是近似值（人工复盘时知悉精度边界）

## 验收

1. **pre_open 告警**：`test_pre_open_snapshot_skip_when_query_asset_empty` 扩展——live 模式 query_asset 返空 → `_alert_critical` 被调（原只 warning）
2. **补基线兜底**：post_close start 缺失 + T-1 close 有值 → 用 T-1 close 作 start 判熔断（**不** `breaker_skipped`）
3. **T-1 也缺**：post_close start 缺失 + T-1 close 也缺 → `breaker_skipped`（不拿 0）
4. **helper**：`get_prev_close_equity` T-1 行存在 → 返 close_total_asset；不存在 → 返 None
5. **回归**：现有 pre_open/post_close/state_store 单测全绿
