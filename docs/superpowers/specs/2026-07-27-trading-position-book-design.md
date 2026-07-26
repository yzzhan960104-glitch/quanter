# 二期交易引擎 gap4：本地持仓账本（position_book · SQLite）

- **日期**：2026-07-27
- **分支**：master
- **状态**：待审（spec review gate）
- **关联**：`2026-07-21-auto-trading-engine-design.md`（二期引擎原始设计）、`2026-07-23-auto-trading-rehearsal-design.md`（彩排）
- **范围**：补完二期自动交易引擎 4 个数据源 gap 中**唯一未接通的 gap4**（post_close 对账缺 local_positions），并冒烟验证 gap1/2/3/4 全链路闭环。

---

## 1. 背景与现状诊断

二期引擎四 cron 触发点（`trading/engine.py`）此前的「数据源层 4 个 gap」逐一核对（基于 master HEAD `ea5e345c` 当前代码，**非旧快照**）：

| Gap | 触发点 | 现状 | 证据（行号） |
|---|---|---|---|
| **gap1 signals** | `_eod` → `eod_plan` | ✅ **已接通** | `engine.py:636-683`：`resolve_active()` 读在线实验 → `pd.read_parquet("data_lake/a_shares_daily.parquet")`（入口只读一次）→ `_load_universe(lake)` 过滤创板科创（300/301/688/689）→ 逐 symbol `_load_df_upto(lake, sym, today)` → `strategy.scan_live(sym, df_upto, today)` 产信号 → 注入实验归因 → `await eod_plan(today, signals, atr_map, capital=...)` |
| **gap2 atr_map** | `_eod` → `eod_plan` | ✅ **已接通（且冗余双保险）** | `engine.py:675-676`：`if s.atr: atr_map[sym] = s.atr`；且 `compute/plan.py:89-90` 已优先从 `signal.atr` 取，`scan_live` 返回的 Signal 自带 `atr`（`neckline_method.py:199`） |
| **gap3 stop_prices** | `_stoploss` → `stop_loss_monitor` | ✅ **已接通** | `engine.py:810-824`：`load_plan(today)` → 仅 confirmed 计划抽 `stop_prices[sym]=stop_price`，双重防御（sym 缺/stop_price 非数跳过），空时显式转 `None` |
| **gap4 local_positions** | `_post_close` → `post_close` | ❌ **唯一真 gap** | `engine.py:826-831`：`_post_close` 仍 `await post_close(today)` **未传 local_positions** → `post_close`（`engine.py:476`）走 `else` 分支跳过对账 → drift 恒 False = **伪对账** |

辅助数据源函数均已就绪：
- `_load_universe`（`engine.py:100`）：创板科创前缀过滤，零 I/O（复用 lake）
- `_load_df_upto`（`engine.py:122`）：`xs(level="symbol").sort_index().loc[:date]`，严格无前视
- 性能不变量（Task 7b fix）：lake 入口只读一次，1993 标的复用同一 DataFrame（历史 bug 58 分钟纯 I/O 已修）

**结论**：本次实际工作量 = 补 gap4 + 全链路冒烟。gap1/2/3 代码已就位，仅需 end-to-end 验证。

---

## 2. 目标与非目标

### 目标
1. 新建 `trading/position_book.py`：SQLite 本地持仓账本，记录「本地系统记账的理论持仓」（独立于 broker 真实持仓）
2. `_handle_order_update`（成交回报 handler）写入：BUY 成交 +qty、SELL 成交 -qty，按 `order_id` 幂等去重
3. `_post_close` 读取账本 → 传 `local_positions` 给 `post_close`，对账链路真跑
4. dry_run 冒烟：跑通 `_eod → 计划落盘 → _post_close 对账` 全链路，证 4 gap 闭环
5. 单测覆盖：position_book 读写/幂等、_post_close 注入 local_positions、_handle_order_update 写入、review_report 生成
6. **新建 `trading/review_report.py`**：最小复盘报告生成器（读 fill 表 + 计划 + 收盘持仓 + drift → markdown），作为 e2e 第 4 步依赖
7. **e2e 端到端验证完整交易链路 4 步**：① 检查数据时效性（`data/freshness.check_freshness`）② 生成交易计划（`_eod`→`eod_plan`→`save_plan`）③ 隔日按计划交易（`confirm_plan`→`_pre_open`→成交回报写账本）④ 生成复盘报告（`review_report.generate_review`）

### 非目标（显式 out of scope）
- **部分成交累加精度**：Phase1 按 `order_id` 整笔去重（首次成交的 `traded_volume` 入账），不处理同 order_id 多次部分成交的单调累计。dry_run 不触发此路径，live 精度属「二期 live 前必修」follow-up
- **post_close 熔断连线**（`check_daily_loss_limit + cancel_all + emergency_halt`）：仍按 `engine.py:459-469` 现状留 follow-up，本 task 不做
- **trailing stop 动态更新**：`_stoploss` 注入的是计划内静态 stop_price（`engine.py:800-802`），时间驱动 trailing 留 follow-up
- **Cockpit 本地侧浮盈富化**：`get_positions` 已从 broker 拿 `avg_price` 算浮盈，本地账本不重复存 avg_price

---

## 3. 架构设计

### 3.1 模块定位

新建 `trading/position_book.py`，定位对齐 `trading_plan.py`（T-1 计划 JSON）、`io/positions.py`（broker 持仓查询）——**纯 I/O 模块，只搬运不判定**。本地持仓账本是 post_close 对账的「本地侧」单一真理源。

**为什么 SQLite 而非 JSON**（design ADR，对齐 `experiment/store.py` ADR3）：
- **幂等天然**：`fill` 表 `UNIQUE(order_id)` 约束让重推 INSERT 失败即跳过，无需手写 `processed_orders` 集合
- **事务一致性**：写流水 + 更新持仓在同一事务内原子提交（写一半崩溃不 corrupt）
- **并发安全**：WAL 模式多读单写，engine 常驻进程 + 单测可并发
- **审计可追溯**：`fill` 表即成交流水，对账偏差可回溯逐笔

### 3.2 存储

DB 路径 `logs/trading_state.db`（运行时状态，对齐 `logs/discovery_trials.db` 惯例）。可由 env `TRADE_STATE_DB` 覆盖（独立进程 env 与 server 解耦）。

**表结构**：

```sql
-- 持仓账本：每个 symbol 当前净持仓（对账读这张）
CREATE TABLE IF NOT EXISTS position (
    symbol     TEXT PRIMARY KEY,
    qty        REAL NOT NULL,        -- 净持仓（BUY 累加 / SELL 累减）
    updated_at TEXT NOT NULL         -- ISO 时间戳，最近一次变更
);

-- 成交流水 + 幂等去重（UNIQUE(order_id) 天然防重推）
CREATE TABLE IF NOT EXISTS fill (
    fill_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    direction  TEXT NOT NULL,        -- "BUY" / "SELL"
    qty        REAL NOT NULL,        -- 成交量（股）
    price      REAL NOT NULL,        -- 成交价（审计/复盘用，不参与持仓计算）
    applied_at TEXT NOT NULL,
    UNIQUE(order_id)                 -- Phase1 整笔去重：同 order_id 重推 INSERT 失败即跳过
);
CREATE INDEX IF NOT EXISTS idx_fill_symbol ON fill(symbol);
```

### 3.3 模块函数（复用 `experiment/store.py` 的 `_connect`/WAL 范式）

```python
# trading/position_book.py
_DEFAULT_DB = "logs/trading_state.db"

@contextmanager
def _connect(db_path):
    """连接上下文：开 WAL，提交/回滚自动。SQLite 连接非线程安全，每次操作新建。"""
    # 复用 experiment/store.py:_connect 范式（WAL + row_factory + commit/rollback）
    ...

def init_db(db_path=_DEFAULT_DB) -> None:
    """幂等建表（CREATE TABLE IF NOT EXISTS）。由 trading/__main__ 启动期调用。"""
    ...

def apply_fill(order_id, symbol, direction, qty, price, *, db_path=_DEFAULT_DB) -> bool:
    """成交回报应用：写流水 + 更新持仓（单事务原子）。

    幂等：order_id 已存在 → INSERT 抛 IntegrityError → 返 False（重推跳过）。
    方向：BUY → +qty；SELL → -qty；其它 → 抛 ValueError（调用方应已过滤 None 方向）。
    清理：持仓归零的标的 DELETE（保持账本干净，对账并集不被 0 干扰）。
    返回：True=首次应用；False=重复跳过。
    """
    ...

def get_local_positions(*, db_path=_DEFAULT_DB) -> dict[str, float]:
    """读本地理论持仓 {symbol: qty}（qty!=0）。供 _post_close 对账用。"""
    ...
```

### 3.4 写入点：`_handle_order_update`（engine.py:834）

在成交回报 handler 的 `kind=="trade"` 分支，方向判定后追加写入（**三连之外的第四连**）：

```python
# 现有三连：a. record_live_trade  b. notify_trade_event  c. _place_take_profit
# 新增第四连 d. position_book.apply_fill（独立 try-except 软降级，不阻断前三连）
if direction in ("BUY", "SELL"):   # None 方向不写（保守，对齐不挂止盈语义）
    try:
        from trading import position_book
        position_book.apply_fill(order_id, symbol, direction, float(qty), float(price))
    except Exception:
        logger.exception("本地账本写入失败 symbol=%s（不影响日志/通知/止盈）", symbol)
```

**关键约束**：
- **不在 pre_open 挂单时写**（挂单 ≠ 成交，写了就跟 broker 偏离）
- **不在 stop_loss / _place_take_profit 下卖出单时写**（下单 ≠ 成交，等成交回报回流）
- **唯一写入点 = 真实成交回报**，与 `record_live_trade` 同源同触发

### 3.5 读取点：`_post_close`（engine.py:826-831）

```python
async def _post_close(self) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    if not calendar.is_trading_day(today):
        logger.info("post_close 跳过：今日非交易日 %s", today)
        return
    # gap4 fix：读本地账本 → 注入 local_positions，对账链路真跑
    from trading import position_book
    local_positions = position_book.get_local_positions()
    await post_close(today, local_positions=local_positions)
    # ⚠️ 空 dict 直接传，不转 None：live 下账本空但 broker 有持仓（疑似外部单）时，
    # reconcile(local={}, broker={有}) 会报 only_broker drift——转 None 会让 post_close
    # 走跳过分支漏报（对账查漏价值丧失）。dry_run 下 gw=None 时 post_close 内部自然跳过。
```

### 3.6 dry_run 自洽性（关键不变量）

- 影子模式（`AUTO_TRADE_MODE=dry_run`）：`_handle_order_update` **根本不触发**（dry_run 不真下单 → QMT 无真实成交回报）→ 账本 `position` 表恒空
- `post_close` 对账分情形（fix 后全诚实，无非伪对账）：
  - **dry_run + gw 未装配**（影子期常态）：`post_close` 内 `gw is None` → 走跳过分支（无 broker 源，不对账，drift 字段不出现）。现状同此，无回归。
  - **dry_run + gw 已装配**（模拟仓连接）：local={} + broker={}（dry_run 不真下单）→ reconcile 空并集 `is_ok=True` → drift=False（真 ok）。
  - **live 正常**：local（真实成交累计）+ broker（真实持仓）→ 正常对账。
  - **live 异常**（账本空但 broker 有，疑似外部单）：local={} + broker={有} → 报 only_broker drift（**对账查漏价值，转 None 会漏报**）。
- **现状对比**：fix 前 `_post_close` 传 `local_positions=None`（缺省）→ `post_close` 走「跳过对账」分支，drift 恒不出现 = 伪对账；fix 后注入真实账本，drift 反映真实偏差。
- **对账检测能力的验证**：单测用 mock broker 持仓 + mock 账本故意造偏差，证 `run_reconcile` 的 drifted/only_local/only_broker 三类检测**真跑**（而非恒跳过）

### 3.7 启动期初始化

`trading/__main__.py` 启动期（`_run_forever` 内，engine.start() 之前）调 `position_book.init_db()`，幂等建表。对齐 `experiment/store.py:init_db` 范式。

### 3.8 复盘报告生成器（e2e 第 4 步依赖，新建 `trading/review_report.py`）

最小版——聚合 position_book.fill 表（当日成交流水）+ trading_plan（当日计划）+ position 表（收盘持仓）+ post_close drift，输出 markdown 报告。数据源全部已就绪，零新 I/O 通道。

```python
# trading/review_report.py
def generate_review(
    date: str,
    *,
    db_path: str = position_book._DEFAULT_DB,
    plan: dict | None = None,       # 透传避免重复 load；None 则内部 load_plan(date)
    drift: bool | None = None,      # post_close 对账结果（None=未对账）
) -> str:
    """生成 T 日交易复盘 markdown（计划/成交/持仓/对账四段）。返回 markdown 字符串。"""
    ...

def save_review(date: str, md: str, *, review_dir="logs/trading_reviews") -> Path:
    """落盘 logs/trading_reviews/review_<date>.md（幂等覆盖，父目录自动建）。"""
    ...
```

报告字段（最小集，对齐 e2e 断言）：
- **计划段**：N 单（symbols + 各单 side/qty/price/stop/tp）
- **成交段**：buy M 笔 / sell K 笔（fill 表 `applied_at LIKE date%` 聚合）+ 每笔均价
- **持仓段**：收盘 position 表（{symbol: qty}）
- **对账段**：drift 状态（True=有偏差/False=ok/None=未对账）

**为什么最小版而非全功能复盘**：本 task 核心是 gap4 对账链路；复盘报告是 e2e 链路终点的「可观测产物」，最小集够验证链路通。全功能复盘（按 experiment_id 聚合 PnL/胜率/Sharpe + 推钉钉）属「Layer 6 LLM 复盘」范畴，留 follow-up。

---

## 4. 风险红线与拷问（Grill Me）

### R1：重推幂等（核心红线）
**拷问**：QMT 成交回报在部分成交/柜台重推时多次推同一 order_id 的 trade，会不会重复加减持仓？
**防御**：`fill` 表 `UNIQUE(order_id)` 约束 → 重推 INSERT 抛 `IntegrityError` → `apply_fill` 返 False 跳过，持仓不重复加减。
**已知精度缺口**：Phase1 按 order_id 整笔去重，同 order_id 多次部分成交只计首次的 `traded_volume`。dry_run 不触发；live 精度属 follow-up（需按 `gw._orders[order_id].qty_traded` 单调累计取增量）。

### R2：事务原子性（账本完整性）
**拷问**：写流水成功但更新持仓时进程崩了，账本会 corrupt 吗？
**防御**：`apply_fill` 在单个 `_connect` 事务内执行 INSERT fill + UPSERT position + DELETE qty=0，事务自动 commit/rollback（`experiment/store.py:_connect` 范式），崩溃回滚不 corrupt。

### R3：方向未知不猜
**拷问**：成交回报方向查不到（`_order_direction` 返 None）怎么办？
**防御**：`direction not in ("BUY","SELL")` 时**不写账本**（对齐现有"保守不挂止盈"语义）。宁可账本漏记（对账 drift 暴露人工排查），不猜方向误记（买当卖 / 卖当买 = 账本失真）。

### R4：dry_run 不污染
**拷问**：影子模式会不会把 DRY_RUN 单写进账本，导致对账永远 drift？
**防御**：dry_run 不真下单 → 无真实成交回报 → `_handle_order_update` 不触发 → 账本恒空。与 broker(空) 诚实一致。`record_live_trade` 虽在 dry_run 也写 CSV，但那是日志层（不参与对账），账本层严格只跟真实成交走。

### R5：进程重启恢复
**拷问**：engine 常驻进程重启后，账本会不会丢？
**防御**：SQLite 持久化在 `logs/trading_state.db`，重启后自然恢复。补了 `engine.py:586` `_tp_placed` 内存集合的同款缺口（重启后柜台重推历史 trade 不重复入账）。

### R6：并发写
**拷问**：多个 cron job 并发写账本会冲突吗？
**防御**：WAL 模式多读单写；engine 是单进程常驻，cron job 在 AsyncIOScheduler 单事件循环串行触发，无并发写。单测虽多线程跑，但每个测试用临时 db 隔离。

---

## 5. 测试策略

### 5.1 单元测试

**`tests/trading/test_position_book.py`（新建）** —— position_book 模块
- `test_apply_fill_buy_accumulates`：BUY 两次 → qty 累加
- `test_apply_fill_sell_decrements`：BUY 后 SELL → qty 减；归零则从 position 表 DELETE
- `test_apply_fill_idempotent`：同 order_id 重推 → 返 False，qty 不变（R1 红线）
- `test_apply_fill_unknown_direction_raises`：direction 非 BUY/SELL → 抛 ValueError
- `test_get_local_positions_excludes_zero`：qty=0 的不返回
- `test_init_db_idempotent`：重复调不报错

**`tests/trading/test_review_report.py`（新建）** —— review_report 模块
- `test_generate_review_sections`：计划/成交/持仓/对账四段齐全
- `test_generate_review_empty_plan`：无计划 → 报告标「无计划」不崩
- `test_save_review_idempotent`：重复写覆盖

**`tests/trading/test_engine.py`（扩展）** —— engine 集成
- `test_post_close_reads_position_book`：monkeypatch `get_local_positions` 返非空 → `post_close` 调 `run_reconcile`
- `test_post_close_empty_book_passes_empty_dict`：账本空 → 传 `{}`（非 None）→ live gw 下 reconcile 空并集 `is_ok=True`
- `test_handle_order_update_writes_book`：BUY 成交回报 → `apply_fill` 被调；方向 None → 不调
- `test_handle_order_update_book_failure_soft_degrades`：apply_fill 抛异常 → 不阻断 record_live_trade / notify / _place_take_profit

### 5.2 端到端 e2e：完整交易链路 4 步（`tests/trading/test_e2e_trading_flow.py`，新建）

**本次交付的核心验收**——验证完整交易链路 4 步闭环。跨日时序用 mock `datetime` 注入（不真睡），broker 走 mock gw（dry_run 不触达真实柜台），data_lake 用 tmp parquet。

**第 1 步 · 检查数据时效性**
- 造小样本 parquet 到 tmp `data_lake/a_shares_daily.parquet`（最新日期 = T 日）
- `check_freshness("daily", expected_date=T, lake_dir=tmp)` → 断言 `result.ok is True`
- 反例：parquet 最新日 = T-1 → 断言 `result.ok is False`（陈旧能检出）

**第 2 步 · 生成交易计划（T 日盘后）**
- mock `resolve_active` 返 1 个颈线法实验 + mock `data_lake` 含 T 日突破小样本
- monkeypatch `datetime` 让 `_eod` 拿到 T 日；跑 `await eng._eod()`
- 断言 `load_plan(T+1)` 返非 None、`confirmed is False`、orders 非空（gap1/2 实证）

**第 3 步 · 隔日按计划交易（T+1 日）**
- monkeypatch `datetime` 到 T+1 日
- `trading_plan.confirm_plan(T+1)` → 断言 `confirmed is True`
- monkeypatch `engine.get_gateway` 返 mock gw + `_submit` 返 `{"state":"DRY_RUN"}`
- 跑 `await eng._pre_open()` → 断言挂单计数 > 0（gap3 实证：confirmed 才挂）
- 模拟成交回报：`await eng._handle_order_update({kind:"trade", stock_code, traded_volume, traded_price, order_id})` → 断言 `position_book.get_local_positions()` 反映该成交（gap4 写入实证）

**第 4 步 · 生成复盘报告（T+1 日盘后）**
- 跑 `await eng._post_close()` → 对账（mock gw 持仓 vs 账本，断言 drift 可计算）
- `review_report.generate_review(T+1, drift=...)` → 断言返非空 markdown，四段齐全
- `save_review` → 断言文件落盘

**全链路断言**：4 步串行跑完无异常 + 计划单 symbol 贯穿出现在 fill 表 / position 表 / 复盘报告（数据一致性）。

### 5.3 dry_run 自洽冒烟
`trading/tools/smoke_trading_engine.py` 若已存在则扩展跑 e2e 4 步 dry_run 版；不存在则 5.2 单测 e2e 已覆盖，不额外造工具脚本。

---

## 6. 实现步骤（高层，详细计划见 writing-plans）

1. 新建 `trading/position_book.py`（`_connect`/`init_db`/`apply_fill`/`get_local_positions`，复用 experiment/store.py 的 WAL 范式）
2. 新建 `trading/review_report.py`（`generate_review`/`save_review`，读 fill 表+计划+持仓+drift）
3. `trading/__main__.py:_run_forever` 加 `position_book.init_db()`（engine.start() 前）
4. `trading/engine.py:_handle_order_update` 加第四连 `apply_fill` 写入（BUY/SELL only，独立 try-except）
5. `trading/engine.py:_post_close` 改读账本传 `local_positions`（空 dict 直传）
6. 单测：`tests/trading/test_position_book.py` + `test_review_report.py` + 扩展 `test_engine.py`
7. **e2e**：`tests/trading/test_e2e_trading_flow.py` 验证完整交易链路 4 步（数据时效 → 计划 → 隔日交易 → 复盘报告）

---

## 7. Follow-up（live 前必修，本 task 显式不做）

1. **部分成交累加精度**：`apply_fill` 升级为按 `gw._orders[order_id].qty_traded` 单调累计取增量（替代 order_id 整笔去重）
2. **post_close 熔断连线**：定 equity 源（`gw.query_asset`）后串联 `check_daily_loss_limit + cancel_all + emergency_halt`（`engine.py:459-469` TODO）
3. **trailing stop 动态更新**：盘中按持仓最高价更新 stop_prices map（海龟 grace/step/floor）
4. **EMT/xtdata 行情源**：`stop_loss_monitor` 现价走 `qmt_market_data.get_quotes`（xtdata），EMT 网关无此行情源（`engine.py:340-344`）——切 EMT live 前必修
