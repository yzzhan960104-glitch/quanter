# 决策记录：外部量化框架评估（2026-08-11）

> 本决策记录为会话产出转存（2026-08-12 落盘，用户拍板）。后续任何"引入外部框架"的提议先对照本记录；不再重复调研，本记录即唯一真相源。

## 背景

为提升颈线法量化策略，评估六个外部开源框架：**Qlib、vnpy/VeighNa、WonderTrader、Backtrader、VectorBT、NautilusTrader**。评估透镜：范式契合度 / 回测执行语义 / 参数探索方法论 / 数据层 / 实盘 / A股语义 / 依赖与工程文化 / Python 3.10 + Windows 硬约束。

## 决策总览

| # | 框架 | 决策 | 一句话依据 |
|---|---|---|---|
| 1 | Qlib | **不引入** | 因子打分范式与离散形态识别异构；回测/执行/数据层无法复用；依赖体量与文化冲突 |
| 2 | vnpy/VeighNa | **本体不引入**；**vnpy.alpha 记为未来第二策略家族候选** | 网关生态最强但迁移=推倒重建；vnpy.alpha 是 Qlib 价值的低门槛实现 |
| 3 | WonderTrader | **不引入** | 性能卖点在框架层，瓶颈在识别算法热路径（P1 自研已对症）；C++ 黑盒违背文化 |
| 4 | Backtrader | **不引入** | 原版停更（2018），fork 仅兼容维护；用死框架重写活系统 |
| 5 | VectorBT | **不引入** | 向量化思路与 P1 自研重合；numba 依赖；新功能优先进付费 PRO |
| 6 | NautilusTrader | **不引入** | 硬约束出局：Python 3.10 不在官方支持范围；无 A股生态 |

## 统一依据：四类不可迁移资产

任何框架迁移都会摧毁以下自研资产中最值钱的部分：
1. **回测执行模拟的实盘等价性**——`decide_exit` 回测/实盘单源 + 无前视纪律 + golden 回归守护（`test_scan_symbol_matches_strategy` 等）
2. **discovery 参数探索方法论**——DSR 多重检验 / inner-outer 信息隔离 / 断点续跑 / 跨夜收敛，全部框架只有简单网格
3. **已建成的实盘引擎**——T1 拆分完成的 phases 体系 + QMT 网关
4. **数据指纹/完整性体系**——snapshot hash / engine_hash / 无前视加载

## 逐项决策依据与借鉴点

**① Qlib（不引入）**：范式根本异构（因子打分+组合调仓 vs 离散形态+事件驱动执行）；TopkDropoutStrategy 无法表达挂单回踩/分级止盈；数据需迁移 bin 格式；约 40 万行平台与"极简无黑盒"冲突。
→ 借鉴：**point-in-time instrument 设计**（calendar/instrument 分离、防幸存者偏差）→ 已并入整体优化 spec P5 walk-forward 分折 universe 重建。

**② vnpy/VeighNa（本体不引入）**：网关生态（XTP/奇点/国君 hft/东证/东财）是唯一有增量价值的部分，但接入 = 抛弃自研 trading 引擎（T1 刚拆完）；CTA 模板体系偏期货风格，与 A股股票事件语义不匹配；VeighNa Studio 全家桶与极简文化冲突。
→ 未来候选：**vnpy.alpha**（4.0 新增：因子工程内置 Alpha158 + LightGBM/MLP 训练 + A股数据工作流，MIT，Python 3.10+ 兼容）。触发条件：P3 敏感性分析后仍决定策略多元化——届时优先于 Qlib 评估。

**③ WonderTrader（不引入）**：性能卖点在 C++ 回测引擎，但本项目 720s/组 瓶颈在识别算法 O(tops²) 纯 Python 热路径——框架迁移解决不了，P1 向量化自研已覆盖；C++/二进制黑盒违背文化。

**④ Backtrader（不引入）**：事件驱动范式可表达执行模拟，但原版 2018 年停更，backtrader2 fork 仅修兼容无新功能；等价于用死框架重写活系统。
→ 借鉴：**Analyzer/Observer 架构思想** → P3 报告扩展点参考。

**⑤ VectorBT（不引入）**：向量化思路与 P1 自研方向完全重合，而 P1 是自研版（numpy 预计算 + 等价性守护 + discovery 集成）；引入则 golden 测试/engine_hash 全失效；numba 依赖；开源版新功能优先进付费 vectorbtpro。
→ 借鉴：向量化思想 → P1 已覆盖。

**⑥ NautilusTrader（不引入）**：硬约束出局——官方支持 Python 3.11-3.13（本机 3.10 不在范围，全项目迁移不可接受）；无 A股生态；回测/实盘同构架构已有等价物（decide_exit 单源 + 回测 replay 与实盘同参数同源）。

## 调研事实快照（2026-08-11 联网核实）

- vnpy/VeighNa：4.0+（Studio 4.3/4.4），Python 3.10+，MIT，活跃（2026-05 portfolio_strategy 1.3.0）
- WonderTrader/wtpy：C++ 核心 + Python 3.8+，活跃（2026 年中），CTA/SEL/HFT/UFT 四引擎
- Backtrader：原版停更；backtrader2 fork 1.9.76.123 兼容维护
- VectorBT：开源 v1.1.0 维护中（py3.14/pandas3/Rust 引擎可选项），新功能优先 PRO 付费
- NautilusTrader：2025 版本 1.221，Python 3.11-3.13，Windows Server 2022+（wheel 仅标准精度）

## 对主线的影响

- **P0-P6 优化计划不变**，全部自研体系内完成（Qlib 借鉴点已并入 P5，见 `docs/superpowers/specs/2026-08-12-overall-optimization-design.md`）
- **未来候选注册**：vnpy.alpha 作为"第二策略家族"唯一年度候选，触发条件 = P3 数据驱动决策
- 框架评估事项**关闭**，不再重复调研
