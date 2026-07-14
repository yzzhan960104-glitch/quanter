# 参数瘦身 + 分层展示设计（StrategyConfig 死码清理 + /lab 分层）

> 2026-07-14 · brainstorm 阶段产出 · 用户已认可，授权连续推进（免人工确认）

## 1. 背景与定位

Spec 2 的 /lab Parameter Lab 把 `StrategyConfig` 全部 39 个字段经 `paramMeta.ts` 中文映射铺成参数表单后，研究员反馈：**参数太多、多数非形态核心**，调参视野被风控/执行类参数干扰。同时 codegraph 审计发现 **6 个字段是死代码/未实现预留**（全仓库零消费）。

本 spec 做两件事：
1. **删 6 个死参数**（YAGNI：预留开关不保留，将来实现对应形态时再加）。
2. **/lab 参数表单分层展示**：形态核心 4 组默认展开，计划/执行/风控 3 组折叠在「显示高级参数」开关后（仅控可见性，保留全调参能力）。

**不引入后端"生效集"语义**（与既有 boolean 开关重叠、numeric 参数无法干净禁用）——纯清理 + 展示分层。

## 2. 参数必要性审计结论（codegraph 全仓库属性访问扫描）

按 `.字段` 属性访问次数 + 消费文件分类 39 字段（删 6 死码后剩 33：形态核心 20 / 高级 13）：

| 分类 | 字段 | 处置 |
|---|---|---|
| 🔴 死码/未实现（attr=0，零消费，registry.py:45 自承"未实现"） | `symmetry_tolerance` `pattern_width_bonus` `false_breakout_threshold` `false_breakout_window` `enable_pot_breakout` `enable_bottom_flip` | **删** |
| 🟢 形态核心（pattern detectors 直接消费） | `min_pattern_bars` `max_pattern_bars` `zigzag_threshold_atr` `min_pattern_depth` `max_pattern_depth` `hs_max_pattern_depth` `w_price_tolerance` `right_vol_shrink` `breakout_vol_multiplier` `neckline_height_multiple` `abc_wave_detect` `right_above_left` `ma26w_filter` `ma26w_window` `pattern_tension_ratio` `confirm_bars` `triangle_breakout_min` `triangle_breakout_max` `triangle_max_pattern_depth` `enable_triangle_bottom` | 保留·默认展开 |
| 🔵 计划生成/离场执行（plan/execution） | `pullback_window_bars` `pullback_max_pct` `stop_loss_atr_buffer` `min_rr_ratio` `max_holding_bars` `timeout_exit_threshold` `trailing_activation_bars` `trailing_to_breakeven` | 保留·高级折叠 |
| 🟡 风控过滤（risk/screener） | `liquidity_min_amount` `hv_window` `hv_max_quantile` `max_position_pct` `macro_regime_veto` | 保留·高级折叠 |

> 注：`enable_triangle_bottom` **真生效**（registry.py:57 `enable_field` gate 三角形态，test_screener 验证 False 关闭）——保留。`triangle_max_pattern_depth` 经 registry `model_copy` 覆写间接消费——保留。

**删除安全性**：6 个待删字段全仓库（caisen/server/scripts/tests）零属性访问、零测试构造引用（除 registry.py:45 注释）；`StrategyConfig` 为 pydantic BaseModel，删字段不影响 model_dump/CLI/归档（无消费方）。

## 3. 已确认决策

| 决策点 | 选定 | 理由 |
|---|---|---|
| 开关语义 | UI 展示分层（非后端生效集） | 用户选 C：纯清理+分层，不引入语义糊的 enabled 元信息 |
| 死参数处置 | 删除（非标记 reserved） | YAGNI；registry.py:45 注释一并清；将来实现形态时重新加 |
| 分层粒度 | group→tier 映射（非逐字段元数据） | tier 是 group 的属性：形态核心组 vs 高级组，无需逐字段加 tier |
| 高级参数可调性 | 折叠但可编辑可提交 | 开关仅控可见性，保留全调参能力 |
| paramMeta 同步 | 双向守护 test_param_meta_sync 强制 | config 删字段→PARAM_META 必须同步删，否则 orphan 报错 |

## 4. 改动清单

### 4.1 后端删字段（`caisen/config.py`）
删除 6 个 Field 定义：`symmetry_tolerance`、`pattern_width_bonus`、`false_breakout_threshold`、`false_breakout_window`、`enable_pot_breakout`、`enable_bottom_flip`。
清理 `caisen/patterns/registry.py:45` 那条"未实现的 enable_pot_breakout/enable_bottom_flip/false_breakout_* 开关待对应形态…"注释。

### 4.2 paramMeta 同步 + 分层（`web/src/components/lab/paramMeta.ts`）
- 删 6 条 PARAM_META 条目（与 config 字段集重新双向匹配）。
- **重分组 1 个字段**：`confirm_bars` 当前在「风控」组，但它是 ZigZag pivot 确认窗（被 `zigzag_causal`/`screener` 消费，形态核心）——重分到「蔡森方法学」组，使 group→tier 分类与消费方一致（否则分层会把它误归高级）。sync 守护只校验键集、不校验 group，故安全。
- 新增分层常量：
  ```ts
  export const CORE_GROUPS: ParamGroup[] = ['时间跨度', '空间高度', '量价配合', '蔡森方法学']
  // 高级 = PARAM_GROUPS \ CORE_GROUPS = ['交易执行', '时间止损', '风控']
  export function isCoreGroup(g: ParamGroup): boolean { return CORE_GROUPS.includes(g) }
  ```

### 4.3 前端分层展示
**`NewReplayDrawer.vue`**：
- 新增 `showAdvanced` ref（默认 `false`）+ 顶部 `el-switch`「显示高级参数（计划/执行/风控）」。
- 分组渲染：形态核心组（isCoreGroup=true）始终渲染且默认展开；高级组仅在 `showAdvanced=true` 时渲染。
- 高级参数仍绑定 cfg、可编辑、进 cfg_override 提交（仅可见性受开关控制）。

**`ParamLabView.vue` 参数详情面板（只读）**：
- 同 `showAdvanced` 逻辑：形态核心组常显；高级组折叠在开关后。

### 4.4 测试
- `tests/test_param_meta_sync.py`：删字段后自动重新双向匹配（守护自带，无需改测试逻辑）。
- `web/src/components/lab/NewReplayDrawer.spec.ts`：加用例「默认仅渲染形态核心组（4 组标题在、高级 3 组标题不在）；切换 showAdvanced 后高级组出现」。
- 既有 `caisen/config.py` 相关测试（StrategyConfig 构造、replay、CLI）不受影响——6 字段零引用。

## 5. 边界与风险

- **删字段破坏向后兼容？** 否——6 字段零消费，无 cfg_override/model_dump/CLI/归档引用。老 replay_runs JSON 归档里即使快照含这些字段，加载时被忽略（无消费方读它）。
- **paramMeta 漏删导致 sync 报错？** 正是守护目的——`test_param_meta_sync.py` orphan 分支会抓 PARAM_META 残留键，TDD 强制同步。
- **高级参数折叠后用户找不到？** 开关显式标注「计划/执行/风控」，且详情面板与抽屉一致；不损失调参能力。
- **enable_triangle_bottom 误删？** 审计已区分——它真 gate 三角形态（registry + test_screener），保留。

## 6. 测试策略

- 后端：`pytest tests/test_param_meta_sync.py -q`（删字段后双向匹配）；全量 `pytest -q` 不回归（700+，6 字段零引用）。
- 前端：`cd web && npm run typecheck && npm run test`（typecheck 净 + vitest 含新 drawer 用例）。
- 免 E2E（本改动无新交互链路，drawer 开关由组件单测覆盖；/lab E2E 既有用例不回归）。

## 7. 未确认项

无。用户已授权连续推进（写 plan → subagent 开发，免人工确认）。

---

状态：用户已认可（2026-07-14）→ commit → 转 writing-plans → subagent 开发（全程免确认）。
