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
