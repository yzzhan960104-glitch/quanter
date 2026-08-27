# 东方财富量化（东财掘金）接入调研：与 QMT 的关系 + 单文件集成可行性（2026-08-21）

> **命题**：东方财富证券自家的「东方财富量化」（emquant.18.cn）如何接入 QMT？颈线策略「一个 py 文件集成所有内容」在它上面是否可行？
> **姊妹篇**：[券商托管执行调研](2026-08-20-broker-hosted-execution-feasibility.md) · [PTrade 颈线单文件试点设计 v2](../superpowers/plans/2026-08-20-ptrade-neckline-pilot-design.md)（D6 已定东方财富，PTrade 可用性待核实）。
> **证据基线**：emquant.18.cn 官方帮助文档（FAQ / 快速开始 / 策略指引，2026-08-21 抓取）+ 公开渠道交叉。接口名按官方指引书写（部分为伪代码级），精确签名以终端内 API 文档为准（§八 诚实边界）。

## 一、TL;DR 裁决

| 问题 | 裁决 | 一句话 |
|---|---|---|
| 「东方财富量化」是什么 | **东财版掘金终端**（官方文档自称「东财掘金帮助文档」） | 本地 Windows 终端 + `gm` SDK（`pip install gm`），与 myquant 掘金同源 |
| 如何接入 QMT | **不存在官方互通** | 掘金（gm SDK）与 QMT（xtquant）是两套独立体系；QMT 权限向东财另行申请，与掘金终端无关。本仓库现有引擎本就是 QMT 系（xtquant/miniQMT）——「接入 QMT」这件事已完成，东财掘金是**另一套 SDK 的重写**，不是接入 |
| 一个 py 文件集成所有内容 | ✅ **成立，且比 PTrade 容易一个量级** | 本地 Python（3.6-3.12）自由依赖、官方明示支持第三方库与外部数据、无券商锁定版本、无「仅单文件」部署约束 |
| 满足 D1（决策逻辑住券商侧/关机可跑） | ❌ **不满足** | 官方指引原文：「SDK 运行无法脱离终端（很多动态资源是终端在管），需要指定一个登陆状态的终端」——策略进程必须连**本地在线**终端，关机即停。这是又一条本地腿，不是托管 |

## 二、平台真身辨析（三个易混名，先钉死）

| 名称 | 真身 | 与本题关系 |
|---|---|---|
| **东方财富量化终端**（emquant.18.cn，帮助文档自称「东财掘金」） | 东财版掘金量化终端：本地 Windows 图形终端 + 多语言策略 SDK（Python 的 SDK 即 `gm` 包） | **本题主体**。落地式终端，策略实盘/仿真/回测三模式 |
| **Choice 数据量化接口**（quantapi.eastmoney.com，EmQuantAPI） | 东财 Choice 金融数据 API（纯数据，无交易通道） | ⚠️ 域名 emquant 与 EmQuantAPI **撞名**——它是数据服务，不是本题的交易终端 |
| **迅投 QMT / miniQMT**（xtquant） | 迅投 ThinkTrader 体系，券商分发 | 本仓库现有引擎的通道。与掘金**无互通**；「东方远见 QMT 申请页」属**东方证券**（dfzq.com.cn）≠ 东方财富证券，勿混淆 |

另：叩富网单源称东方财富证券「支持恒生 PTrade 和迅投 QMT，50 万资金要求」（§五路线 3 的核实线索）；本仓库 QMT 腿若本就开在东财，则该能力已被事实上使用。

## 三、运行模型实证（官方文档逐条，试点设计的事实底座）

**进程拓扑**：本地 Windows 终端（图形界面，含策略管理/监控/手动交易/本地风控）+ 策略进程（本地 python 跑 main.py，可用第三方 IDE 如 PyCharm；`serv_addr` 指向在线终端）+ 数据/仿真服务（云端）。**关键**：FAQ 明示「策略委托根据交易通道的配置，直接发送到对应的券商柜台」——实盘下单不经东财服务器中转策略逻辑，但**策略进程本身离不开本地终端**。

| 维度 | 实证（官方 FAQ/指引） | 试点含义 |
|---|---|---|
| Python 环境 | 3.6-3.12；SDK `pip install gm`；支持第三方 IDE | numpy 版本死活题消失——直接用本仓库 `.venv310` 同款环境 |
| 第三方库/外部数据 | FAQ 明示「支持用户自由使用第三方库和外部文件、数据等」 | 单文件可 import 自由；状态可用 pickle/sqlite 任意持久化 |
| 事件模型 | `run(strategy_id, filename, mode, token, serv_addr)`；`init(context)`；`on_bar/on_tick/on_order/on_execution_report`；`schedule(schedule_func, date_rule='1d', time_rule='09:30:00')` 定时 | 四 job 映射同构 PTrade 设计 §1（schedule 盘前/盘后 + on_bar/on_tick 巡检） |
| 运行模式 | `MODE_LIVE=1 / MODE_BACKTEST=2`（仿真常量未见于已抓文档，W0' 核对） | 仿真柜台：每账号 ≤10 个仿真策略，可设手续费/出入金 |
| 下单族 | `order_volume / order_value / order_target_volume / order_cancel(cl_ord_id) / get_orders`；委托主键 `cl_ord_id` | 限价买/撤单/查单齐全；沪南市价/盘后定价参数表齐备 |
| 账户查询 | `cash() / positions()`（指引伪代码写法） | CAP 扣减可落地 |
| 行情订阅 | `subscribe(symbols, frequency, count)`；tick 3 秒推送；bar 在 eob 后可得；**免费版订阅 ≤50 标的** | 巡检粒度 **tick 3s，优于 PTrade 1m**，接近本地 30s；50 标的上限只约束订阅——巡检仅订持仓+当日挂单标的（颈线试点 ≤8 只），识别走 history 批量，不受限 |
| 历史数据 | `history(symbol, frequency, start_time, end_time, adjust, adjust_end_time, df=True)`；**定点前复权**（ADJUST_PREV + adjust_end_time）；单次 33000 条；数据接口有分钟/每日流控（自动冷却等待） | 复权口径**可显式定点**——比 PTrade 黑盒口径更可控；对拍时与 data_lake qfq 统一基准日即可 |
| 非交易时段 | FAQ：「非交易时段，实时模式下策略不能运行」 | 策略进程生命周期（常驻 or 每日拉起）是 W0' 核对项 |
| 运维脆弱点 | token 更新即旧 token 失效；同账户异地登录会被踢；终端须在线 | 与 QMT 同级的本地运维负担，非托管 |

## 四、「一个 py 文件集成所有内容」：可行性设计（对照 PTrade 设计 v2 逐项）

**分区沿用 PTrade 设计 §2 骨架**（§0 参数快照 / §1 识别内核 / §2 数据层 / §3 状态层 / §4 风控闸 / §5 订单工具 / §6 事件处理 / §7 对拍审计，~1300-1600 行），差异点：

| 项 | PTrade 版 | 东财掘金版 | 难度变化 |
|---|---|---|---|
| §1 识别内核 | 逐字搬 + 版本死活题（numpy≥1.23） | 逐字搬 + 本地自由环境 | ⬇️ 死活题消失 |
| 部署单元 | **必须**单文件（券商沙箱） | 单文件是**选择**不是约束（可多文件/直接 import 本仓库） | ⬇️ |
| §2 数据层 | `get_history`（口径黑盒待核对） | `history(adjust_end_time=)` 定点复权，口径可控 | ⬇️ |
| 巡检粒度 | handle_data 1m（tick 待核对） | on_tick 3 秒推送 | ⬇️ 更接近本地 30s 语义 |
| §3 状态 | 研究目录 pickle + 原子写 | 本地文件系统任意（pickle/sqlite） | ⬇️ |
| §6 事件续跑/崩溃恢复 | W0 未知数 | 策略进程=本地进程，崩溃恢复语义自己写（supervisor 同本地引擎） | ➡️ 已知范式 |
| 托管属性 | ✅ 券商服务端 | ❌ 本地终端在线才活 | ⬆️ **核心倒退** |

**等价证据**：§1 逐字块照搬 PTrade 设计 §2 纪律（Signal 全量照抄、本地 pytest 同一基线直跑 `test_neckline_recognition` 等）——两个试点共用同一识别内核与同一测试基线，只有执行编排层（§5/§6）分叉。

**额外红利**：掘金自带 `MODE_BACKTEST` 回测——单文件可直接冒烟，但**禁止**用掘金回测做策略判断（撮合语义与本仓库 `backtest.simulate_exit` 不同，08-13 审计口径仍以本仓库回测为准）。

## 五、与 D1/D6 的冲突：东财旗下三条路线

**冲突陈述**：D1 拍板「决策逻辑必须住券商侧」→ 直接 B（PTrade）。东财掘金**不满足** D1（本地腿）。东财旗下可选：

| 路线 | 形态 | 满足 D1？ | 成本/前置 |
|---|---|---|---|
| 1. 东财掘金当**排练场** | 单文件试点先在掘金仿真跑通（识别内核/状态模型/audit 格式/对拍方法论全量验证），PTrade 版只重写执行编排层 | ❌（但产出可复用） | 最低：仿真免费、无版本死活题、无券商沙箱约束 |
| 2. 东财掘金 + VPS | Windows VPS 跑终端+策略 | ❌（托管感来自 VPS，= 变相方案 A） | VPS ¥200-400/月 + 终端保活运维 |
| 3. 东财 PTrade | 若东财提供 PTrade（cofool 单源：支持，50 万门槛） | ✅ 唯一满足 D1 的东财路径 | W0 向东财核实 + 开通 |

**拍板（2026-08-21）**：**纯用东财掘金**——D1（券商托管诉求）撤回，接受本地终端形态；路线 3（东财 PTrade 核实）关闭，PTrade 版设计（08-20 v2）搁置备查。试点设计见[东财掘金颈线单文件试点设计](../superpowers/plans/2026-08-21-emquant-neckline-pilot-design.md)。

## 六、若走掘金（路线 1）的 W0' 核对表

| 项 | 内容 | 状态 |
|---|---|---|
| 终端 + SDK | 终端下载安装（东财账户）、`pip install gm` 版本与 py3.10 兼容、token 获取 | ⚠️ 待实测 |
| 仿真通道 | 仿真常量（MODE_SIMULATION?）、仿真账户开通条件（免费？门槛？）、≤10 策略限制确认 | ⚠️ 待实测 |
| schedule 时点 | 盘前 09:20/盘后 15:35 定时触发精度；非交易时段策略进程生命周期（常驻 or 每日拉起） | ⚠️ 待实测 |
| 数据对拍 | `history` 定点前复权 vs data_lake qfq：UNIVERSE × 2×window 日 CSV 逐列比对（统一 adjust_end_time 基准日，容差 1e-6）；不一致进排除池 | ⚠️ 待实测 |
| 订阅上限 | 免费版 50 标的实况；付费档数字 | ⚠️ 待实测 |
| 流控影响 | 盘前 300 只 × history 批量拉取耗时（多标的一次调用是否可行）；分钟/每日流控实测 | ⚠️ 待实测 |
| 崩溃恢复 | 策略进程/终端重启后：cl_ord_id 查询重建状态、挂单幂等三查（同 PTrade 设计 §3 语义） | ⚠️ 待实测 |
| 跌停价来源 | 证券基本信息接口（官方指引提及含涨跌停/停复牌字段）——接口名待核 | ⚠️ 待实测 |

## 七、参考

- [东方财富量化帮助中心（emquant.18.cn）](https://emquant.18.cn/help/?doc=guide) · [常见问题（东财掘金）](https://emquant.18.cn/help/doc/faq/) · [快速开始/终端指引（emt.18.cn 镜像）](https://emt.18.cn/api/quant-help/guide/guide.html) · [策略指引 30 分钟入门](https://emt.18.cn/api/quant-help/guide/559.html)
- [掘金量化终端（myquant.cn，同源平台）](https://myquant.cn/terminal?intro=stock)
- [Choice 数据量化接口（EmQuantAPI，撞名辨析）](https://quantapi.eastmoney.com/)
- [叩富网：东财支持 PTrade/QMT 单源（50 万）](https://licai.cofool.com/ask/qa_3042941.html) · [同城理财：东财量化 10 万（冲突源）](https://licai.jiantou8.com/ask/qa_5668392_1_2.html)
- [东方证券（≠东财）QMT 申请页，防混淆](https://yuanjian.dfzq.com.cn/newHome/qmt)

## 八、诚实边界

- gm API 名称按官方策略指引书写，该指引**明示以伪代码表述**——精确函数签名/常量（如 MODE_SIMULATION、cash/positions 真名）未逐条核实，W0' 以终端内 API 文档为准。
- 「东财掘金与 QMT 无互通」是基于缺席证据（双方官方文档均无互通方案）+ SDK 体系不同（gm vs xtquant）的判定，非官方否定声明。
- 东财量化门槛多源冲突（10 万 / 30 万普通柜台+300 万极速 / PTrade·QMT 50 万），均非官方口径；以东财客户经理书面答复为准。
- 「东财支持 PTrade」为叩富网单源问答，且本仓库 QMT 腿现券商未在文中确认——若 QMT 腿本就在东财，可视为事实旁证，但仍需官方确认 PTrade 产品线。
- 本文未安装/实测终端与 SDK；流控表、订阅上限等数字来自官方 FAQ 快照（2024-2025 更新痕迹），现行版本可能已变。
