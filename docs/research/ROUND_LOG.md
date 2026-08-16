# 优化循环日志（ROUND_LOG）

> 长周期策略及参数优化模式的**单一时间线**：每轮一节，记录「问题假设 → 回测命令+数据指纹 → 关键数字 → 结论决策 → 下一步」。人可读；阶段 3 自动环已可自动 append。
> 协议：`docs/superpowers/specs/2026-08-16-tuning-loop-protocol.md`；全景：`docs/2026-08-16-strategy-param-landscape.md`。

---

## R0 · 2026-08-16 · 战役开启（现状梳理 + A3/A4 首轮循环）

- **假设**：基建已全就位但策略证据倒挂——A3 成交真实性/A4 Kelly 收敛是最高优先空洞（roadmap 定调）；先验证可信度再调参。
- **指纹**：HEAD `6db624fa`（战役分支 optimize/long-loop-0816）；engine_hash `53090190e55f`；snapshot `3fdcbbcb2e3160e1`（universe 1193）；ACTIVE=`neckline_disc_20260725_25c602`（note 口径 outer ann=18.4% calmar=7.24）。
- **动作与关键数字**：
  - **R0-A3 滑点敏感性**（`diag/a3_fill_realism_probe.py`，20 组 replay run，报告 `2026-08-16-a3-fill-realism.md`）：
    - 🔴 **口径裂缝实锤**：ACTIVE replay 口径 outer **-23.9%@0bps**（vs note 的 +18.4% scan 口径）；DRAFT 47d350 **-44.5%**（vs +53.4%）——**符号反转**。scan 口径（kelly_metrics 无并发约束逐笔复利）与 replay 组合口径（max_positions=6）的裂缝是系统最大单点失真。
    - 摩擦斜率温和（每 10bps 侵蚀 0.6-1.7pp）——主问题是组合约束不是交易成本。
    - stop_gap=43/611（7% 止损跳空）；n_skipped=455（组合约束吃掉 43% 信号流）。
    - 🟢 **唯一正边缘**：p_553e383d（min_rr 1.5/max_h_atr 2.5，信号 518→67）replay outer **+5.5%/dd 1.4%**——少而精信号在组合约束下存活。
  - **R0-A4 Kelly 收敛**（`diag/a4_kelly_convergence.py`，报告 `2026-08-16-a4-kelly-convergence.md`）：正 kelly 年 4/6 贴线（2022/2023 塌零是策略级特征）；wf OOS 无翻负两折归零；CV 0.92 → 分数 Kelly 可上、hat 取保守分位。
  - **C3 悬置提案**：verify 时挖出 **`_judge` dd 符号反转 bug**（负值 dd 相减把改善判成劣化——Phase C 以来 verify 裁决 dd 闸全部不可信）；修复 + 重裁决口，p_553e383d REJECTED→**APPROVED**→publish DRAFT `neckline_prop_20260816_3e383d`。
  - **C4**：`_create_experiment_draft` UNIQUE 幂等修复（08-16 06:08 daemon 整轮 crash 的根因）。
  - **C5**：补采零动作——13761 段全部 unfillable（停牌真值）；4 只尾部陈旧=真实停牌/退市，freeze 警告是正确可观测信号。
  - **T1.3 分数 Kelly 落地**（ADR-14）：TRADE_SIZING_MODE/FRACTION/HAT 三键，默认 fixed 零行为变化，单向安全。
  - **T2.2/T2.3**：DRAFT discard 状态机 + autopromote 七门（G1-G7）+ 灰度两步 + AUTO_PROMOTE_ENABLED 总开关（ADR-15）。
  - **T3.1-T3.4 自动环**：逐笔归因注入 digest / auto-verify 链 / 提案学习回路 / 18:35 门槛日报 cron。
- **G7 常量定稿**：`A3_SURVIVES_10BPS=False`（双参数集 replay outer 0bps 即负）；`A4_POS_KELLY_RATIO=0.667` → **autopromote 整体 fail-closed**——当前没有任何参数集配自动上实盘权重，这是 R0 的诚实产出。
- **DRAFT 池处置**（审计 note 留痕）：47d350/e5cb49/75fa90/a1693e 四条 discard（理由各见 audit_log）；**保留 3e383d 为 R1 种子**（唯一 replay 正边缘证据链）。
- **结论**：颈线法在组合约束口径下当前无正边缘；「搜索目标 ≠ 实盘口径」是根因级发现。首战不出参数出新认知——循环的价值已兑现。
- **下一步（R1）**：
  1. **搜索目标口径切换**：inner 快筛从 scan 切到 replay 口径（或轻量 replay 代理 + 抽样 universe 平衡成本）——循环协议 §三军规的落地。
  2. 以 3e383d 为工作基线，沿「信号质量优先」方向（min_rr/max_h_atr 收紧邻域）受控探索。
  3. 重跑 A3 探针验证新候选 → G7 常量按 ADR-15 修订流程更新。

---

## R1 · 2026-08-16 晚 · 口径修复轮（搜索目标切换 + 质量网格 + G7 翻绿）

- **假设**：R0 口径裂缝是根因——搜索目标切组合口径后，「少而精」质量方向应在组合口径下兑现正边缘并过滑点存活。
- **指纹**：engine_hash `53090190e55f → f457eeaf1946`（objective.py 入 ENGINE_FILES，机制内重置）；snapshot `3fdcbbcb2e3160e1`（universe 1193）。
- **R1-1 搜索目标切组合口径** ✅：
  - `evaluate_portfolio`（discovery/objective.py）：run_full_scan 产物 → `build_equity_curve` 组合约束后处理（max_positions=6/资金/滑点=实盘 PositionModel 同源）+ 分年 min_yearly_calmar + sharpe 补充键；`worker._objective_fn` env 切换（`DISCOVERY_OBJECTIVE`，默认 portfolio，scan 为对照口）。
  - **标定背书**（diag/r1_portfolio_calibration.py）：三参数集六段全符号一致；3e383d 段逐位吻合（inner +1.81%/outer +5.50%）；25c602 的 716 笔仅 174 入净值（组合约束吃掉 76%）而 3e383d 85 笔入 75——**信号越泛滥口径折损越大，裂缝机理再添一证**。提速 ~1.7x。
  - 端到端冒烟（`discovery run` 单 trial）抓出 calmar 负值 dd 误走 inf 分支的真 bug 并修复；daemon 今夜起以组合口径搜索（新 trial 时代）。
  - **附带治理修复**：提案 publish 物化全参数（3e383d 存量 2 键 partial 是 scan 侧 KeyError 地雷，audit `r1-repair` 物化为 21 键）；autopromote 测试污染修复（patch 窗口 lazy-import 冻结 fake 的实弹教训）。
- **R1-2 质量网格**（diag/r1_quality_grid.py，14 格 replay 口径@5bps，inner 选择/outer 报告）：
  - **inner 冠军 `touch3`**（min_touches 2→3）：inner +3.7% (n=73) / outer +2.3% dd -1.7% → DRAFT `neckline_r1_touch3_20260816`。
  - **min_rr 在 1.2-1.8 档完全非 binding**（同 mh 档逐位相同——实际 rr 分布远高于档位，P4 复活的参数再度半死）；**质量方向实质由 max_h_atr 单维驱动**（2.0 全灭 n=9 / 2.5 精选 / 3.0 泛滥 279 笔弱）；vol2.0/supp0.7 把 outer 打负——过严质量闸反噬。
  - win60 与 base 逐位一致（无操作格=内部一致性检查 ✓）。
- **R1-3 A3 探针翻绿** ✅（--experiments 两验体）：
  - **touch3：盈亏平衡 48.6bps，10bps 存活率 79% → 可存活**；**3e383d：51.0bps，89%，50bps 仍 +2.6% → 可存活**；ACTIVE 仍薄边缘（0bps 即负）。
  - **G7 `A3_SURVIVES_10BPS` False→True**（ADR-15 修订记录：常量语义=「存在过滑点存活的候选方向」，非全参数集背书）。
- **七门实弹（touch3 dry-run，G7 翻绿前跑）**：G3 ✓（dd 1.7% vs 基线 18.5%）/ G4 ✓（wf oos calmar 全 ≥0：0.0/3.72/0.54/0.0）/ G5 ✓（邻域均值 0.96 ≥ base×0.5）/ G6 ✓（DSR 0.9999）；**G1 ✗（inner n=73<100）**；**G2 ✗（calmar 代理 1.375<1.5——outer dd 1.7% 太浅使比值吃亏，绝对改善已过 ann≥0）**；kelly_hat=0.0（分年下三分位，2022/2023 塌零拉低——保守方向特性）。
- **结论**：口径修复轮三件事全部达成；R1 不 promote（G1 拦截，诚实态）。**留用户决策点：G1=100 与质量方向的结构性矛盾**——质量收紧天然减 n（touch3 73 / 3e383d 85 均 <100），保持门槛则质量候选永远差一步；降门槛有「为特定候选调闸」之嫌（ADR-15 自己的警告）。建议：保持 G1=100，R2 网格向「n≥100 的质量组合」探索（touch3×window80 / max_h_atr 2.7-2.8 细分档），凑足样本量再过闸。
- **下一步（R2）**：
  1. 质量网格扩展：touch3 邻域 × window/流动性维度，目标 inner n≥100 且 ann 不塌。
  2. 观察首夜组合口径 daemon 搜索产出（trial 形状/前沿质量 vs scan 时代）。
  3. auto_publish 桥的 note 口径对齐（ACTIVE note 仍是 scan 口径 18.4%，组合口径 trial 与之比较会保守停滞——已知边界，待组合口径冠军出现时随 promote 换 note）。
