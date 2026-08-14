> 最近复核：2026-08-14 · 维护者：glm-5.3-session ·
> 权威归宿：**job 时间窗 / 时钟语义**（单一归宿）。进程/调度结构见 [#4](04-process-topology.md)；时钟统一改造细节见 memory C6。

# #8 控制 / 时间流

quanter 的时间模型：单一时钟源（C6 `trading/clock.py`）+ 各 job 的时间窗 + 读写口径命名区分（防 T+1 错位）。本视图是 [#4](04-process-topology.md) 拓扑的「时间切片」——只画带时间语义的 job，不重复拓扑结构。

## 交易日时间轴 + 时钟源

```mermaid
flowchart LR
  Clock["🕐 trading/clock.py<br/>单一时间源<br/>now / today / trading_day"]

  subgraph Day["交易日时间轴（T 日）"]
    direction LR
    EOD["T-1 盘后<br/>eod"] --> PRE["T 09:22–10:00<br/>pre_open 窗口"]
    PRE --> SL["T 盘中<br/>stop_loss 监控"]
    SL --> PC["T 盘后<br/>post_close"]
    PC --> BR["T 盘后<br/>brief 播报"]
  end

  Clock -.读/写统一.-> EOD
  Clock -.读/写统一.-> PRE
  Clock -.读/写统一.-> PC

  EOD -.写 trading_day<br/>=next_trading_day(today).-> Ready["data_ready / account_daily.start<br/>(次日熔断基线)"]
  PRE -.读 plan_date = T+1.-> Submit["submit_order"]
```

## 时间语义表

| job | 时段 | 读口径 | 写口径 | 关键不变量 |
|---|---|---|---|---|
| `eod` | T-1 盘后 | `today` | `trading_day = next_trading_day(today)` → data_ready | 写次日 `data_ready`（为 T 日 pre_open 备数据闸）。**08-14 订正：eod/pipeline 不写 account_daily**（`start` 唯一写入方在 pre_open） |
| `pre_open` | T `[09:22, 10:00)`（env 可调） | `plan_date = trading_day`（=T+1 写入值） | trade_event(SIGNAL)/plan + **account_daily.start**（query_asset 精确抓取，失败 T-1 close 兜底——DG-G3） | **窗口过只补数据 + CRITICAL**（不补挂单） |
| `stop_loss` | T 盘中 | `today` | position.current_stop / trade_event | 盘中监控，L2 单只拒单；**只做 per-position 止损，不做组合级权益判定**（CR-3） |
| `post_close` | T 盘后（15:30 cron） | `today` | position + account_daily.close | 日终闭合（fill 累加↔position，account_daily start+close 非空）；**「日内 -3% 熔断」唯一判定点在此——盘后确认、次日生效（CR-3）** |
| `brief` | T 盘后 | `today` | job_ledger brief_<bot> | 播报幂等（第 8 域） |
| `data_pipeline`（schtasks） | 定时 daily | — | data_lake parquet | 独立进程（[T15](../../plans/wayfinder/T15.md) env 合成） |
| `discovery`（schtasks） | 02:00 daily | — | experiments.db | DETACHED daemon |

## 时钟统一（C6）—— 防漂移陷阱

- **单一时间源** `trading/clock.py`：`now()` / `today()` / `trading_day`（读/写口径命名区分）。全包 `datetime.now()` 收口于此（grep 仅 clock.py 有直接 `datetime.now`）。
- **入口缓存防漂移**：`_eod` 用 `_today`/`_td` 读/写分流；`pre_open` 内部用传入 `date` 参数，不直接读 clock。
- **T+1 错位修复（memory）**：旧 `_eod` 用 `today` 落盘 → pre_open 次日读 T+1 永远差一天 → 永不挂单。修复后 eod 写 `next_trading_day`、pre_open 读 `plan_date` 对齐。
- **测试单口子**：`monkeypatch trading.clock`（C6 收口后唯一注入点）。

## 时区（C9 / T6 配套）

- 全程 `Asia/Shanghai`（前端 `toLocalDateStr` + vitest TZ；C9 T6 收口）。
- `trade_cal` / 日历读取统一走 Tushare 交易日历（C-9 T0-fix sys.path 解阻后）。

## 已知时间相关风险（详见 [#6](06-tech-debt.md)）

- **~~`account_daily.start` 漏采 → 熔断基线裸奔~~ ✅ 已治（DG-G3 · 2026-08-13）**：基线缺失判定层 fail-closed（live `_CriticalHalt` / dry 停手）+ pre_open 精确抓取 / T-1 close 兜底双路（`phases/pre_open.py:363-418`）。残留：10:00 后启动当日 start 无人写 → 走 T-1 兜底或 fail-closed 停手（不再裸奔）；**curr_equity 缺失方向仍静默跳过（CR-4）**。
- **「日内熔断」的时间语义（CR-3）**：`check_daily_loss_limit` 唯一判定点 = 15:30 post_close，非盘中——盘中组合级 -3% 回撤零实时保护（stop_loss 30s 巡检只做 per-position 止损）。
- **lifespan 补跑时序**：启动补跑读 `job_ledger` 判四态，窗口过不补挂单（C8）。
- **P2-ts 时间戳三口径**：clock / state_store / tasks_db 的 tz 语义统一（Asia/Shanghai aware 分库迁移）未做（审计 spec P2-ts 待领取）。
