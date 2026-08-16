# ADR-15：量化门槛自动 promote（autopromote 七门）

- 日期：2026-08-16
- 状态：已落地（G7 常量随 R0 报告定稿回填；总开关默认关）
- 编号说明：与 ADR-14 同因避让 discovery spec 内部 ADR11-13，接 architecture 序列
- 关联：ADR-14（分数 Kelly）；`research/autopromote.py`；T2.1 循环协议；R0-A3/R0-A4 报告

## Context（为什么推翻「promote 留人审」红线）

原红线（discovery/cli.py cmd_publish 注释）：「过拟合参数若直冲 ACTIVE 会绕开人审」。该红线在搜索产出低频、人可逐条复核时成立。长周期优化模式下 DRAFT 产出节奏（daemon 每夜 + 提案每日）超过人审消化能力（2026-08-16 已积压 4 条 DRAFT + 1 条悬置提案），红线反噬为「积压无人处置」。

用户决策（2026-08-16）：授权量化门槛自动 promote。

## Decision（受控替代，不是取消人审）

人审**前置**为门槛数值定稿（本 ADR 的七门常量），日常放行交量化闸。任何支撑件缺失自动回退红线：

1. **七门**（详见 `research/autopromote.py` 模块 docstring 表格）：G1 样本量 / G2 outer 改善（含**绝对不亏下限**）/ G3 风险不劣化（abs 幅度口径，C3 教训）/ G4 wf 跨年 / G5 邻域高原 / G6 DSR / G7 可信度前置（A3/A4 结论常量，**未定稿 → 拒跑**）。
2. **灰度两步**：过闸 → 旧 0.7/新 0.3 → 观察期 → confirm → 新 1.0 + 旧 archive。顺序红线：先降/归档旧再上新（资金守恒 validate_weight_sum，测试实弹抓出两次）。
3. **总开关** `AUTO_PROMOTE_ENABLED`（默认 false）：一键冻结回人审。
4. **审计**：operator=`autopromote:gate-v1`，门槛读数随播报/ROUND_LOG 留痕。
5. **播报**：钉钉逐门绿红 + kelly_hat env 行（衔接 ADR-14）+ 回滚 SOP。
6. **回滚**：`set-weight 旧 1.0 && archive 新` 两命令；极端用 `experiment rollback`。
7. **override 口**（review 注记）：CLI `--weight/--baseline` 为人工紧急干预保留，可偏离 0.3/冠军默认——资金守恒校验 + 总开关 + dry-run 默认三重兜底；使用须在 ROUND_LOG 记理由。

## 口径声明（R0 实证驱动的关键设计）

G2/G3 用 **replay 引擎口径**（PositionModel 组合约束=实盘同源）。R0 实证：ACTIVE 冠军 scan 口径 outer +18.4% vs replay 口径 **-23.9%@0bps**；DRAFT 47d350 scan +53.4% vs replay **-44.5%@0bps**——**符号反转**。kelly_metrics 搜索目标假设所有信号独立可下注（无视 max_positions=6 并发约束），replay 组合口径才是实盘真相。autopromote 的放行判据必须站实盘侧；G2 的绝对不亏下限（ann≥0）在当前基线为负的现实下是唯一诚实的「改善」定义。G4/G5/G6 保留 scan 口径作统计稳健性工具（折间稳定/邻域/多重比较的分辨率在 scan 口径下更高，且不直接决定资金）。

## 对抗性推演

- **为什么不全用 replay 口径**：wf 四折 × replay 每折 2 段 = 8 次全市场 replay（≈40min+），邻域 5 样本同理——门槛预算爆炸。scan 口径的统计工具（DSR/邻域）在「参数是否孤峰/是否多重比较虚高」问题上与资金口径正交，混用是预算下的诚实妥协（本文显式声明）。
- **为什么 G7 设计成常量而非每候选现算**：A3/A4 是**策略级/冠军级**结论（滑点敏感性、Kelly 收敛），不是候选级属性；每候选现算会把 30-45min 的门槛评估再翻倍。常量随报告定稿更新，修订走本 ADR 的修订记录。
- **为什么灰度不是直接 1.0**：七门全绿仍有未知未知（实盘成交/数据延迟）；0.3 权重的实盘暴露是最后一道「用真钱做 OOS」的检验，成本 30% 资金 × 观察期。

## 失效条件（红线恢复）

- G7 常量为 None（首轮报告未定稿）
- `AUTO_PROMOTE_ENABLED != true`
- 写库异常（audit 链断裂即视为未授权变更，回滚 + 人审）
- 季度人审：门槛数值本身每季度复核一次（防止门槛被特定候选形状「训练」出来）

## Consequences

- 正面：DRAFT 池有可自动化处置路径（过闸灰度/不过 discard），循环闭环不再依赖人盯。
- 负面/代价：门槛数值本身成为新的过拟合面（调门槛放行候选=变相人审漏洞）——靠季度人审 + ADR 修订记录对冲；评估成本 30-45min/候选。
- 后续：G7 常量已随 R0 报告定稿回填（`A3_SURVIVES_10BPS=False`、`A4_POS_KELLY_RATIO=0.667`，见 `research/autopromote.py` 常量注释）——R1 找到 replay 口径正边缘候选后按修订流程更新。

## 修订记录

- **R1（2026-08-16 晚）**：`A3_SURVIVES_10BPS` False→True。依据：R1-3 滑点探针对两个质量方向候选实证翻绿——`neckline_r1_touch3_20260816`（盈亏平衡 48.6bps，10bps 存活率 79%）与 `neckline_prop_20260816_3e383d`（51.0bps，89%，50bps 仍 +2.6%）。ACTIVE 基线仍薄边缘——常量语义收窄为「存在过滑点存活的候选方向」，非全参数集背书。同轮七门实弹（touch3）：G3/G4/G5/G6 绿，G1 红（inner n=73<100——质量收紧减样本与样本量门槛的结构性矛盾，**未调门槛**，留 ROUND_LOG 决策点）、G2 红（calmar 代理 1.375<1.5）、G7 随本修订翻绿。
