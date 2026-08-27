# 东财掘金颈线策略单文件试点 · 设计方案（2026-08-21 · v1）

> **状态**：设计稿 v1——由 [PTrade 版设计 v2](2026-08-20-ptrade-neckline-pilot-design.md)（已搁置）改造；**D1 于 2026-08-21 撤回：纯用东财掘金**，接受本地终端形态。W0' 核对表跑通后才排实施计划。事实底座：[东财掘金接入调研](../../research/2026-08-21-emquant-integration-feasibility.md)（平台真身/运行模型/官方文档实证）。
> **事实审查红线**：gm API 名按官方策略指引书写（指引自述伪代码级），**全部以 §八 W0' 终端实测为准**，核实前不写实施计划。

## 0. 目标与非目标

**目标**：把颈线策略（已定稿参数集）的「识别 + 挂单 + 巡检出场」完整闭环装进**一个 gm SDK 策略文件**，跑在东财掘金终端（**仿真模式**起步），与本地 QMT 引擎双轨对照 ≥20 交易日，验证掘金腿与本地腿信号/挂单/巡检动作一致性——为「掘金作为简化执行载体 / 第二执行腿」提供依据。

**非目标**：
- **券商托管（D1 已撤回）**：掘金=本地终端形态（官方指引：「SDK 运行无法脱离终端」），关机可跑不成立；无人值守须本机常开或 VPS——与现状 QMT 腿同级，非倒退，但原托管动机清单（防误杀/session 独占/断电 SLA）不在本试点解决范围；
- discovery / autopromote / 参数搜索（optuna 等不进单文件，参数以**定稿快照**硬编码，见 §2 §0 段）；
- fill 真相源 / 对账 / 钉钉告警链 / job_ledger 台账（试点期 audit CSV + 人工复核补位）；
- 熔断/简报等盘后分析（本地腿继续做，掘金腿只产 audit 原始件）；
- **实盘资金**：仿真阶段零真金；上实盘前另行决策，且须先解决与 QMT 腿**同账户双网关**冲突（§9 遗留-1）。

### 0.5 决策记录

| # | 问题 | 结论 | 影响 |
|---|---|---|---|
| D1' | 托管诉求（原 D1「决策逻辑住券商侧」） | **2026-08-21 撤回——纯用东财掘金** | 接受本地终端形态；东财 PTrade 核实关闭（不再是前置）；PTrade 版设计搁置备查 |
| D2 | 真金还是仿真？ | **仿真**（掘金仿真柜台，每账号 ≤10 仿真策略） | 资金上限/亏损中止线/双敞口消解；仿真撮合口径 W0' 验证 |
| D3 | 无人择时 | 接受（仿真零真金 + ≤2 单/日硬闸兜底） | RISK_BLOCK.flag 是**本地文件**——人工随时可触达（PTrade 版的远程触达矛盾在本机场景消失；若日后 VPS 化则重现） |
| D4 | 双轨期冻结 | 参数 + UNIVERSE 双冻结 20 交易日 | 同 PTrade v2；一致率分母由此可定义（§7） |
| D6' | 载体 | 东财掘金终端 + gm SDK | 不涉及换券商 |

**定位声明**：本试点只裁「掘金腿的工程一致性」，**不构成颈线策略晋级/加仓依据**。08-13 策略审计裁定（可信度低、不建议实盘资金）与仿真试点不冲突；上实盘须另行决策并直面该审计前提（§9 遗留-2）。

## 1. 总体架构：引擎四 job ↔ gm SDK 事件映射

| 本地（现状） | gm SDK | 语义对齐说明 |
|---|---|---|
| eod_plan：T-1 晚扫描（`detect_signal(df_upto 截至 T-1)`）→ SIGNAL.meta → auto confirm | `schedule(date_rule='1d', time_rule='09:15')` 阶段① | 数据口径不变：`history(end_time=T-1)` 仍「截至 T-1 收盘」，扫描时点从 T-1 晚挪到 T 日盘前不引入当日数据；schedule 可指定时刻（官方示例 9:30），精度与最早可触发时刻 W0' 实测 |
| pre_open 09:22：撤昨日单 → 超期平仓 → ADR-16 双值闸 → 逐单挂限价买 | 同一 schedule 阶段②③④⑤ | 顺序照搬本地红线：先撤后挂；额度逐单扣减 fail-closed；`cl_ord_id` 为委托主键 |
| stop_loss 30s 巡检（止损/止盈/撤单/trailing） | `on_tick`（tick 3 秒推送）或 `on_bar('60s')` | **tick 3s 优于 PTrade 1m，接近本地 30s 语义**；订阅面 = 持仓 + 当日挂单标的（≤8 只，远低于免费版 50 标的上限） |
| post_close 15:30：对账/熔断/简报 | `schedule(time_rule='15:35')` | 只落 audit CSV；熔断与简报留本地腿 |
| pipeline 18:00 数据同步 / catchup / job_ledger | 无对应 | `history` 按需；补跑以状态文件幂等（§3） |

## 2. 单文件分区设计（预估 ~1300-1600 行）

```
emquant_neckline_pilot.py
├─ §0 参数区          ~80 行   ID_PARAMS / EXEC_PARAMS / UNIVERSE 定稿快照 + PARAMS_FINGERPRINT
│                              （EXEC_PARAMS 键集须含 buy_limit_atr_mult——method_v0.py:525 消费它，
│                              signal.py:117 键集清单漏列，以实码为准）
├─ §1 识别内核        ~790 行  method_v0.py 652 行**零编辑逐字块** + signal.py 133 行 Signal **全量照抄**
│                              （唯一允许的编辑 = `from .signal import Signal` 改内嵌）
├─ §2 数据层          ~80 行   gm history 封装 → df_upto（end_time=T-1、adjust=ADJUST_PREV +
│                              adjust_end_time 定点复权、列名对齐 open/high/low/close/volume、DatetimeIndex）
├─ §3 状态层          ~150 行  state.pkl 读写（**tmp+rename 原子落盘**）+ 崩溃恢复/幂等（§3）
│                              + RISK_BLOCK/CAP 读取（本地文件，人工直触）
├─ §4 风控闸          ~100 行  block flag / 总仓额度 fail-closed / 试点硬闸（§6）
├─ §5 订单工具        ~120 行  order_volume（限价买/卖）/ order_cancel / get_orders / cash /
│                              positions / 跌停价（签名 W0' 核）
├─ §6 事件处理        ~250 行  init（订阅巡检标的）/ run(mode=仿真, token, strategy_id) /
│                              schedule 五阶段盘前 / on_tick 巡检 / schedule 盘后落 audit
└─ §7 对拍审计         ~50 行  audit_YYYYMMDD.csv（信号/挂单/撤单/成交/巡检动作逐行）
```

**依赖自检**：§1 只用 pandas/numpy + 标准库 math；§0/§3-§7 只用标准库 + gm SDK。无任何本仓库 import。

**环境**：本地 Python（终端支持 3.6-3.12，建议直接复用 `.venv310`）——PTrade 版的 numpy 版本死活题消失；但 gm 对 **protobuf 版本敏感**（FAQ 提及需 3.6.0 时代的兼容处理），`.venv310` 与 gm 共存是 W0' 第 0 项。

**等价证据（同一测试基线）**：§1 逐字块可原样拷出为本地 .py 供 pytest import，直接跑既有 `test_neckline_recognition` / `test_detect_signal` 等用例——对拍从「另写对照单测」升为「同一测试基线」。

## 3. 状态模型与崩溃恢复

```
<本地工作目录>/neckline_pilot/
  state.pkl            # 见下 schema
  RISK_BLOCK.flag      # 存在 = block_new_orders（人工 touch/rm，唯一择时闸，ADR-16）
  CAP.txt              # max_total_position 数值（人工编辑，缺省 1.0）
  audit_YYYYMMDD.csv   # 每日对拍日志（本地腿同机直接读）
```

`state.pkl` schema（v1）：

```python
{
  "version": 1,
  "scan_done": {"2026-08-21"},          # 当日已扫描标记（幂等防重扫）
  "placed":  {"2026-08-21": [cl_ord_id, ...]},  # 当日已挂（幂等防重挂）
  "orders":   {cl_ord_id: {"symbol","date","price","qty","purpose","exec_params","status"}},
  "positions":{symbol: {"entry_date","entry_price","qty","remaining_qty",
                        "stop","tp1_price","tp1_done","tp2_price",
                        "trailing":{"activated","cur_stop"},"exec_params"}},
}
```

**写入纪律**：state.pkl 一律「写临时文件 + rename」原子落盘——崩溃落在写入中间 = pickle 损坏，两行代码消灭一类恢复场景。

**崩溃恢复语义（对齐本地 has_order/UNIQUE 防重挂的意图）**：
- 盘前 schedule 幂等三查：state 当日标记 + `get_orders` 柜台实况 + `positions` 柜台实况；**以柜台实况为准修 state**（绝不逆推——state 说挂了但柜台没有 → 视为未挂，重新走闸后可补挂；柜台有而 state 无 → 吸收进 state 继续管理，exec_params 从 §0 定稿快照回填）。
- 策略进程崩溃重启：只损失巡检时效；trailing 水位从 `cur_stop` 与当日行情重放推导。
- **策略进程生命周期是本地自己的事**（常驻 or 计划任务拉起，范式同现有引擎 supervisor）——没有 PTrade 的「事件是否补跑」未知数，但终端须在线（SDK 无法脱离终端）。
- 识别本身幂等（纯函数），重扫零风险。

## 4. 订单生命周期规则全集（从 exec_params 键集导出）

> **公式实证（08-20 复审逐条对照 master 实码）**：entry 公式与 `method_v0.py:525` 一致；stop/tp1/tp2/cancel_on 与 `price_levels.py` 单源一致。仓库 `signal.py:62-64` / `trading/compute/plan.py:170` 两处注释过时（仍写「scan_live entry=颈线」），**以实码为准**（debt 见 §9）。

| 阶段 | 规则 | 价位/口径（与本地单源同口径） |
|---|---|---|
| 挂单 | T 日开盘前限价买；**每日撤昨日单 → 窗口内重挂**（本地模型，非一单挂多日） | `entry = neckline + buy_limit_atr_mult × ATR` |
| 撤单-冲天 | 盘中最高价触及即撤 | `cancel_on = neckline + cancel_thresh_mult × H`，H = neckline − bottom |
| 撤单-超时 | 信号过期不再挂：**以 formed_at（突破日）起算**，`trading_days_between(formed_at, today) > max_wait` 即过期（对齐 `pre_open.py:574-575` 严格大于口径；**不得以挂单日起**——挂单日晚一日=晚关窗一日，边界日双腿分叉） | 交易日计数以 formed_at 起 |
| 止损 | 触价即卖剩余全量 | `stop = neckline − stop_atr_mult × ATR`（base） |
| trailing | 持仓 `trailing_grace` 日后启动，按 `trailing_step` 步进上移，下限 `trailing_floor` | 语义以本地 `trading/compute/stop.py` 实现为准逐句重写，W1 对照单测；**grace=0 时 step/floor 互锁归零（不激活）的耦合约束一并照搬**（discovery/constraints 同源语义） |
| 止盈1 | 触价卖 `tp1_portion` 比例（一次） | `tp1 = neckline + tp1_h_mult × H` |
| 止盈2 | 触价清仓 | `tp2 = neckline + tp_h_mult × H` |
| 超期平仓 | 持仓 `max_holding` 交易日 → 次日开盘前跌停价清仓 | 对齐本地「跌停价保证成交，接受滑点」（`pre_open.py:430-434`）；**holding 计数基准日 = T-1（pretrade_date 收盘口径），绝不用当日**——本地红线：用当日多算一日 → 误平窗口内持仓（致命，`pre_open.py:436-439`） |
| 部分成交 | 剩余腿继续等；撤单后已成交部分转持仓管理 | 与本地 OrderStateMachine 口径一致（状态词汇同源） |

## 5. 数据链路与复权对拍（最大正确性风险，W0'/W1 主验收）

1. 数据源只用 gm `history`（**定点前复权**：`adjust=ADJUST_PREV, adjust_end_time=` 显式指定基准日——比 PTrade 黑盒口径可控）；不用掘金其他数据通道，少一层口径。
2. **对拍方法**：本地从 data_lake 导出 UNIVERSE × 近 `2×window` 日 qfq 日线 CSV → 掘金侧**同基准日**定点取数逐列比对（|Δclose| 相对容差 1e-6）；不一致标的进排除池并记录原因（复权因子时点差）。注意 33000 条单次上限与分批。
3. **信号级对拍**：双轨每日信号集合（symbol / neckline / entry_price / rr）**要求 100% 一致**才准进 W2——颈线位差一分钱信号日期就分叉，没有「近似一致」。

## 6. 风控面（ADR-16 模拟 + 试点硬闸 · 仿真版）

**ADR-16 双值的模拟**：
- `block_new_orders` → `RISK_BLOCK.flag` 文件存在即全拦（存量管理照常：撤单/超期/trailing 不受影响，与 `pre_open.py:478` 同口径）；**唯一择时闸，无任何指标闸**——本地 ADR-16（2026-08-17）已退役全部 regime 指标闸（`engine.py:453` / `pipeline.py:244`），两侧语义一致。**本地文件=人工随时可触达**（D3 矛盾在本机场景消失）。
- `max_total_position` → `CAP.txt` 数值；挂单前 `总权益 × CAP − 持仓市值 − 已挂买单金额` 逐单扣减（本地口径含未终态买单，`state_store.py:1470` 同义；`pre_open.py:540` 同式）；**fail-closed**：资金/持仓查询失败 → 当日不挂。

**试点期附加硬闸**（验收后可放开，写死在 §0 参数区）：单日新挂 ≤2 单；单票市值 ≤5%（仿真账户语义内，作为实盘纪律预演）；audit CSV 每日人工复核（替代钉钉 CRITICAL 链）。

**治理降级清单（相对本地腿，试点期明示接受）**：拒单风暴自动拨 block_new_orders（`order_state.py:599-620`）不在掘金腿——由 ≤2 单/日硬闸兜底规模、audit 人工复核兜底发现；熔断基线 / CRITICAL 告警链 / 对账 drift 告警同理不在场。终端自带本地风控 GUI（持仓/标的限制/合规）可作第二道闸，配置项 W0' 评估。

## 7. 双轨对照与验收指标

| 指标 | 口径 | 门槛 |
|---|---|---|
| 信号一致率 | **比对域 = 双腿可见 universe 交集**（§5 排除池标的记豁免行，不计为不一致）；逐字段比对 symbol/neckline/entry_price/rr | 100%（W2 准入条件） |
| 挂单一致率 | 挂单标的/价格/数量 | 100% |
| 成交差异 | 同日同标的成交价差——**仿真撮合 vs 本地真实成交**：仿真成交价不代表未来实盘成交价，仅记录分布，为上实盘的预期滑点提供量级参考 | 记录 bps，无门槛（观察项） |
| 巡检动作差异 | 止损/止盈/trailing 触发时点偏差（tick 3s vs 30s） | 记录分布，W3 评估是否可接受 |
| 样本量 | 连续 20 交易日 + ≥5 笔真实信号 | 不足则顺延 |

**冻结口径（D4）**：双轨期 20 交易日内本地腿参数不晋级、UNIVERSE 不刷新、掘金 §0 快照不搬动——任何一侧口径漂移即打破 100% 一致率的可比前提。

**回退 SOP（三步，写全）**：任何异常 → ① touch RISK_BLOCK.flag（拦增量；**不撤已挂买单**——存量管理照常语义）→ ② 终端手动交易页人工撤未成交买单 → ③ 人工清仓仿真腿持仓；本地腿始终为真相源不受影响。

## 8. W0' 核对清单（终端实测，逐项核实后才写实施计划）

| 项 | 内容 | 状态 |
|---|---|---|
| **环境共存（第 0 步）** | `.venv310`（py3.10）与 gm SDK 共存；gm 对 protobuf 版本敏感（FAQ 提及 3.6.0 兼容问题）——确认不破坏现有引擎环境（必要时独立 venv） | ⚠️ 待实测 |
| 仿真通道 | MODE_SIMULATION 常量与 run 签名；仿真账户开通（免费？门槛？）；≤10 仿真策略确认；手续费/出入金设置 | ⚠️ 待实测 |
| schedule | 盘前 09:15-09:20 / 盘后 15:35 触发精度；FAQ「非交易时段实时模式不能运行」的确切语义（策略进程能否盘前存活并触发） | ⚠️ 待实测 |
| 订单族签名 | `order_volume` 限价参数、`order_cancel(cl_ord_id)`、`get_orders` 字段、部分成交状态词汇 | ⚠️ 待实测 |
| 账户查询 | cash / positions 真名与字段（CAP 扣减用） | ⚠️ 待实测 |
| 跌停价来源 | 证券基本信息接口（官方指引称含涨跌停/停复牌字段）——接口名待核 | ⚠️ 待实测 |
| 数据对拍 | `history` 定点复权 vs data_lake qfq（统一基准日，1e-6）；33000 条上限与分批策略 | ⚠️ 待实测 |
| 流控 | 盘前 300 只 × `history` 批量耗时（多标的单次调用可行性；分钟/每日流控实测） | ⚠️ 待实测 |
| 订阅 | tick 3s 推送实况；免费版 50 标的上限（巡检面 ≤8 只不受限，但须确认订阅数=持仓+挂单动态增删可行） | ⚠️ 待实测 |
| token 运维 | 获取路径；「更新即旧 token 失效」的运维规程 | ⚠️ 待实测 |
| 崩溃恢复 | 终端重启后策略进程与未终态委托的重建路径 | ⚠️ 待实测 |

## 9. 波次与遗留

**波次**：W0' 环境与口径核对（1-2 天：装终端 + gm + 仿真账户 + 核对表逐项 + 数据对拍）→ W1 单文件 v0（2-4 天：§1 逐字块过本地 pytest 基线 + 仿真挂载）→ W2 双轨仿真（≥20 交易日）→ W3 决策（掘金腿收编价值：上实盘 / 保持仿真对照腿 / 回退）。

**遗留待决**：
1. **同账户双网关冲突（W3 前必须回答）**：掘金实盘通道与 QMT 引擎绑同一东财账户——两网关并发下单的隔离/互斥方案（仿真期无碍，上实盘前必须设计：账户分设 or 时段互斥 or 收编单网关）。
2. 上实盘须直面 08-13 策略审计前提（市场过滤/跨熊 walk-forward/回测真实性均未补齐），并另行决策资金规模与中止线。
3. 若未来重拾托管诉求：本方案 VPS 化（调研路线 2），或重启东财 PTrade 核实（PTrade v2 设计已搁置备查）。

**仓库侧顺带 debt（不阻塞本设计，建议单独小工单）**：
- `strategies/neckline/signal.py:62-64` 与 `trading/compute/plan.py:170` 注释过时：仍写「scan_live entry_price=颈线」，实码 `method_v0.py:525` 为 `颈线 + buy_limit_atr_mult × ATR`；
- `signal.py:117` exec_params 键集描述未列 `buy_limit_atr_mult`，而 `method_v0.py:525` 实际消费它（§0 EXEC_PARAMS 快照已要求显式收录）。
