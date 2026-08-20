# 富途量化接入可行性调研与接入方案（2026-08-20）

> 调研输入：`E:\quanter\量化使用手册.pdf`（369 页，富途牛牛客户端内置量化平台手册）+ 富途 OpenAPI 官方文档 v10.10 + 2026-05 八部门跨境监管新规公开报道 + 本仓库架构现状（QMT 单券商）。本文给出裁决、能力对照、对接设计与分波次实施计划。

## 一、TL;DR 裁决

| 维度 | 裁决 | 一句话理由 |
|---|---|---|
| 技术可行性 | ✅ 成立 | OpenAPI（OpenD + futu-api）成熟稳定；本仓库 `broker/base.py` 七方法抽象就位，接入缝清晰 |
| **合规可行性（实盘）** | ⚠️ **P0 前提未决** | 2026-05-22 八部门新规：**仅持内地身份（无境外身份）投资者两年整治期内禁止买入与入金**，仅可卖出/出金。实盘路径存亡取决于账户资格（见 §三） |
| 模拟盘/数据可行性 | ✅ 无门槛 | OpenD 登录**无需开户**（牛牛号即可），模拟账户 + 行情/历史 K 线全部可用，研究用途零合规风险 |
| 推荐路径 | **OpenAPI 接入自研系统** | 牛牛内置量化平台（手册所述）是封闭生态（仅标准库、50 标的上限、无法联动本仓库数据湖/风控），只配做参考与备用监控 |
| 一期范围建议 | **数据接入先行** | 港美股日频落湖 + 快照行情；交易网关与引擎泛化放二期，主引擎 A 股四 job 不动 |

## 二、调研对象定性：这本手册是什么

《量化使用手册.pdf》是**富途牛牛客户端内置「量化」功能**（可视化积木策略 + Python 代码策略）的接口手册，**不是** OpenAPI 文档。核心内容：

- **指标库 100+**：趋势（MA/EMA/SAR/MACD/ATR…）/超买超卖（KDJ/RSI/CCI…）/量价（OBV/VWAP…）/压力支撑（BOLL/ENE/MIKE…），每个指标带 `is_*_bullish_alignment` 等形态判断变体；支持麦语言与 Python 双通道自定义指标注册。
- **策略框架（代码策略）**：`StrategyBase` 约定函数——`initialize()`（仅启动时一次）内调 `trigger_symbols()`（**每策略最多 50 个运行标的**）/`custom_indicator()`（指标须先注册后使用）/`global_variables()`；主逻辑写 `handle_data()`，响应四类触发：每 K 线 / 每 tick / 每 N 秒 / 定时。
- **交易执行积木**：限价/市价/止损限价/止损市价/触及限价（止盈）/触及市价/跟踪止损限价/跟踪止损市价/改单/撤单/全部清仓/期货反手/期货移仓；下单限频 **15 笔/30 秒**。
- **约束（决定它只能当参考）**：代码策略仅支持 **Python 标准库、禁第三方包**，底层禁硬盘读写/网络请求/界面创建——与本仓库 pandas/optuna/hmmlearn 全家桶完全不兼容；策略在富途云沙箱运行，无法读写本仓库 data_lake / trading_state.db。
- **回测 + 实盘**双模式（`device_time()` 在回测中返回历史当前时间）；消息推送（与 App 通知打通，可当远程告警通道用）。

**结论**：手册平台 = 封闭策略沙箱。对已建成本仓库工业级系统的我们，其价值是（a）订单类型/字段语义的官方对照表，（b）手机端策略监控/推送的备用通道。接入主路径必须是 OpenAPI。

## 三、合规红线（P0，先于一切技术问题）

2026-05-22 证监会等八部门《综合整治非法跨境证券期货经营活动实施方案》（约 2026-05 → 2028-05 两年集中整治）：

1. **仅持内地身份证/护照、无任何境外身份**的投资者：**禁止境内入金、禁止买入**；允许卖出持仓、转出资金；账户不被强制清退。→ 此类账户**无法实盘量化买入**，对量化系统等于不可用（只卖不买无策略意义）。
2. **富途已全面停止大陆身份新开户**（李华 2026Q1 业绩会确认；现存路径仅「实际在境外工作/生活」者凭境外工作/生活证明开户）。
3. **持有任何境外身份**（香港身份证、海外护照等）投资者**不受影响**——这是唯一可持续的实盘资格。
4. **模拟盘不受限**：OpenD 登录无需开户（牛牛号/注册手机号即可，首登问卷+协议），模拟交易用虚拟资金，数据研究用途完全合规。
5. 行情权限按 **OpenD 登录 IP** 判定境内/国际客户——境内 IP（境内认证客户）反而是行情权益最好的一档（港股 LV2 免费，见 §四）。

**行动含义**：开工前必须先回答「账户资格」（§十-待决问题 1）。若答案是「仅内地身份」→ 本方案自动降级为「数据接入 + 模拟盘研究」，W5 实盘灰度波次作废；若「有境外身份/存量合规账户可买入」→ 全量路径可行。

## 四、富途 OpenAPI 能力盘点（对接设计的事实基础）

### 4.1 架构与部署

- **OpenD 网关**：本机/云端部署的 TCP 网关程序（可视化 GUI 版 / 命令行 CLI 版），SDK 经自定义 TCP 协议（默认端口 11111，`FutuOpenD.xml` 可配）连 OpenD，OpenD 连富途服务器。CLI 版适合服务器化（配置文件 + 参数：`rsa_key`、`pdt_protection`、`auto_hold_quote_right` 等）。
- **SDK**：`pip install futu-api`（官方开源 FutunnOpen/py-futu-api，另有 C++/Java/JS/C#）。关键对象（以官方文档为准）：`OpenQuoteContext`（行情/订阅/历史 K 线）、`OpenSecTradeContext`（交易：`place_order`/`modify_order`/`cancel_order`/`accinfo`/`position_list`/`order_list`/`today_trades`，`TrdEnv.REAL|SIMULATE`）、订单推送 `OrderHandlerBase.on_order_update_trd/on_order_fill_trd`（对应本仓库 QMT 回调注入模式）。
- **交易解锁**：实盘下单前须 `unlock_trade`（交易密码明文或 MD5；CLI API 不兼容「富途令牌」二步验证，需关闭令牌功能；GUI 版 OpenD 可免此步）。⚠️ 密码经 `.env`/凭据文件注入，**严禁落库/落 git**（仓库 credentials 纪律同 QMT）。
- **版本匹配**：`futu-api` SDK 与 OpenD 版本必须配套（协议版本协商），升级需同步升、否则连接失败——纳入依赖纪律（同 xtquant 本地包管理经验）。

### 4.2 市场与账户

| 项 | 事实 |
|---|---|
| 市场 | 港股 / 美股 / A股通（沪深股通北向，仅实盘不支持模拟）/ 日、马暂缓；期货期权（港/美） |
| 综合账户 | 单账户多币种多市场（HKD/USD/CNY…）——与 QMT 单币种 CNY 不同，见 §六-缝隙7 |
| 模拟账户 | 港股模拟 / 美股模拟 / 美股融资融券模拟 / 港期货模拟（`acc_list` 按 `filter_trdmarket` 取）；⚠️ 旧美股模拟账户与其他客户端不互通（官方 Issue #227） |
| 单笔上限（FUTU HK） | A股通 ≤100 万股且 ≤500 万 CNY；美股 ≤50 万股且 ≤500 万 USD；港期/期权 ≤3000 手 |

### 4.3 行情权限与额度（对接设计的关键预算约束）

行情权限（按 OpenD 登录 IP 定级，境内认证客户档）：

| 市场 | 境内认证客户（我们的档） | 国际客户 |
|---|---|---|
| 港股证券/窝轮牛熊 | **LV2 免费** | LV1 免费，LV2 付费 |
| 港股期权 | LV2 推广期免费 | LV1 免费 |
| 美股股票/ETF | **LV3 免费**（推广期，Nasdaq Basic+TotalView+Arcabook） | 同左（LV3 推广期） |
| A 股 | **LV1 免费** | 不支持 |
| 美期 CME 系 | 需开户+付费 LV2 | 同左 |

额度分层（订阅额度 / 历史 K 线额度，每 7 天滚动窗口）：

| 条件（满足其一） | 订阅额度 | 历史 K 线额度 |
|---|---|---|
| 总资产 <1 万 HKD（含未开户） | 100 | 100 |
| 总资产 ≥1 万 HKD | 300 | 300 |
| ≥50 万 HKD 或 月交易 >200 笔 或 >200 万 HKD | 1000 | 1000 |
| ≥500 万 HKD 或 >2000 笔 或 >2000 万 HKD | 2000 | 2000 |

额度语义要点（直接决定数据同步设计）：

- **历史 K 线按「标的」计**：同一标的 7 天窗口内重复请求、拉不同周期，都只占 1 个额度 → **增量日更天然免费**（昨日拉过的标的今天重拉不耗额度，窗口滚动 7 天）。
- 订阅额度按「每股每订阅类型」计 1 个，**须所有连接都退订才释放**（多连接坑）；期权链独立额度池。
- 接口限频：快照/历史 K 线约 60 次/30 秒（逐接口页「接口限制」为准）；下单 15 笔/30 秒（手册口径）。
- 数据深度：分 K（≤60min）最近 8 年；日 K 最近 20 年——日频研究足够，超 8 年的分 K 需混源（tushare 不覆盖港美，无替代，接受即可）。

## 五、能力对照：QMT（现状）vs 富途 OpenAPI

| 维度 | QMT（miniQMT/xtquant） | 富途 OpenAPI（futu-api + OpenD） |
|---|---|---|
| 市场 | 仅 A 股 | 港/美/A股通/期货期权 |
| 网关进程 | QMT 客户端内嵌 miniQMT（userdata 路径 + session 单进程独占） | 独立 OpenD（CLI 服务化，无 GUI 依赖） |
| 状态推送 | C++ 回调线程（on_stock_order/on_trade…） | OrderHandlerBase 推送回调（on_order_update_trd…） |
| 实盘解锁 | 客户端登录态 | `unlock_trade`（密码 MD5；与富途令牌互斥） |
| 行情 | xtdata 全推（无额度概念） | 订阅额度 + 快照接口限频（预算化） |
| 历史 K 线 | xtdata 本地下载（无额度） | 7 天窗口按标的配额 + 分页 `page_ptr` |
| 资金/币种 | CNY 单币 | 多币种综合账户（需折算决策） |
| 模拟 | 无（dry_run 影子模式自实现） | 官方模拟账户（TrdEnv.SIMULATE，真实市场环境） |
| 稳定性坑 | 客户端被杀/抢 session（本仓库 08-18 两次事故） | OpenD 单点/版本协议匹配/额度释放 |

**结论**：futu 的模拟账户与独立网关进程反而**优于** QMT 的两条历史痛点（无真模拟、客户端耦合）；行情额度是新增的预算型约束。

## 六、quanter 对接缝隙分析与设计

现有缝隙逐一过（行号为 2026-08-20 master 现状）：

### 缝隙 1：broker 抽象 —— 已就位，纯新增子类 ✅

`broker/base.py:61` `BaseExecutionGateway` 七方法（connect/disconnect/submit_order/cancel_order/_fetch_broker_positions/query_asset/get_quote）+ `sync_positions` 模板方法（对账骨架复用 `trading.compute.reconcile`）。`OrderResult`（`broker/base.py:46`）复用 OrderState 状态机。

**设计**：镜像 QMT 四文件分层新增 `broker/futu_connection.py`（契约根：futu-api 容错导入 try/except（同 `qmt_connection.py:74` 模式）、订单状态映射 `_map_futu_order_status → OrderState`、超时/退避常量、unlock_trade 生命周期）/ `broker/futu_io.py`（position_list/accinfo/order_list 快照查询）/ `broker/futu_business.py`（place_order/modify/cancel + 推送回报→`OrderUpdateCallback` 注入主线程，同 QMT 线程模型）/ `broker/futu.py`（组装垫片 + 显式列名 re-export，同 `qmt.py` 范式）。状态映射纪律照抄 `_map_qmt_status` 的保守归并（未知态归 SUBMITTED，绝不冒进 FILLED/REJECTED）。

### 缝隙 2：gateway_service 单例 + 单账户 —— 需小改装配 ⚠️

`trading/gateway_service.py:81` `get_gateway` 为 QMT 唯一单例；`trading/account.py:18` 单 account_id；QMT session 单进程独占。

**设计**：新增 `BROKER_GATEWAY=qmt|futu|mock` env 选网关（首期**互斥运行**而非并发——一个引擎实例绑一个券商，避免动 state_store 单账户假设；`state_store` 的 account 表 `upsert_account` 已存在，futu 用独立 account_id）。并发双券商留二期（需 account 维度贯穿 order/fill/position 查询——schema 已有 account_id 列，改动可控但面广）。

### 缝隙 3：行情源直绑 xtdata —— get_quote 契约已上提，补实现 ⚠️

`broker/qmt_quote.py:201` 直绑 xtdata（无行情源抽象），但 `get_quote` 已是基类契约（返回 `last_price/high_limit/…` 标准化 dict）。

**设计**：`futu` 网关 `get_quote` 走 `get_market_snapshot`（免订阅、限频 60/30s——恰好覆盖 stop_loss 30 秒轮询节奏：一次全池快照而非逐标的）。**订阅制推送（tick/K 线推送）二期再上**，届时需订阅额度管理器（退订须本连接全退；断线重订阅）。注意快照无 A 股涨跌停字段语义：港美无涨跌停（美股有 LULD 动态熔断但不暴露 limit 字段）→ `high_limit/low_limit` 返 None，消费方（risk_shield 涨跌停闸）已是缺失降级语义（ADR-16 后该闸已撤，风险面更小）。

### 缝隙 4：引擎时钟/日历 A 股硬编码 —— 最大架构债，推二期 🔴

`config/market.py` `MARKET_HOURS` 仅 A 股；`trading/clock.py`/`trading/calendar.py` 绑 tushare trade_cal；TradingEngine 四 job（pre_open 09:22 / stop_loss 30s / post_close 15:30 / pipeline 18:00）全部 A 股节奏。美股常规时段 = 北京夜间（且夏令时漂移）、港股有半日市——**主引擎四 job 无法直接复用**。

**设计**：一期 futu 网关**不进主引擎 job 循环**（作为独立执行通道：日内由人工/独立脚本驱动，或仅数据用途）；二期若要美股无人值守，需做 per-market 会话抽象（MarketSession：日历+时段+时区）并让 engine 泛化为多市场实例——这是一次独立的架构立项，不混入 futu 首期。

### 缝隙 5：数据层 tushare 单源 —— 新增 futu 数据集注册 ⚠️

`config/registry.py:206` `TUSHARE_DATASETS` 43 集声明式注册；`data/tushare_sync.py:244` `sync_dataset` 通用同步器（by_symbol/by_date）；`ops/sync_all_datasets.py` 挂 pipeline 尾 fire-and-forget。

**设计**：新增 `FUTU_DATASETS`（首批：`hk_daily`/`us_daily`/`hk_stock_basic`/`us_stock_basic`，字段对齐 lake 既有 OHLCV 口径）；`data/futu_sync.py` 镜像 `sync_dataset`（by_symbol + `request_history_kline` 分页 `page_ptr`；**7 天窗口去重语义天然适配增量日更**）；落 `data_lake` parquet——**LFS 治理红线：产物落库提交即违规**（ed3776b6 之后）。改 pipeline 测试须 patch `_spawn_all_datasets_sync`（dataset-sync-governance 裁决）。额度预算：一期港美各 ≤100 只（未开户/低资产档即够），标的池扩容前先查 `get_history_kl_quota` 剩余额度。

### 缝隙 6：ADR-16 人工风控两值 —— 按账户维度扩展 ⚠️

`trading/risk_ctrl.py`（block on/off + position N）+ `state_store.read/write_risk_control`（key：`block_new_orders`/`max_total_position`）当前是全局单值；`trading/compute/risk.py:50` `check_order` 四闸（connection/dry_run/master_switch/session）。

**设计**：两值 key 加 account 维度（`risk_ctrl` CLI 加 `--account`，缺省当前账户=向后兼容）。**严禁**因 futu 接入重新引入任何自动指标闸（ADR-16 裁决：风控面=双值，择时判断权归人工）。session 闸对港美时段的口径需按缝隙 4 的会话抽象走，一期 futu 通道不挂 session 闸则必须在 dry_run/人工确认下运行。

### 缝隙 7：币种/手数/交易日差异 —— 显式决策点 ⚠️

- `query_asset` 基类契约 4 字段（cash/total_asset/market_value）隐含单币假设；futu 综合账户多币。**设计**：一期 futu 锁单一市场子账户（如美股账户 USD 记账），多币折算二期（需汇率源，`TRD_CURRENCY` 决策记录进 ADR）。
- 港股每手 board lot（`lot_size` 接口可查）、最小价差 tick 与 A 股 100 股/0.01 不同；美股零股规则。**设计**：下单前 `lot_size`/`qty` 校验放 futu_business（柜台也会兜底拒单，但前置校验减少废单噪音）。
- 港美交易日历 ≠ A 股日历：`clock.trading_day` 不能直接用于 futu 通道（二期 MarketSession 一并解决；一期人工确认交易日）。

## 七、部署与运维

1. **OpenD CLI 部署**：独立目录 + `FutuOpenD.xml`（api_port 11111、登录账号、rsa_key 路径）；Windows 任务计划/服务化守护（勿与 QMT 客户端同目录）；**多会话纪律**：OpenD 端口独占，起停脚本纳入 `ops/`，避免 08-18 式并行会话互杀（凌晨劫客户端/傍晚杀引擎的教训——futu 通道上线后「杀进程清单」必须显式列 OpenD）。
2. **凭据**：交易密码 MD5 与 rsa 私钥走 `.env`/本地凭据文件（`.env.example` 只留键名）；首登问卷/协议人工完成一次后配置持久化。
3. **connect 超时守护**：引擎 lifespan 接 futu 网关时，connect 必须带超时+失败快错（2026-08-14 教训：无客户端 connect 挂死 lifespan；OpenD 不在时 `OpenQuoteContext` 构造会阻塞）。
4. **监控**：OpenD 进程存活探测（同 `_client_process_alive` 模式）、断线重连退避（`_RECONNECT_BACKOFFS` 同款）、额度水位（`get_history_kl_quota`/`query_subscription` 每日 post_close 上报简报）。
5. **升级纪律**：futu-api 与 OpenD 版本配对锁定（requirements 固定版本 + 升级跑契约冒烟），防协议不匹配静默失败。

## 八、测试策略（对齐 2007 基线纪律）

- **FakeOpenD 假件**：`tests/conftest.py` 注入假 `futu` 模块（镜像 `_install_fake_xtquant`：枚举值与 `_FUTU_*` 字面量契约一致），无 futu-api 也能全量跑单测。
- **契约测试与 QMT 同构**：状态映射表驱动（futu 订单状态枚举 × OrderState 期望）、unlock/断线熔断/回报推送线程模型，镜像 `test_qmt_gateway.py` 替身模式（W2 已收口的替身单源纪律）。
- **模拟盘冒烟**：`trading/tools/futu_live_smoke.py`（镜像 qmt_live_smoke）：快照→历史 K 线→模拟下单→撤单→持仓/资金查询，一键 go/no-go。
- 零覆盖损失红线：W1-W4 每波全量后端单测通过（当前基线 2007 项/~3.5min）。

## 九、实施波次（每波独立可验收，随时可停在合规闸前）

| 波次 | 内容 | 验收 | 依赖 |
|---|---|---|---|
| **W0 冒烟（0.5-1 天）** | 装 OpenD CLI + futu-api；模拟账户；冒烟脚本四连（快照/K 线/模拟单/持仓）；验证境内 IP 行情定级（港股 LV2 免费） | `docs/research/` 冒烟报告 + **合规资格答案**（§十-1）→ 决定 W4/W5 存废 | 牛牛账号（无需开户） |
| **W1 数据接入（2-3 天）** | FUTU_DATASETS 注册 + futu_sync + 落湖 + pipeline 挂尾；额度水位监控 | hk_daily/us_daily 增量日更绿；parquet 不入库；pipeline 测试 patch 纪律 | W0 |
| **W2 交易网关（3-5 天）** | broker/futu* 四文件 + 状态映射 + unlock + FakeOpenD 单测 | 契约测试绿；模拟盘 place/cancel/回报闭环 | W0 |
| **W3 装配+风控（2-3 天）** | BROKER_GATEWAY 选装 + account 维度两值 + 告警接线 | dry_run 全链路（信号→futu 模拟单→fill 落库→简报） | W1+W2 |
| **W4 模拟盘长跑（≥2 周）** | 每日模拟单 + 对账 + 额度/断线观测 | 连续 10 交易日零人工干预事故 | W3 |
| **W5 实盘灰度（仅当合规过关）** | 最小资金、单标的、人工开关常开 | 真实成交/对账/简报闭环；两值风控演练 | **W4 + 境外身份资格** |

## 十、待决问题（按 AGENTS.md 最多三问，答案直接改裁方案形状）

1. **账户资格**：是否持有境外身份（香港身份证/海外护照）或「实际在境外工作/生活」可开户？存量富途账户当前买入权限状态？——**P0，决定 W5 与实盘路径存废**。
2. **目标市场优先级**：港股 / 美股 / A股通（仅实盘无模拟）？——决定时钟复杂度（美股=北京夜间+夏令时）、币种记账、额度预算分配。
3. **一期用途**：纯数据研究 / 模拟执行验证 / 直接奔实盘？——决定波次裁剪（纯研究可停在 W1；奔实盘才需要 W3-W5 全链）。

## 十一、风险清单

| 级 | 风险 | 缓解 |
|---|---|---|
| **P0** | 合规：仅内地身份禁买入/入金（2026-05 新规，两年整治期） | §三资格确认前置；模拟盘/数据路径不受限 |
| **P0** | OpenD 单点 + SDK/OpenD 版本协议不匹配 | 版本配对锁死；进程守护；connect 超时快错 |
| P1 | 订阅额度释放坑（须所有连接退订） | 单连接原则；额度水位日报 |
| P1 | 历史 K 线 7 天窗配额耗尽 | by_symbol 增量日更（窗口去重免费）；`get_history_kl_quota` 前置检查 |
| P1 | 港美日历/半日市/夏令时与 A 股时钟冲突 | 一期不进主引擎 job；二期 MarketSession 立项 |
| P2 | 多币种折算（HKD/USD/CNY） | 一期单市场子账户；二期汇率源+ADR |
| P2 | 旧美股模拟账户体系不互通（官方 Issue #227） | W0 用新版模拟账户验证 |
| P2 | 港股手数/碎股/最小价差废单 | lot_size 前置校验 + 柜台兜底 |

## 十二、参考

- 富途 OpenAPI 文档（v10.10）：[介绍](https://openapi.futunn.com/futu-api-doc/) / [权限与额度](https://openapi.futunn.com/futu-api-doc/intro/authority.html) / [获取历史K线](https://openapi.futunn.com/futu-api-doc/quote/request-history-kline.html) / [历史K线额度明细](https://openapi.futunn.com/futu-api-doc/quote/get-history-kl-quota.html) / [解锁交易](https://openapi.futunn.com/futu-api-doc/trade/unlock.html) / [命令行 OpenD](https://openapi.futunn.com/futu-api-doc/opend/opend-cmd.html) / [交易相关 Q&A](https://openapi.futunn.com/futu-api-doc/qa/trade.html) / [行情相关 Q&A](https://openapi.futunn.com/futu-api-doc/qa/quote.html)
- Python SDK：[FutunnOpen/py-futu-api](https://github.com/FutunnOpen/py-futu-api)
- 监管：[21 财经·跨境证券经纪格局（2026-05-31）](https://www.21jingji.com/article/20260531/herald/0e7444b71b94bb7c4d75a5c88754e003.html)（八部门实施方案、两年整治期、富途停大陆新开户）
- 本仓库：`docs/superpowers/plans/2026-07-07-qmt-broker-access-phase1.md`（QMT 一期接入范式）、`docs/architecture/16-manual-risk-control-replaces-regime.md`（ADR-16 两值风控）

## 十三、诚实边界

- 监管细则（境外身份认定、过渡期起算日精确到日）以官方文件与富途客服答复为准，本文依据 2026-05-31 报道归纳，**未逐字核对原文**；额度/限频数字取自 v10.10 文档快照，富途声明保留调整权。
- futu-api 具体函数签名（page_ptr/TrdEnv/OrderHandlerBase 等）凭官方文档与 SDK 通识陈述，**未在本机实测**——W0 冒烟波次即是实证步骤。
- 未调研 moomoo（海外版）与富途 API 的差异细节（同集团同 SDK，行情权限档不同）；若目标市场以美股为主可另行评估。
