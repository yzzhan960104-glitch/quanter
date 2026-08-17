# ADR-16：regime 指标闸移除，人工风控双值接管增量拦截

- 日期：2026-08-17
- 状态：已裁决（本 ADR 与代码落地同批）；**修订 1**（同日）：影子期闸一并移除
- 关联：A1/DG-G4 落地 spec `docs/superpowers/specs/2026-08-14-a1-a2-regime-and-mincalmar-design.md`；首次实弹拦截 2026-08-17 09:22（15 单全拒，HS300 4666≤MA200 4702；宽度 23%≤50%）

## Context（为什么）

A1 regime 闸（HS300 MA200 + 市场宽度双腿确认）的落地动因：颈线法 2022 熊市折外 calmar=-0.62 的负期望实证——用环境闸断新单防熊市放血。2026-08-17 首次实弹拦截后，系统所有者作出终局裁决：

> 我的策略是在择时的情况下放大收益。择时不是几个单纯的指标可以判读。只通过人工来判断。

即：市场环境判断的质量责任回归人工，自动指标不作为下单拦截依据。风控面收缩为两个人工控制的值：**①是否拦截增量下单（开关）②总仓位比例（上限）**。

## Decision（决策）

1. **regime 整体移除**：`trading/compute/regime.py`、`data_ctx.load_regime_frames/regime_state_today` 及当日缓存、`engine._regime_gate`、`_pre_open_gate` ④ 段（四段→三段）、pipeline eod 前置停产段、gateway_service 状态字段的 regime 面，连同相关测试与诊断脚本全部删除。不留 env 开关、不留死配置（对齐 A-2 删挡板与 trailing 删除先例；回滚走 git）。
2. **人工风控双值**（`logs/trading_state.db` 新 kv 表 `risk_control`，运行时可改、无需重启）：
   - `block_new_orders`：'0'（默认，不拦）/'1'（拦截一切增量买入——自动挂单与手动下单全覆盖）；
   - `max_total_position`：0.0~1.0（默认 1.0=不限制；起步建议 0.80，复活原死配置 `TRADE_MAX_TOTAL_EXPOSURE` 的语义）。
   - 修改通道：REST `GET/PUT /risk/control` + CLI `python -m trading.risk_ctrl`。
3. **消费点（只拦增量，不碰存量退出）**：
   - `compute/risk.py::check_order` 加 master_switch 关：开关 ON 拒新买单；卖出（止损/止盈/超期平仓）永不拦；
   - pre_open 挂单前 gate：开关 ON → 全部拒挂 + 钉钉播报 + 台账 skipped（接管原 regime ④ 段位置）；
   - pre_open 逐单总仓位检查：持仓市值 + 当日已挂未成交买单 + 本单金额 ≤ 比例 × 总权益，超限单跳过并播报。
4. **fail-closed 边界**：双值读取异常 → 视同拦截（宁可不挂，不可裸奔）；播报正文含当前生效值。

## 对抗性推演（为什么不是别的）

- **为什么不留观测-only（指标照算照播不拦）**：裁决明示"不需要 HS300 MA200 + 市场宽度这样的判断"——指标本身失去消费方，留观测面=死计算（SSoT review P2 同型债务），删。
- **为什么 kv DB 而非 env**：开关的生命周期是"盘中/盘前临时收紧"，env 改动需重启进程——单例锁 + QMT session 使重启成本与风险（错失窗口、session 重连）远高于一次 DB 写；且 critical.py 的 env-only SSoT 边界保持给策略参数，运维开关走 DB 不混淆两域。
- **为什么 eod 照常产信号**：拦截语义是"增量下单动作"，不是"研究停产"——信号/回测迭代数据流不断，人工开关 ON 时计划照产、挂单被拦，环境转好拨回即恢复，无需等 18:00 eod 周期。
- **为什么默认 block=0 / max_pos=1.0**：零行为变化起步 + 人工按需收紧（单向安全范式，对齐 ADR-14 的灰度纪律）；若默认收紧则本 ADR 变相成为另一套自动默认值，违背裁决本意。
- **为什么不并入 emergency_halt**：halt 是自动熔断（-3%）后的粘滞锁+停调度语义，解锁有专门 SOP；人工开关是常态控制、无熔断含义，混用会把"日常拨开关"与"事故解锁"两个人工动作搅在一起。

## Consequences

- 正面：环境判断权与责任归人工（裁决本意）；假多头/假空头指标误判不再拦单；总仓位上限从死配置（`TRADE_MAX_TOTAL_EXPOSURE` 无消费方）变为真实生效的防护层。
- 负面/代价：2022 式熊市负期望敞口重新打开——残余防护 = 人审 veto（/review + 钉钉 review bot 终局）+ 组合 -3% 熔断 + 个股止损/止盈 + 影子期闸 + 单笔 pos_cap 5%；防护质量依赖所有者看盘纪律与双值设置。此为风险偏好决策，系统所有者签署本 ADR 即接受。
- 回滚：git revert 本批提交；过渡期应急可将 `block_new_orders=1` 手动全拦（人工闸本身即是回滚手段）。

## 修订 1（2026-08-17 同日）：影子期闸一并移除

**裁决**：系统所有者确认移除 `check_shadow_gate`（TRADE_SHADOW_MIN_DAYS≥5 硬闸）——与主裁决同
一逻辑：它是又一道"自动化冻结交易"的闸。

**论据**（拆除时的风险评估）：
1. **副作用错位**：闸挡的是整个引擎而非新实验——触发时存量持仓的盘中止损巡检、超期平仓、
   组合熔断评估一并停摆。为拦"新参数上实盘太快"而冻结"存量风控监控"，代价结构与本 ADR
   "只拦增量、不碰存量退出"的裁决哲学相反。
2. **语义更优的替代已存在**：换实验时人工 `risk_ctrl block on`（只拦增量、存量退出照常、
   随时可逆）比"冻结全引擎 5 天"精确。
3. **消失的保护与残余**：新实验 promote 后即刻真金生效，无强制观察窗口。autopromote 链
   本身默认 dry-run + 七门门槛 + 钉钉播报（ADR-15）；人工 `experiment promote` 补一条上线
   WARN 播报（通知不拦截，接管影子期闸的知情价值）。

**落地**：删 `check_shadow_gate`/`_days_since_activation`/resolve_active 顶层绑定
（trading/__main__.py）+ lifespan 条件分支（presentation/server/main.py，engine 无条件
start）+ `TRADE_SHADOW_MIN_DAYS` env + 4 个测试文件清理；历史包袱注记：07-28 曾因影子期
被绕过（0→1）出过实弹问题、08-14 回 5 基线——闸的历史价值属实，但人工双值上线后其
保护对象已被更优机制覆盖。
