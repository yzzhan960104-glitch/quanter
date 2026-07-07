# EMT 极速交易接入设计（Phase 1.5：替代 MiniQMT）

> 日期：2026-07-08
> 背景：MiniQMT（迅投 xtquant）因监管停用；改用东方财富证券 **EMT 极速交易 API**（`emt_api_python` v2.27.0）
> 范围：新增 `EmtExecutionGateway` 子类，**复用 Phase 1 全部设施**

---

## 1. 复用 vs 新增（架构红利兑现）

当初设计 `BaseExecutionGateway` 抽象基类正是为这一刻——切换券商 = 新增一个子类：

**100% 复用**（零改动）：`BaseExecutionGateway` / `risk_shield`（10 关挡板）/ `trading_service`（编排+流水）/ 6 个 REST 路由 / `live_trades.csv` 五场景流水 / `OrderStateMachine` / conftest 假注入测试基建 / Phase 2-3 计划。

**新增**：`trading/emt_gateway.py`（`EmtExecutionGateway(BaseExecutionGateway)`），结构照搬 `qmt_gateway.py`（线程边界 `run_in_executor`+`call_soon_threadsafe`，EMT 同款"同步调用+C++回调线程"）。

---

## 2. EMT API 事实（从 SDK 真实代码 + 开发手册确认，无幻觉）

主流程（来源 `emt_api_python/test/tradertest.py`）：
```python
api = TestApi()                              # 继承 TraderApi（实现回调）
api.createTraderApi(client_id, save_path, log_level)
api.subscribePublicTopic(2)                  # EMT_TERT_QUICK
api.setSoftwareVersion("quanter")
session = api.login(ip, port, user, password, sock_type=1, local_ip)  # 0=失败
order_emt_id = api.insertOrder(order_dict, session)                    # 0=失败
api.cancelOrder(order_emt_id, session)
api.queryAsset(session, reqid) / api.queryPosition(session, reqid)
```

回调（CTP 风格，**需快速返回否则堵塞断线**）：
- `onOrderEvent(data, error, session)`：报单状态变化
- `onTradeEvent(data, session)`：成交回报
- `onCancelOrderError(data, error, session)`：撤单失败
- `onQueryAsset / onQueryPosition(data, error, reqid, last, session)`：查询响应
- `onDisconnected(reason)`：断线（**不自动重连**）

---

## 3. 枚举映射（来源开发手册 §5.1）

### 3.1 order_status → OrderState（复用 OrderStateMachine）

| EMT 枚举 | 值 | → OrderState |
|---|---|---|
| INIT / NOTRADEQUEUEING / UNKNOWN | 0 / 4 / 11 | SUBMITTED |
| ALLTRADED | 1 | FILLED |
| PARTTRADEDQUEUEING | 2 | PARTIAL_FILLED |
| PARTTRADEDNOTQUEUEING | 3 | PARTIAL_CANCELLED |
| CANCELED | 5 | CANCELLED |
| REJECTED | 6 | REJECTED |

### 3.2 下单必填字段（order_dict）

| 字段 | 取值 | 说明 |
|---|---|---|
| `market` | 沪A=2 / 深A=1 / 北A=5 | EMT_MARKET_TYPE |
| `side` | 买=1 / 卖=2 | EMT_SIDE_TYPE |
| `price_type` | 限价=1 | EMT_PRICE_TYPE（市价 2/3/4） |
| `business_type` | 0 | 普通股票业务 |
| `position_effect` | 1 | 开仓 |
| `ticker` | '600000'（纯数字，无后缀） | |
| `price` / `quantity` | float / int | |

### 3.3 标的编码转换（内部 ↔ EMT）

- `'600000.SH'` → `ticker='600000'` + `market=2`（沪A）
- `'000001.SZ'` → `ticker='000001'` + `market=1`（深A）
- `'830xxx.BJ'` → `ticker='830xxx'` + `market=5`（北A）
- 转换函数 `_split_symbol(symbol) -> (ticker, market)`，后缀决定 market

---

## 4. 配置（`.env` 已就位，仿真账号有效期 2027-07-07）

```ini
EMT_IP=61.152.230.41
EMT_PORT=19088
EMT_USER=510100014396           # API 用户名（非资金账号）
EMT_PASSWORD=Kg3625             # 仅本地 .env，不进 commit
EMT_CLIENT_ID=3                 # 自定义整数，多客户端须不同
EMT_SOCK_TYPE=1                 # 1=TCP（EMT 仅支持 TCP）
EMT_LOCAL_IP=127.0.0.1
EMT_QUOTE_IP=61.152.230.216     # EMQ L1 行情（后续行情接入用）
EMT_QUOTE_PORT=8093
```

**安全红线**：EMT 是 CTP 风格，密码是 API 登录凭证（不像 MiniQMT 由客户端消化）——必须进 `.env`（已 gitignore）。

---

## 5. Python 3.10 约束（关键环境事实）

`vnemttrader.pyd` 绑定 `python310.dll`（SDK v2.27.0 官方只提供 3.10 版，无 3.12）。项目用 **`.venv310`（Python 3.10.11）** 跑，与系统 3.12 隔离。验证：vnemttrader 在 3.10 加载成功（153 方法），既有 492 测试在 3.10 venv 全绿。

**开发/运行纪律**：EMT 相关命令一律用 `.venv310/Scripts/python`（不用系统 `python`）。

---

## 6. vnemttrader 加载（`emt_gateway.py` 顶部）

```python
import os, sys
_EMT_LIB = os.path.join(<project_root>, "emt_api_python", "lib", "windows")
if _EMT_LIB not in sys.path:
    sys.path.insert(0, _EMT_LIB)
if os.path.isdir(_EMT_LIB):
    try:
        os.add_dll_directory(_EMT_LIB)   # Python 3.8+ Windows DLL 查找
    except (OSError, AttributeError):
        pass
try:
    from vnemttrader import TraderApi
    _EMT_AVAILABLE = True
except ImportError:
    TraderApi = None
    _EMT_AVAILABLE = False
```

延迟容错：无 vnemttrader 环境（CI/非 Windows）退化为 object 基类，模块仍可 import。

---

## 7. 测试策略

`tests/conftest.py` 扩展（与既有假 xtquant 同款手法）：注入假 `vnemttrader.TraderApi`（FakeTraderApi 记录调用 + 可配置 login_rc/insertOrder 返回值）。

`tests/test_emt_gateway.py`（仿 `test_qmt_gateway.py`）：
- `_map_emt_status` 6 枚举 → OrderState
- `_split_symbol` 沪/深/北后缀解析
- login 成功/失败（session=0 抛 ConnectionError）
- insertOrder 返 order_emt_id（0=REJECTED）
- cancelOrder
- onDisconnected → 断线锁定
- 回调经 call_soon_threadsafe 投递主线程

CI 友好（假注入，无需真实 EMT 柜台）。

---

## 8. service 装配（`trading_service`）

`get_gateway()` 按 env 优先级选网关：
1. `EMT_USER` + `EMT_PASSWORD` 齐全 → `EmtExecutionGateway`
2. 否则 `QMT_USERDATA_PATH` + `QMT_ACCOUNT_ID` 齐全 → `QmtExecutionGateway`
3. 都无 → None（`/status` 返 unavailable）

`/status` 四态、`/positions`、`/asset`、`/submit_order`、`/cancel_order` 全部复用，字段适配 EMT 回调 dict（`onQueryAsset` 的 `total_asset/buying_power` ↔ 既有 `get_asset` 返回；`onQueryPosition` 的 `total_qty/sellable_qty/avg_price` ↔ `_fetch_broker_positions`）。

---

## 9. 联调（`scripts/emt_smoke.py`）

5 步人工确认（仿 `qmt_smoke.py`，去 emoji 防 GBK 崩）：
1. `login`（期望 session≠0）
2. `queryAsset`（期望返资产 dict）
3. `queryPosition`（期望返持仓 list）
4. dry_run 演示（不真下单）
5. 真最小限价单 100 股（需 YES 确认）→ 查 order → 撤单

用 `.venv310/Scripts/python scripts/emt_smoke.py` 跑。

---

## 10. 验收

- [ ] `test_emt_gateway.py` + 既有 492 测试在 `.venv310` 全绿
- [ ] `/status` 配置 EMT 后返 `disconnected`（不再是 unavailable）
- [ ] `login` 成功后 status 变 `live`
- [ ] dry_run 下单落 CSV
- [ ] 真实 100 股最小单 + 撤单回报对账（仿真账号 510100014396）
