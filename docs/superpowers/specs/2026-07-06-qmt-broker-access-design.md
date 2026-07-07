# QMT 实盘接入完成 · 设计稿

> 日期：2026-07-06
> 范围：补全国金 MiniQMT (xtquant) 实盘接入的「装配缺口 + 风控收口 + 行情 + 联调验收」
> 状态：已与研究员对齐方案（后端 API 收口 + 最严风控挡板 + 一并接入 xtdata 行情）

---

## 1. 背景与现状

### 1.1 不是从零接入——骨架已存在且经得起事实审查

前几轮重构已经把 QMT 网关骨架打得相当扎实。逐一核对 `xtquant/xtconstant.py`、`xtquant/xttrader.py`、`xtquant/doc/xttrader.md` 后确认现有 `trading/qmt_gateway.py` 的 API 调用**全部正确**：

| 核对项 | 官方事实 | `qmt_gateway.py` 现状 | 结论 |
|---|---|---|---|
| 买卖方向 | `STOCK_BUY=23 / STOCK_SELL=24` | 同 | ✅ |
| 报价类型 | `LATEST_PRICE=5 / FIX_PRICE=11` | 同 | ✅ |
| 委托状态 7 枚举 | `48/50/51/54/55/56/57` | 字面量逐一对齐 + connect 时强校验防版本漂移 | ✅ |
| `order_stock_async` 8 参数 | `(account, stock_code, order_type, order_volume, price_type, price, strategy_name, order_remark)` | 顺序完全一致 | ✅ |
| `cancel_order_stock(account, order_id)` → `0/-1` | 同 | ✅ |
| 连接时序 | `XtQuantTrader(path,sid)→register_callback→start→connect==0→subscribe==0` | 同（且同步调用全投线程池） | ✅ |
| 线程边界 | xtquant 是同步 C++ 绑定 + C++ 线程回调 | `run_in_executor` + `call_soon_threadsafe` 严格执行 | ✅ |

**结论：`qmt_gateway.py` 核心逻辑（线程边界 / 状态映射 / seq↔real 映射 / 断线锁定）本期不动其内部**——只补它的外围。

### 1.2 真正的 4 个装配缺口

1. **`.env` 无任何 QMT 配置** → `trading_service.get_qmt_gateway()` 恒返 None，`/status` 恒 `unavailable`
2. **缺 6 个路由**：`/connect /disconnect /submit_order /cancel_order /orders /asset` → 网关有能力但 HTTP/前端触达不到
3. **`qmt_gateway.py` 零单测**（codegraph 确认 no covering tests）→ 重构无安全网
4. **`trading/__init__.py` 未导出 `QmtExecutionGateway`**

### 1.3 关键环境事实

- **账号**：`62138335`（资金账号，字符串；普通 STOCK 类型）
- **密码 `100486` 不进代码、不进环境变量**——xtquant 通过 MiniQMT 客户端 `userdata_mini` 目录与**已登录的客户端进程**通信，密码只在客户端登录界面输入
- **客户端路径**：`D:\国金QMT交易端模拟\`（已确认 `bin.x64/XtMiniQmt.exe`、`XtItClient.exe` 存在）
- **`userdata_mini` 路径**：`D:\国金QMT交易端模拟\userdata_mini`（**已确认生成**：模拟盘 `XtItClient.exe` 已启动登录，目录含 `miniqmtShm*` 共享内存 / `quoter` / `users`，xtquant 可直连）
- **环境定性**：当前为**模拟盘**（`XtItClient`），非真实柜台；下单/回报/持仓链路与实盘一致，但成交为模拟撮合，适合联调

---

## 2. 目标与非目标（Phase 1）

> **本 spec 为 3-phase epic 的 Phase 1**。Phase 2（前端 Cockpit 连接/下单 UI）、Phase 3（策略引擎自动实盘）在 Phase 1 验收后各自起独立 spec。Phase 2/3 概要见 §14。

### 2.1 目标（Phase 1 交付）

1. 补全 `.env` / `.env.example` 的 QMT 配置项
2. 新增 `trading/qmt_market_data.py`（xtdata 行情封装，延迟容错）
3. 新增 `trading/risk_shield.py`（下单风控挡板纯函数，10 关；**dry_run 为请求级参数**，前端按单传入；env `QMT_ALLOW_LIVE_TRADE` 作环境总闸）
4. 扩展 `server/services/trading_service.py`：connect/disconnect/submit_order/cancel_order/get_orders/get_asset；submit 经挡板
5. 扩展 `server/api/v1/trading.py`：6 个新路由（`/submit_order` body 含 `dry_run` 字段，为 Phase 2 前端控制预留）
6. **交易流水全覆盖**：dry_run / 被挡板拦截 / 真单成交 / 废单 / 撤单 五种情况均落 `live_trades.csv`，direction 字段区分（契约见 §6.3）
7. 新增 `tests/test_qmt_gateway.py` + `tests/test_risk_shield.py`（mock xtquant，CI 可跑）
8. 修复 `trading/__init__.py` 导出
9. 新增 `scripts/qmt_smoke.py`（首次真实联调脚本，分步人工确认）

### 2.2 非目标（Phase 1 不做，归入后续 phase）

- 前端 Cockpit 连接/下单/撤单 UI → **Phase 2**（本期 API 已暴露 `dry_run` 字段为其预留）
- 策略引擎自动实盘（executor / rebalance / 调度） → **Phase 3**
- 断线自动重连（本期仅断线锁定 + 人工 `connect` 复位；退避策略留后）
- 历史成交回测对账（`reconcile` 已存在，本期不接入调度）

---

## 3. 架构总览

```
HTTP/前端 ──► server/api/v1/trading.py（薄路由，run_in_threadpool）
                    │
                    ▼
        server/services/trading_service.py（业务 + 风控编排）
          │  ├─ get_qmt_gateway() 单例（已有 lazy）
          │  ├─ submit_order → risk_shield.check()（新增）
          │  ├─ connect/disconnect/cancel/orders/asset（新增）
          │  └─ get_positions 富化（接 qmt_market_data 补市值/盈亏）
          │
          ├──► trading/qmt_gateway.py（已有，零改动核心）
          │       XtQuantTraderCallback + run_in_executor + call_soon_threadsafe
          │
          ├──► trading/risk_shield.py（新增，纯函数挡板）
          │
          └──► trading/qmt_market_data.py（新增，xtdata 行情）
                  get_quote() → 涨跌停/盘口校验 + 市值
```

**设计纪律**：
- 薄路由层只做 HTTP 解耦与异常→HTTP 码映射，零业务逻辑
- 业务编排集中在 `trading_service`
- 风控挡板是**纯函数**（无 I/O、可单测），I/O 类关卡（涨跌停查询）由挡板接收预取的 quote 数据，挡板本身不发起网络调用——保证可确定性单测

---

## 4. 组件清单

| # | 文件 | 动作 | 职责 |
|---|---|---|---|
| 1 | `.env` + `.env.example` | 改 | 加 QMT_* 配置 |
| 2 | `trading/__init__.py` | 改 | 导出 `QmtExecutionGateway` |
| 3 | `trading/qmt_market_data.py` | 新增 | xtdata 延迟容错；`get_quote(symbol)` |
| 4 | `trading/risk_shield.py` | 新增 | 9 关纯函数挡板 → `RiskDecision` |
| 5 | `server/services/trading_service.py` | 改 | 6 个业务函数 + submit 经挡板 + positions 富化 |
| 6 | `server/api/v1/trading.py` | 改 | 6 个新路由 |
| 7 | `tests/test_qmt_gateway.py` + `tests/test_risk_shield.py` | 新增 | mock 单测 |
| 8 | `scripts/qmt_smoke.py` | 新增 | 真实联调脚本 |

---

## 5. 配置契约（`.env`）

```ini
# === QMT 实盘交易 ===
# userdata_mini 完整路径（MiniQMT 首启登录后自动生成）
QMT_USERDATA_PATH=D:\国金QMT交易端模拟\userdata_mini
QMT_ACCOUNT_ID=62138335
QMT_SESSION_ID=123456
QMT_STRATEGY_NAME=quanter

# === 风控挡板（环境级总闸；dry_run 由前端按单控制，不在此）===
QMT_ALLOW_LIVE_TRADE=false      # 实盘总闸：false=即使前端 dry_run=false 也强制模拟（拒真单）；true=放行前端实盘请求
QMT_ORDER_MAX_AMOUNT=1000       # 单笔金额上限（元）
QMT_ORDER_MAX_SHARES=100        # 单笔股数上限（联调期 1 手）
QMT_SYMBOL_WHITELIST=510300.SH,511010.SH,510500.SH,159915.SZ
QMT_ENFORCE_SESSION=true        # 是否强制 A 股交易时段校验
```

> **安全红线**：密码 `100486` 绝不出现在 `.env` 或任何代码中。`.gitignore` 已含 `.env`（核对 `.gitignore` 第一行）。

---

## 6. 风控挡板（`trading/risk_shield.py`）

### 6.1 设计：纯函数 + 预取数据

```python
@dataclass(frozen=True)
class RiskDecision:
    blocked: bool
    reason: str = ""
    stage: str = ""              # 命中的关卡名，便于审计

def check_order(
    order: OrderRequest,
    *,
    dry_run: bool,
    allow_live: bool,
    whitelist: set[str],
    max_amount: float,
    max_shares: float,
    quote: Mapping[str, Any] | None,   # 预取的 xtdata tick；None=跳过涨跌停关
    enforce_session: bool,
    is_locked: bool,
    connected: bool,
    confirm: bool,
) -> RiskDecision: ...
```

**为什么 quote 由外部预取传入，而非挡板内部拉取**：挡板若是纯函数（无 I/O），即可在 `test_risk_shield.py` 里确定性注入各种 quote 场景（涨停/跌停/正常/None）做穷举单测，无需 mock 网络。I/O 责任留在 `trading_service`。

**dry_run / allow_live 来源约定**（前端控制是否真实下单的核心机制）：
- `dry_run` 是**请求级**参数（`POST /submit_order` body 字段），由前端按单传入；`check_order` 的 `dry_run` 形参透传该值
- `allow_live` 来自 env `QMT_ALLOW_LIVE_TRADE`（环境级总闸，进程级常量）
- 组合语义：
  - `dry_run=true` → 模拟：不调 `order_stock_async`，落 `DRY_RUN_BUY/SELL` 流水（请求价量）
  - `dry_run=false` 且 `allow_live=false` → 拒单（强制模拟，防误触实盘）
  - `dry_run=false` 且 `allow_live=true` → 真下单，走网关

### 6.2 九关校验顺序（短路：任一不过即返 blocked）

| # | 关卡 | 拦截条件 | 对应 CLAUDE.md 拷问 |
|---|---|---|---|
| 1 | 断线/连接 | `is_locked or not connected` | 状态机边界 |
| 2 | dry_run（请求级） | `body.dry_run=true` → 不真下单，落 `DRY_RUN_*` 流水 | 前端控制模拟/实盘 |
| 3 | 实盘总闸（env） | `body.dry_run=false and not QMT_ALLOW_LIVE_TRADE` → 拒单 | 环境级硬开关，双保险 |
| 4 | 二次确认 | `not confirm` | 防误触 |
| 5 | 标的白名单 | `symbol not in whitelist` | 限定可操作标的 |
| 6 | 整手契约 | `qty % 100 != 0` 或 `qty<=0` | A 股契约 |
| 7 | 金额上限 | `qty*price > max_amount`（限价）/ 市价按 quote.last 估 | 单笔敞口硬顶 |
| 8 | 股数上限 | `qty > max_shares` | 单笔敞口硬顶 |
| 9 | 涨跌停封板 | `quote` 存在且 `last>=high_limit or last<=low_limit` | 流动性/极端行情 |
| 10 | 交易时段 | `enforce_session and not in_a_share_session()` | 防隔夜/非时段废单 |

> 共 10 关，自上而下短路；命名沿用「九关 + 时段」口语，实为 10 个检查点。

**HTTP 码统一约定**（消除歧义）：挡板任一关命中 → 路由返 `409 + 中文 reason`（reason 内嵌命中关卡名，前端据此分流提示）；不引入 449/425 等非标准码。所有挡板命中与 dry_run 模拟均落 CSV（契约见 §6.3）。

### 6.3 交易流水全覆盖契约（`live_trades.csv`）

**核心需求**（研究员明确要求）：无论模拟、被拦截、还是真实成交，**全部落 CSV**，供 Layer 6 LLM 复盘 + 实盘审计。`live_trades.csv` 既有 7 列（timestamp/symbol/direction/shares/price/strategy/rationale）不变，仅扩展 direction 取值集合。

| 触发场景 | 落 CSV 时机 | direction | shares/price 来源 | rationale |
|---|---|---|---|---|
| `body.dry_run=true` | `submit_order` 挡板第 2 关命中后立即 | `DRY_RUN_BUY` / `DRY_RUN_SELL` | 请求值（不查成交） | 策略/手动意图 |
| 挡板其他关命中 | 命中后立即 | `BLOCKED` | 请求值 | 命中关卡名（如"金额超限"） |
| 真单成交 | `on_stock_trade` 回调（主线程） | `BUY` / `SELL` | 真实成交量/成交价 | 策略 |
| 真单废单 | `on_order_error` 回调 | `REJECTED` | 请求值 | `error_msg` |
| 撤单确认 | `on_stock_order` 推 CANCELLED | `CANCEL` | 已成交部分 | — |

**实现要点**：
- `record_live_trade` 签名不变（已接受 `direction: str`），仅规范 direction 取值集合
- `trading_service.submit_order` 在挡板命中 / dry_run 时**同步**调用 `record_live_trade`（确保即使后续真单链路异常，意图也已留痕）
- 网关 `_process_order_update` 经上层回调在成交/废单/撤单时调用（异步，主线程安全）
- CSV 追加写（既有 `utf-8-sig` + 表头自适应），不引入 DB

---

## 7. 数据流（一笔下单的完整时序）

```
POST /api/v1/trading/submit_order {symbol, qty, side, price?, confirm}
  │
  ▼ server/api/v1/trading.py（薄路由）
  │
  ▼ trading_service.submit_order(order, confirm)
  │   ├─ 1. 预取 quote = await qmt_market_data.get_quote(symbol)   # 可 None
  │   ├─ 2. decision = risk_shield.check_order(...)                 # 9 关
  │   │      if decision.blocked:
  │   │          record_live_trade(direction="BLOCKED", ...)
  │   │          raise HTTPException(409, decision.reason)
  │   ├─ 3. (可选) MacroAwareGateway.submit_order(order, regime)    # 宏观一票否决
  │   └─ 4. result = await gw.submit_order(order)                   # run_in_executor
  │             → trader.order_stock_async(...) → seq
  │             → OrderResult(order_id=str(seq), SUBMITTED)
  │
  ▼ HTTP 200 {order_id: "<seq>", state: "SUBMITTED", message}

[异步 · xtquant C++ 线程]
  on_order_stock_async_response(seq, real_id)
    → _seq_to_real[seq] = real_id            # 撤单锚点建立
  on_stock_order / on_stock_trade
    → call_soon_threadsafe → _process_order_update (主线程)
      → 更新 _orders[order_id]
      → create_task(上层回调)
        → record_live_trade() 落 CSV
        → fire_and_forget(钉钉告警)          # 成交/废单/撤单
```

---

## 8. 行情接入（`trading/qmt_market_data.py`）

```python
try:
    from xtquant import xtdata
    _XTDATA_AVAILABLE = True
except ImportError:
    xtdata = None
    _XTDATA_AVAILABLE = False

async def get_quote(symbol: str) -> dict[str, Any] | None:
    """经线程池取 xtdata.get_full_tick([symbol])，返单标的快照 dict。
    None = xtdata 不可用 / 无行情；调用方（risk_shield / positions）须容忍 None。"""
```

**两处消费**：
1. `risk_shield` 第 9 关（涨跌停封板校验）—— `trading_service` 在调挡板前预取
2. `trading_service.get_positions` 富化：
   - `market_value = last_price * qty`
   - `pnl = (last_price - open_cost) * qty`（`open_cost` 取自 `query_stock_positions` 返回的 XtPosition 字段）

**Why `get_full_tick` 而非订阅**：联调期只需「下单前快照」与「持仓估值」，按需拉取足够；订阅推流留待策略引擎实盘期。

---

## 9. 异常与重试矩阵

| 场景 | 网关层行为 | 服务/路由层映射 |
|---|---|---|
| `connect() != 0` | 抛 `ConnectionError`，`_lock_down=True` | `/connect` → 503 + "请确认 MiniQMT 已启动登录" |
| `subscribe() != 0` | warning，退化为主推查询 | `/connect` 200 + warning 字段 |
| `order_stock_async` 返回 -1 | `OrderResult(REJECTED)` | `/submit_order` → 409 + "QMT 拒单" |
| `order_stock_async` 抛异常 | `OrderResult(FAILED)` 不冒泡 | `/submit_order` → 500 + 异常消息 |
| cancel 时 seq→real 映射缺失 | `OrderResult(FAILED)` + 引导文案 | `/cancel_order` → 409 + "真实 order_id 尚未回报，短暂延迟后重试" |
| 断线（`on_disconnected`） | 原子置 `_lock_down=True` + 告警 | 后续 submit → 409（挡板第 1 关） |
| xtdata 不可用 | `get_quote` 返 None | 挡板跳过第 9 关，positions 的 market_value/pnl 为 None |

**幂等约定**：`emergency_halt` 已幂等（lock_down 重复置位不重复处理）；`cancel_order` 对已成交单返回当前终态而非抛错（网关契约）。

---

## 10. 测试策略

### 10.1 `tests/test_qmt_gateway.py`（新增）

用 `unittest.mock` 在 `sys.modules` 注入假 `xtquant.xttrader` / `xtquant.xtconstant` / `xtquant.xttype`，覆盖：

- `_map_qmt_status`：7 个枚举值 + 255 未知 → 正确 OrderState
- `_assert_status_contract`：注入匹配/漂移枚举 → 通过/抛错
- `connect` 时序：start/connect/subscribe 调用顺序；connect!=0 抛 ConnectionError
- `submit_order`：返回 seq-str + SUBMITTED；seq=-1 拒单；异常 FAILED
- `cancel_order`：seq→real 映射命中/缺失；rc=0/非0
- 断线锁定：`on_disconnected` 后 submit 拒单
- 回调投递：`on_stock_order` 经 `call_soon_threadsafe` 到主线程 `_process_order_update`

### 10.2 `tests/test_risk_shield.py`（新增）

纯函数穷举单测，每关至少 1 正 1 负用例：
- dry_run 拦截、allow_live 双保险、confirm 缺失、白名单外、非整手、金额超限、股数超限、涨停封板、跌停封板、quote=None 放行、断线拒单

### 10.3 联调脚本（手跑，不进 CI）

`scripts/qmt_smoke.py` 分 5 步、每步 `input()` 等待人工确认：

1. `connect()` → 期望 `_connected=True, _lock_down=False`
2. `query_asset()` → 期望返回 XtAsset（现金/总资产）
3. `query_positions` → 期望 list（空也 OK）
4. dry_run 下单（白名单内标的）→ 期望 409 + CSV 记 DRY_RUN_REJECT
5. 关 dry_run 后 100 股最小限价单 → 真下单 → 查 orders → 撤单

**铁律**：脚本绝不批量自动跑；每步打印结构化结果 + 回车确认。

---

## 11. 验收标准

- [ ] `pytest tests/test_qmt_gateway.py tests/test_risk_shield.py tests/test_trading_service.py -v` 全绿（CI 无 xtquant 也能跑）
- [ ] 既有 444 测试无回归（不破坏现有契约）
- [ ] `.env` 配置后 `GET /api/v1/trading/status` 返回 `disconnected`（不再是 unavailable）
- [ ] MiniQMT 启动登录后 `POST /api/v1/trading/connect` 成功，status 变 `live`
- [ ] `scripts/qmt_smoke.py` 5 步全通（含 1 笔真实最小单 + 撤单回报对账）
- [ ] dry_run 下单被挡板拦截并落审计 CSV
- [ ] 断线（手动kill MiniQMT）后 `/status` 变 `vetoed_by_risk`，submit 返 409

---

## 12. 风险与回退

| 风险 | 缓解 |
|---|---|
| MiniQMT 未启动 → connect 失败 | 联调脚本第 1 步显式校验 + 友好提示 |
| xtdata 行情接口签名偏差 | 延迟容错 + None 兜底；首次联调在 smoke.py 验证 |
| 真实账号误下单 | dry_run + allow_live 双开关 + 白名单 + 上限 + confirm 五重防线 |
| seq→real 回调未到即撤单 | 网关返 FAILED 引导重试（已实现） |
| 回归破坏现有交易测试 | 全量 pytest 在验收前必跑 |

**回退路径**：任何一步出问题，`QMT_USERDATA_PATH` 置空即让 `get_qmt_gateway()` 返 None，全系统退回 unavailable 模式，不影响回测/组合/宏观链路。

---

## 13. 实施顺序（供 writing-plans 展开）

1. 配置层（`.env` / `.env.example` / `trading/__init__.py` 导出）
2. 纯函数层（`risk_shield.py` + 单测）—— 零依赖，最先落地
3. 行情层（`qmt_market_data.py`）
4. 网关单测（`test_qmt_gateway.py`）
5. 服务层（`trading_service` 扩展）
6. 路由层（`trading.py` 扩展）
7. 联调脚本（`qmt_smoke.py`）
8. 全量回归 + 真实联调（需 MiniQMT 启动）

---

## 14. 后续 Phase 概要（同 epic，Phase 1 验收后各自起 spec）

### Phase 2：前端 Cockpit 连接/下单 UI

`LiveCockpitView.vue` 已有四态心跳灯 + Treemap 持仓 + 一键熔断 + CSV 导出，`api/trading.ts` 已有 4 个接口。本期增量：

- **连接控制**：「连接/断开」按钮 → `POST /connect`、`POST /disconnect`；状态灯已就位仅需绑定
- **下单面板**（核心）：symbol / qty / side / price / **dry_run 开关**（前端控制模拟 vs 实盘的总入口）/ confirm 二次确认 → `POST /submit_order`
- **撤单 + 订单列表**：`POST /cancel_order` + `GET /orders`，实时展示订单状态机流转（SUBMITTED→PARTIAL_FILLED→FILLED/CANCELLED）
- **持仓富化**：`PositionsTable` 接 `market_value/pnl`（Phase 1 行情接入后非 null）
- `api/trading.ts` 增 `connect/disconnect/submitOrder/cancelOrder/getOrders/getAsset` 六方法 + 对应 TS 类型

### Phase 3：策略引擎自动实盘

策略契约为 `BaseStrategy.generate_target_weights() -> List[TargetWeightSignal]`（**目标权重**，非订单），当前仅回测消费。本期新建 `executor/` 模块：

- **rebalance 算法**：`target_weights` vs 当前真实持仓（`gw.query_stock_positions`）→ 算 delta → 拆买/卖 `OrderRequest` 列表（考虑 A 股 100 整手、可用资金、T+1 冻结）
- **调度器**：日频（收盘前 N 分钟触发）/ 事件驱动；复用 `server/celery_app.py` 或 APScheduler
- **执行循环**：拉数据（`price_data` + `macro`）→ `fit` → `generate_target_weights` → rebalance → 逐单经 `risk_shield` → `gw.submit_order` → 流水/告警
- **风控前置**：策略层叠加 `MacroAwareGateway`（regime=-1 否决/减半，已实现）
- **与 Phase 1 边界**：复用 `QmtExecutionGateway` / `risk_shield` / `record_live_trade`，不重写交易链路；executor 只负责"信号→订单"转换与调度
