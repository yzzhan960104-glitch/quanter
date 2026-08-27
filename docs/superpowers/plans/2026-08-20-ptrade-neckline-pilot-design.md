# PTrade 颈线策略单文件试点 · 设计方案（2026-08-20 · v2 评审回填）

> **状态**：**已搁置（2026-08-21）**——D1 撤回（纯用东财掘金），试点载体改为 gm SDK 单文件：见[东财掘金版设计](2026-08-21-emquant-neckline-pilot-design.md)；若未来重拾券商托管诉求再启用本文。以下 v2 内容（含决策记录与全量评审回填）保留备查。姊妹篇：[券商托管执行调研](../../research/2026-08-20-broker-hosted-execution-feasibility.md)（§8.1 已按码评估「能装进一个文件」）；[富途接入调研](../../research/2026-08-20-futu-integration-feasibility.md)；[东财掘金接入调研](../../research/2026-08-21-emquant-integration-feasibility.md)（§五 三条路线与拍板记录）。
> **事实审查红线**：本文 PTrade 接口名按公开文档通识书写，**全部以 §八 W0 核对表核实为准**，核实前不写实施计划。

## 0. 目标与非目标

**目标**：把颈线策略（已定稿参数集）的「识别 + 挂单 + 巡检出场」完整闭环装进**一个 PTrade 策略文件**，在券商服务端托管运行（关机可跑），**PTrade 腿全程模拟盘**，与本地引擎双轨对照 ≥20 交易日，验证托管腿与本地腿信号/挂单/巡检动作一致性。

**非目标**（明确不搬，留在本地腿）：
- discovery / autopromote / 参数搜索（optuna 等不在 PTrade 内置库，参数以**定稿快照**形式硬编码进文件，见 §2 §0 段）；
- fill 真相源 / 对账 / 钉钉告警链 / job_ledger 台账（治理降级清单见调研 §六 + 本文 §6，试点期以 audit CSV + 人工复核补位）；
- 熔断/简报等盘后分析（本地腿继续做，PTrade 腿只产 audit 原始件）；
- **实盘资金**：模拟盘阶段零真金；若 W3 裁决收编，收编前须另行决策 W2b 小资金实盘过渡（§9）。

### 0.5 决策记录（2026-08-20 复审拍板）

| # | 问题 | 结论 | 影响 |
|---|---|---|---|
| D1 | 先方案 A（VPS 上云）还是直接 B（PTrade 托管）？ | **直接 B**——诉求是「决策逻辑必须住券商侧」，非单纯无人值守 | 方案 A 不做（调研 §九-2 的动机问题已答） |
| D2 | 真金还是模拟盘？ | **模拟盘**（PTrade 支持模拟环境） | 资金上限/亏损中止线/双敞口问题全部消解；模拟盘口径一致性升为 W0 硬项（§8） |
| D3 | 无人值守下人工择时闸谁触？ | **接受试点期无人择时** | 零真金 + ≤2 单/日硬闸兜底；W0 附带核实研究目录远程触达通道（非门槛，供收编后用） |
| D4 | 双轨期参数/池子动不动？ | **参数 + UNIVERSE 双冻结** 20 交易日 | 本地不晋级、池子不刷新、快照不搬动；一致率分母由此可定义（§7） |
| D5 | 同信号双账户双敞口？ | **消解**（模拟盘无真实敞口） | 本地腿真金照常跑现状信号，不受影响 |
| D6 | 券商？ | **东方财富**（现券商） | 双腿同一券商（本地 QMT 腿 + PTrade 托管腿）便于对账与资金观察；**W0 第 0 步核实东财是否实际提供 PTrade**——公开渠道清单（国金/国盛/华鑫等）未含东财，且东财自家「东方财富量化」（emquant）≠ 恒生 PTrade；若不提供，D6 重议（换券商，或另立 emquant 平台调研——不在本设计范围） |

**定位声明**：本试点只裁「托管执行腿的工程一致性」，**不构成颈线策略晋级/加仓依据**。08-13 策略审计裁定（可信度低、不建议实盘资金）与模拟盘试点不冲突；但 W3 若走到收编=真金上线，须重新直面该审计前提（§9 遗留-2）。

## 1. 总体架构：引擎四 job ↔ PTrade 事件映射

| 本地（现状） | PTrade 事件 | 语义对齐说明 |
|---|---|---|
| eod_plan：T-1 晚扫描（`detect_signal(df_upto 截至 T-1)`）→ SIGNAL.meta 落库 → auto confirm | `before_trading_start` 阶段① | 识别数据口径同为「截至 T-1 收盘」（`get_history` 取昨日及以前，不含当日）；auto-confirm 语义 = 自动挂单，人工闸由 RISK_BLOCK.flag 承接（§6） |
| pre_open 09:22：撤昨日单 → 超期平仓 → ADR-16 双值闸 → 逐单挂限价买 | `before_trading_start` 阶段②③④⑤ | 顺序照搬本地红线：先撤后挂；额度逐单扣减 fail-closed |
| stop_loss 30s 巡检（止损/止盈/撤单/trailing） | `handle_data`（分钟频率）或 `run_interval` | **粒度差异是已知观察项**（30s→1m），W2 双轨量化影响；tick 级可选（§8 待核对） |
| post_close 15:30：对账/熔断/简报 | `after_trading_end` | 只落 audit CSV（§2 §7 段）；熔断与简报留本地腿 |
| pipeline 18:00 数据同步 / catchup 补跑 / job_ledger | 无对应 | 数据按需 `get_history`；补跑以状态文件幂等（§3）；台账降级为 audit CSV |

## 2. 单文件分区设计（预估 ~1600 行）

```
ptrade_neckline_pilot.py
├─ §0 参数区          ~80 行   ID_PARAMS / EXEC_PARAMS / UNIVERSE 定稿快照 + PARAMS_FINGERPRINT（来源
│                              本地 engine_hash 与参数集 ID）。EXEC_PARAMS 键集须含 buy_limit_atr_mult——
│                              method_v0.py:525 实际消费它，signal.py:117 键集清单漏列，以实码消费为准
├─ §1 识别内核        ~790 行  method_v0.py 652 行**零编辑逐字块** + signal.py 133 行 Signal **全量照抄**
│                              （不精简：detect_signal 以关键字实参构造 Signal，精简必破「逐字」；
│                              全量只多 ~130 行。唯一允许的编辑 = `from .signal import Signal` 改内嵌）
├─ §2 数据层          ~80 行   get_history 封装 → df_upto 构造（截至 T-1、fq 前复权断言、列名对齐
│                              open/high/low/close/volume、DatetimeIndex）
├─ §3 状态层          ~150 行  state.pkl 读写（**tmp+rename 原子落盘**）+ 崩溃恢复/幂等（§3）
│                              + RISK_BLOCK/CAP 读取
├─ §4 风控闸          ~100 行  block flag / 总仓额度 fail-closed / 试点硬闸（§6）
├─ §5 订单工具        ~120 行  挂限价/撤单/查未成交/查持仓/快照价/跌停价（接口名待 §8 核对）
├─ §6 事件处理        ~250 行  initialize / before_trading_start（五阶段）/ handle_data 巡检 /
│                              after_trading_end 落 audit
└─ §7 对拍审计         ~50 行  audit_YYYYMMDD.csv（信号/挂单/撤单/成交/巡检动作逐行）
```

**依赖自检**：§1 只用 pandas/numpy + 标准库 math；§0/§3-§7 只用标准库 + PTrade API。无任何本仓库 import。

**版本红线（W0 死活题）**：§1 依赖 `numpy.lib.stride_tricks.sliding_window_view`（numpy≥1.23）与 dataclasses（py≥3.7）——券商内置版本不符则「逐字搬」不成立，回到设计层另议；**不可顺手改写向量化实现**（等价红线，改写即背离「同一份识别内核」的试点前提）。

**等价证据（同一测试基线）**：§1 布局保证该分区可原样拷出为本地 .py 供 pytest import，直接跑既有 `test_neckline_recognition` / `test_detect_signal` 等用例——对拍从「另写对照单测」升为「同一测试基线」，这是单文件试点能拿到的最强等价证据。

## 3. 状态模型（研究目录）与崩溃恢复

```
research://neckline_pilot/
  state.pkl            # 见下 schema
  RISK_BLOCK.flag      # 存在 = block_new_orders（人工 touch/rm，唯一择时闸，ADR-16）
  CAP.txt              # max_total_position 数值（人工编辑，缺省 1.0）
  audit_YYYYMMDD.csv   # 每日对拍日志（本地腿每日拉取）
```

`state.pkl` schema（v1）：

```python
{
  "version": 1,
  "scan_done": {"2026-08-20"},          # 当日已扫描标记（幂等防重扫）
  "placed":  {"2026-08-20": [ptrade_order_id, ...]},  # 当日已挂（幂等防重挂）
  "orders":   {order_id: {"symbol","date","price","qty","purpose","exec_params","status"}},
  "positions":{symbol: {"entry_date","entry_price","qty","remaining_qty",
                        "stop","tp1_price","tp1_done","tp2_price",
                        "trailing":{"activated","cur_stop"},"exec_params"}},
}
```

**写入纪律**：state.pkl 一律「写临时文件 + rename」原子落盘——崩溃落在写入中间 = pickle 损坏，两行代码消灭一类恢复场景。

**崩溃恢复语义（对齐本地 has_order/UNIQUE 防重挂的意图）**：
- `before_trading_start` 幂等三查：state 当日标记 + `get_open_orders` 柜台实况 + `get_positions` 柜台实况；**以柜台实况为准修 state**（绝不逆推——state 说挂了但柜台没有 → 视为未挂，重新走闸后可补挂；柜台有而 state 无 → 吸收进 state 继续管理，exec_params 从 §0 定稿快照回填）。
- `handle_data` 崩溃重启：只损失巡检时效，状态经同一路径恢复；trailing 水位从 `cur_stop` 与当日行情重放推导。
- 识别本身幂等（纯函数），重扫零风险。
- **前提**：以上依赖「异常退出后当日事件会重跑」——PTrade 是否补跑 `before_trading_start` 是 §8 W0 新增核对项；若确认不补跑，当日一致性损失记为观察行而非静默。

## 4. 订单生命周期规则全集（从 exec_params 键集导出）

> **公式实证（2026-08-20 复审逐条对照 master 实码）**：entry 公式与 `method_v0.py:525` 一致；stop/tp1/tp2/cancel_on 与 `price_levels.py` 单源一致。仓库 `signal.py:62-64` / `trading/compute/plan.py:170` 两处注释过时（仍写「scan_live entry=颈线」），**以实码为准**（debt 见 §9）。

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

## 5. 数据链路与复权对拍（最大正确性风险，W0/W1 主验收）

1. 数据源只用 `get_history`（fq 前复权、不含当日）；**不用内置 tushare**——少一层口径。
2. **对拍方法**：本地从 data_lake 导出 UNIVERSE × 近 `2×window` 日 qfq 日线 CSV → PTrade 研究目录上传 → 同参数取数逐列比对（|Δclose| 相对容差 1e-6）；不一致标的进排除池并记录原因（复权因子时点差）。
3. **信号级对拍**：双轨每日信号集合（symbol / neckline / entry_price / rr）**要求 100% 一致**才准进 W2 实盘腿——颈线位差一分钱信号日期就分叉，没有「近似一致」。

## 6. 风控面（ADR-16 沙箱化 + 试点硬闸 · 模拟盘版）

**ADR-16 双值的沙箱模拟**：
- `block_new_orders` → `RISK_BLOCK.flag` 文件存在即全拦（存量管理照常：撤单/超期/trailing 不受影响，与 `pre_open.py:478` 同口径）；**唯一择时闸，无任何指标闸**——本地 ADR-16（2026-08-17）已退役全部 regime 指标闸（`engine.py:453` / `pipeline.py:244`），两侧语义一致。
- `max_total_position` → `CAP.txt` 数值；挂单前 `总权益 × CAP − 持仓市值 − 已挂买单金额` 逐单扣减（本地口径含未终态买单，`state_store.py:1470` 同义；`pre_open.py:540` 同式）；**fail-closed**：资金/持仓查询失败 → 当日不挂（宁错拦人工补，不裸奔）。

**试点期附加硬闸**（验收后可放开，写死在 §0 参数区）：单日新挂 ≤2 单；单票市值 ≤5%（模拟账户语义内，作为收编后的纪律预演）；audit CSV 每日人工复核（替代钉钉 CRITICAL 链）。

**无人择时的显式接受（D3）**：模拟盘阶段 `RISK_BLOCK.flag` 仅人工在客户端 touch（唯一通道）；托管卖点=关机无人值守 ⇒ 试点期实质「无人择时」，以零真金 + ≤2 单/日硬闸兜底。W0 顺带核实研究目录有无客户端外远程管理通道（网页端），**不设为门槛**——收编实盘前若有通道则恢复人工闸可达性，无通道则收编决策须重新评估。

**治理降级清单（相对本地腿，试点期明示接受）**：拒单风暴自动拨 block_new_orders（`order_state.py:599-620`）不在 PTrade 腿——由 ≤2 单/日硬闸兜底规模、audit 人工复核兜底发现；熔断基线 / CRITICAL 告警链 / 对账 drift 告警同理不在场。

## 7. 双轨对照与验收指标

| 指标 | 口径 | 门槛 |
|---|---|---|
| 信号一致率 | **比对域 = 双腿可见 universe 交集**（§5 排除池标的记豁免行，不计为不一致——否则 100% 在数学上不可达）；逐字段比对 symbol/neckline/entry_price/rr | 100%（W2 准入条件） |
| 挂单一致率 | 挂单标的/价格/数量 | 100% |
| 成交差异 | 同日同标的成交价差——**模拟撮合 vs 本地真实成交**：模拟盘成交价不代表未来实盘成交价，仅记录分布，为 W2b 实盘过渡的预期滑点提供量级参考 | 记录 bps，无门槛（观察项） |
| 巡检动作差异 | 止损/止盈/trailing 触发时点偏差（1m vs 30s） | 记录分布，W3 评估是否可接受 |
| 样本量 | 连续 20 交易日 + ≥5 笔真实信号 | 不足则顺延 |

**冻结口径（D4）**：双轨期 20 交易日内本地腿参数不晋级、UNIVERSE 不刷新、PTrade §0 快照不搬动——任何一侧口径漂移即打破 100% 一致率的可比前提。

**回退 SOP（三步，写全）**：任何异常 → ① touch RISK_BLOCK.flag（拦增量；**不撤已挂买单**——存量管理照常语义）→ ② 券商 App 人工撤未成交买单 → ③ 人工清仓 PTrade 腿持仓；本地腿始终为真相源不受影响。

## 8. W0 API 核对清单（事实审查红线：逐项核实后才写实施计划）

| 用途 | 预期接口（通识） | 状态 |
|---|---|---|
| **券商侧：东财 PTrade 可用性** | 东方财富证券是否提供 PTrade（恒生）通道、开通门槛、模拟盘支持——公开信息未证实（D6） | ⚠️ 新增·**第 0 步**——不通则 D6 重议，后续项全部悬置 |
| **运行环境版本** | Python 版本 + numpy/pandas 版本（`sliding_window_view` 需 numpy≥1.23、dataclasses 需 py≥3.7） | ⚠️ 新增·**死活题**——版本不符则逐字搬不成立（§2 版本红线） |
| **模拟盘口径一致性** | 模拟环境 `get_history`/撮合语义 vs 实盘（原待决4 升格——模拟盘是 W2 主体，此项从背景变主角） | ⚠️ 新增·硬项 |
| 日线数据 | `get_history(count, frequency='1d', fq='pre', include=False)` | ⚠️ 待核对签名与 include 语义 |
| 限价下单 | order 族（按股数 + 限价参数） | ⚠️ 待核对 |
| 撤单 | `cancel_order(order_id)` | ⚠️ 待核对 |
| 未成交/全量订单查询 | `get_open_orders` / `get_orders` | ⚠️ 待核对 |
| 成交查询 | trades/deals 族 | ⚠️ 待核对 |
| 实时快照 | `get_snapshot` | ⚠️ 待核对 |
| 间隔/定时巡检 | `run_interval` / `handle_data` 频率设定 / `tick_data` | ⚠️ 待核对（粒度选项决定 §1 巡检实现） |
| 资金/持仓查询 | account/positions 族 | ⚠️ 待核对 |
| **before_trading_start 时点与时限** | 事件触发时刻（本地 09:22 语义不可强求复刻，记录差异）；300 只 × 2×window 日线盘前扫描耗时实测 | ⚠️ 新增（结论反哺 UNIVERSE 上限） |
| **策略崩溃后当日续跑语义** | 异常退出后是否自动重启、从哪个事件补跑（§3 幂等三查的前提） | ⚠️ 新增 |
| **跌停价来源** | snapshot 涨跌停字段 or 昨收×档位自算（主板 10% / ST 5% / 北交所 30%——UNIVERSE 若含此类标的须全覆盖） | ⚠️ 新增 |
| 研究目录 | `get_research_path()` | ✅ 已证实（ptradeapi.com） |
| 研究目录远程管理通道 | 网页端能否 touch/编辑研究目录文件 | ⚠️ 新增（非门槛，D3 附带核实） |
| `g` 全局变量持久化 | 可序列化部分自动落地 | ✅ 已证实（文档） |

## 9. 波次与待决问题

**波次**：W0 环境与口径核对（1-2 天：**东财 PTrade 可用性核实（第 0 步，不通则 D6 重议）**、开通、核对表逐项跑通含版本死活题、数据对拍、模拟盘口径验证）→ W1 单文件 v0（2-4 天：§1 逐字块过本地 pytest 同一基线 + PTrade 模拟盘挂载）→ W2 双轨模拟盘（≥20 交易日）→ W3 决策（收编 → **须先过 W2b 小资金实盘过渡再全量** / 回退）。

**已决问题**（2026-08-20 评审拍板，见 §0.5）：券商=东方财富（待 W0 核实 PTrade 可用性）；模拟盘；无人择时接受；参数+UNIVERSE 双冻结；实盘收编须另行决策（W2b）。

**遗留待决**：
1. 巡检粒度：分钟级 `handle_data` 起步，还是直接争取 tick？影响 trailing/cancel_on 时点语义（W0 看券商能力定）。
2. W2b 小资金实盘过渡的资金规模与准入门槛——W3 收编决策时再定；届时须直面 08-13 策略审计前提（市场过滤/跨熊 walk-forward/回测真实性均未补齐）。

**仓库侧顺带 debt（不阻塞本设计，建议单独小工单）**：
- `strategies/neckline/signal.py:62-64` 与 `trading/compute/plan.py:170` 注释过时：仍写「scan_live entry_price=颈线」，实码 `method_v0.py:525` 为 `颈线 + buy_limit_atr_mult × ATR`；
- `signal.py:117` exec_params 键集描述未列 `buy_limit_atr_mult`，而 `method_v0.py:525` 实际消费它（§0 EXEC_PARAMS 快照已要求显式收录）。
