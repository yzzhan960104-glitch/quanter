# 掘金（东财）试点接入方案——R6-8 冠军策略换代（2026-08-26）

> 交付对象：`emquant/emquant_neckline_pilot.py`（单文件产物，指纹 `01903dec5029da9d`）
> 策略来源：ACTIVE 实验 `neckline_prop_20260825_8_loop`（R6-8 九小时循环产物，
> raw outer +515.2% / 模拟线 +511.5% / dd 8.4%；G4/wf 2024 折 −1.005 由用户明示豁免）
> 文档定位：换代接入的完整方案——变更清单 / 部署步骤 / 验证协议 / 分歧台账 / 回滚。

---

## 一、本次改写内容（相对上一代快照 `3466a8fca0b554cb`）

### 1. §0 参数区换代（export_snapshot 重导）

| 层 | 上一代 | 本代（R6-8 冠军） |
|---|---|---|
| ID | window 80 / supp 0.6 / max_h_atr 5.0 / decay None / min_rr 2.0 … | **window 60 / supp 0.3 / max_h_atr 4.5 / decay_tau 60 / min_rr 2.0** |
| EXEC | buy_limit 0.5 / cooldown 8 / max_wait 8 / max_holding 20 / tp1 1.0 / tp2(id) 2.5 … | **buy_limit 2.5 / cooldown 0 / max_wait 27 / max_holding 30 / tp1 2.0 / tp_h(id) 1.5 / chase_entry True / cancel None / trailing 10/0.05/0.5** |

快照通道不变：`experiment.db ACTIVE → build_strategy 合并 → config/params_snapshot.json + universe.json`（universe 仍为近 60 日日均额前 300 创板科创）。

### 2. pilot_body 反转 regime 支持（核心工程）

新冠军 `tp1_h_mult=2.0 > tp_h_mult=1.5`（tp1 名义位挂在 tp2 之上）——旧躯干
tp2 一触全平会把 90% 仓位在 1.5H 倒掉，**丢掉该参数集的最大单点边际**。本次为
tick 状态机补反转语义（`decide_position`）：

- **tp2 触价** → 只卖 lot2 份额 `floor(remaining×(1−tp1_portion)/100)×100`
  （reason=`tp2_share`，置 `tp2_done` 一档一次；不足一手 → `tp2_dust` 份额沉
  lot1 不落单）；
- **tp1 触价**（反转下）→ lot1 即剩余，全卖（reason=`tp1`）；
- **同日冲高承接**：跳空/连续 tick 下 tp2_share 先判、tp1 后判——tick 序天然
  复刻回测「首触 tp2 当日 lot1 若摸 tp1 按 tp1 成交」；
- **盘后 sweep**（after_close）：tp2 已触但 lot1 未出 → 置 `force_exit`，次日
  首 tick 跟价全出（reason=`tp2_eod_sweep`）——回测「lot1 随 tp2 同价平」的
  最近似（隔夜跳空风险入分歧台账）；
- 正常 regime（tp1≤tp2，历史全档）**零行为变化**（守护测试钉死）；
- 浮点防御：`(1−portion)` 下溢截断 epsilon（1.0−0.9=0.0999… 把整手截成 dust
  的实锤 bug，测试抓出）。

### 3. 评审遗留项清偿（0821 评审两项之一）

- **on_tick price 防御**（pilot_body 原 :1712）：tick 价 None/0/NaN/非数值 →
  WARN 审计（`tick_price_invalid`）+ 跳过本事件，不再炸整根回调；
- （FR7 audit 列数 spec 勘误已在上一代清偿，本次复核无回归。）

### 4. 上游白名单修复（本次发现的真 bug）

`strategies/neckline/strategy.py` 的 `_NECKLINE_EXEC_KEYS` 漏列 R6-5 腿键
（chase_entry/timeout_extend_*）——分流丢弃致 exec_cfg 静默回落默认。影响面：
**replay 口径（七门 G1-G3）与 export_snapshot 一直跑的是 chase-off 版本**
（scan 口径 G4-G6 与 R6-8 循环读数不受影响——run_full_scan 直吃 params dict）。
修列后 replay 口径与 scan 口径一致化（replay 读数将随 chase 生效变化，属预期
修正；R6-8 七门报告的 G1-G3 读数系 chase-off 口径，留档不改写历史）。

### 5. 测试

- tests/emquant **744 项全绿**（新增反转 regime 8 项守护：tp2_share/dust/
  tp2_done 持有/跳空承接/stop 优先/force_exit/正常 regime 零回归）；
- 钉旧 §0 值的测试改造为换代健壮（超期龄期动态取 `EXEC_PARAMS["max_holding"]`、
  cooldown 机制测试显式注入隔离值、fetch 行数/键集字面量随代更新）。

---

## 二、部署步骤（掘金终端）

1. **取产物**：`emquant/emquant_neckline_pilot.py`（重组装自 pilot_body+内核+
   快照；**产物不是源**——改任何源后须 `python emquant/build_pilot.py` 重跑，
   手改产物会被幂等断言拦下）。
2. **掘金量化终端**（东北证券 NET 专业版·测试版）：策略 → 新建/覆盖 → 全文粘贴
   单文件 → 保存为 `emquant_neckline_pilot`（与 state/audit 目录约定无关，运行
   目录由掘金托管）。
3. **定时事件**（ gm schedule，脚本内已注册，无需终端配置）：盘前 09:15、
   盘后 15:35、tick 订阅（持仓∪未终态挂单面）。
4. **账户**：掘金仿真账户（模拟资金）；首跑前确认 `state/`、`dl_audit/` 目录
   可写（脚本自建）。
5. **人工风控双值文件**（ADR-16，与 QMT 腿同款语义、独立文件）：
   `state/RISK_BLOCK.flag` 存在 → 只做存量管理不挂新单；`state/RISK_CAP` 数值
   → 总仓位上限比例。
6. **首日观察**（见 §四验证协议）。

## 三、与 QMT 引擎腿的双轨关系

- **同源参数**：两侧同吃 `neckline_prop_20260825_8_loop`（QMT 腿经 resolve_active
  动态读库；掘金腿经 §0 快照定稿——换代须重导+重部署，这是单文件架构的固有
  时差，README 已声明）。
- **对照域**：两腿 universe 交集（掘金 300 流动性头部 ⊂ QMT 全池）。
- **对照指标**：日度——各自 audit 流水的信号数/挂单数/成交数/成交价 vs 回测
  同日口径；周度——known_divergence 台账聚合 + 逐笔对账样本。

## 四、验证协议（评审三招 + 首日清单）

1. **重组装逐字节**：`python emquant/build_pilot.py` 后 `git diff` 零变化
   （幂等红线）；
2. **指纹复算**：no-gm import 自检（README §验证）：打印
   `PARAMS_FINGERPRINT=01903dec5029da9d`、universe 300；
3. **双 venv 实跑**：tests/emquant 744 项（本仓 .venv310）+ 掘金终端侧
   回放/仿真各一天；
4. **首日清单**：EOD 行 `params_fingerprint` 正确；tick_price_invalid 零误跳
   （除非真有脏行情）；反转形态仓（若成交）出 `TP2_SHARE`/`TP1`/`EOD_SWEEP`
   审计链；cooldown=0 下信号量级（对照 QMT 腿 55 单/日的泛滥读数）。

## 五、已知分歧台账（双轨复盘剔除口径）

| # | 分歧 | 方向 | 处置 |
|---|---|---|---|
| D1 | **chase_entry 无实盘实现**（两侧同）：27 天无回踩仅过期不追入 | pilot 少成交（保守） | 对照期剔除该子集；2022 防御 alpha 归因时单独标注 |
| D2 | tp2 触发日 lot1 未摸 tp1：回测当日同价平 vs pilot 次日首 tick 出 | 隔夜跳空双向 | audit `tp1_eod_sweep_next_open` 标记剔除 |
| D3 | lot2 份额不足一手：份额沉 lot1 | 微量 | audit `tp2_dust_sinks` |
| D4 | tick 序 vs bar 模型：同日多触发的先后（cancel_on/tp 优先序已按 decide_exit 对齐） | 微量 | 已有 C9 口径锚 |
| D5 | timeout_extend（腿 B）两侧默认关 | 无 | R6-5 否决留档 |
| D6 | 掘金 tick 限价单 limit-or-better vs 回测按目标价成交 | pilot 可占优（跳空） | 成交价对账时注意 |

## 六、风险与回滚

- **回滚参数**：`python -m experiment rollback neckline_disc_20260725_25c602`
  （QMT 腿即时生效）+ 掘金腿重导快照重部署（`export_snapshot → build_pilot →
  终端覆盖粘贴`）；
- **停 pilot**：掘金终端停止策略即可（state.pkl 原子落盘，重启自愈）；
- **急停兜底**：`state/RISK_BLOCK.flag` touch（只拦增量）+ 终端撤单面板全撤；
- **上实盘前置**（不变）：双网关冲突（本机 QMT 引擎与掘金终端并存时的柜台
  会话隔离确认）+ 08-13 审计项 + G4 豁免决策记录已在实验 note 留痕。

## 七、遗留与后续

- 掘金腿的 chase 实现（D1）——若 2022 防御 alpha 在双轨对照中显著，考虑在
  pilot_body 补「等待期末市价追入」分支（对称于 QMT 腿同样的缺口）；
- R6-8 七门 G1-G3 的 chase-on 重跑（白名单修复后 replay 口径已具备 chase 语义，
  读数刷新属下一轮排程）；
- 快照时差自动化（ACTIVE 变更 → 掘金重部署的提醒钩子）——目前靠人工，换代
  SOP 已在 README。
