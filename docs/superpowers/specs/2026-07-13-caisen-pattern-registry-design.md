# 蔡森形态注册表架构设计（方案 B·显式注册表）

- **日期**：2026-07-13
- **状态**：设计获批，待 writing-plans 出实现计划
- **范围**：B 段（蔡森策略质量）第一个子项目——形态注册表架构
- **下游**：B1（min_rr 定标）·B2（多头形态扩展：破底翻/破头锅等）均以此为基础
- **相关记忆**：[[quanter-caisen-pipeline]]、[[quanter-caisen-book-whitepaper]]

---

## 1. 背景与动机

蔡森形态学流水线（Phase 2 已交付）当前在 `caisen/patterns/screener.py::_screen_one` 中**硬编码**了 3 个形态的识别调用：

```
W 底(w_detect) → 头肩底(hs_detect, model_copy 覆写 hs_max_pattern_depth)
              → 收敛三角形底(tri_detect, enable 开关 + 覆写 triangle_max_pattern_depth + 额外 pattern_height)
```

每加一个形态都要在 `_screen_one` 改动 3~4 处（detect 调用、enable 判断、cfg 覆写、额外字段提取），违反开闭原则，且 cfg 覆写逻辑散落、难审计。

`config.py` 已预埋未来形态的开关但**未接入 screener**：`enable_pot_breakout`（破头锅，默认开）、`enable_bottom_flip`（破底翻，默认关）、`false_breakout_threshold/window`（假突破参数）—— 这些是 B2 多头形态扩展的占位。

**用户决策（2026-07-13 brainstorm）**：
- B 段顺序：**先建形态注册表架构 → 再 B1（诊断+定标）**；B2 多头扩展更后。
- **不做空头形态**（M头/头肩顶/上涨旗形/假突破等）—— A 股做空成本太高，实操性不足。
- 注册表用**方案 B（显式注册表）**，非装饰器自动扫描（后者违背 CLAUDE.md「显式至上、拒绝黑盒/魔法」）。

## 2. 目标与非目标

### 目标
1. 新建 `caisen/patterns/registry.py`，定义 `PatternMeta` + `PATTERNS`（显式 list，含现有 3 形态）。
2. `screener._screen_one` 步骤 4/5/5b（硬编码 3 detect + 3 套 cfg 覆写）收敛为**遍历 `PATTERNS` 的单段数据驱动逻辑**。
3. **纯重构、零行为变更**——同 cfg 同输入同输出（candidate schema / 排序 / 择优 全不变）。
4. 错误处理强化：在「单 symbol 异常隔离」之外新增「单形态异常隔离」。

### 非目标（本轮不做）
- 不实现任何新形态（破底翻/破头锅/假突破等留 B2）。
- 不改 `candidate` DataFrame schema、不改 `plan.py` 及任何 consumer。
- 不改 screener 对外签名（`screen` / `screen_with_pivots` / `_screen_one`）。
- 不触碰 `enable_pot_breakout` / `enable_bottom_flip` / `false_breakout_*` 的开关语义（保留预埋，待 B2 对应形态实现时再接入注册表）。

## 3. 现状核实（探索结论）

- **现有 3 形态接口高度一致**：`detect(close, pivots, high, low, volume, cfg) -> Result | None`，各 `Result` dataclass 都有 `neckline_price / bottom_price / depth / tension / is_valid`，仅三角形多 `edge_height`。
- **差异仅三点**，全部可数据化：
  1. enable 开关：三角形受 `enable_triangle_bottom` 控制（W底/头肩底总启用）。
  2. cfg 深度覆写：头肩底用 `hs_max_pattern_depth`、三角形用 `triangle_max_pattern_depth`（W底用默认 `max_pattern_depth`）。
  3. 额外输出字段：三角形向 candidate 输出 `pattern_height = res.edge_height`。
- `factors/registry.py`（det oify 记忆提及的 `@register_factor`）已随蔡森 Phase 1 清理删 factors 模块而移除，**无既有注册表参考**，本设计从头设计。

## 4. 架构（方案 B·显式注册表）

```
caisen/patterns/
├── registry.py          ← 新建：PatternMeta + PATTERNS（显式 list）
├── screener.py          ← _screen_one 改为遍历 PATTERNS
├── w_bottom.py          ← 零改（detect 签名不变）
├── head_shoulder.py     ← 零改
├── triangle_bottom.py   ← 零改
└── zigzag_causal.py / neckline.py ← 零改
```

形态差异从「散落在 screener 的代码分支」收敛为「registry.py 的声明式数据」。

## 5. 组件契约

### 5.1 `PatternMeta`（`caisen/patterns/registry.py`）

```python
from dataclasses import dataclass, field
from typing import Callable, Optional

@dataclass(frozen=True)
class PatternMeta:
    """形态注册元信息：声明 screener 如何调用本形态的 detect。

    把「enable 开关 / depth 覆写 / 额外输出字段」三类形态差异收敛为数据，
    让 screener 用统一的遍历逻辑处理所有形态（开闭原则：新形态只改 PATTERNS）。
    """
    name: str                                # pattern_type 标识（"w_bottom"），与 candidate.pattern_type / plan.py 消费一致
    detect: Callable                         # detect(close, pivots, high, low, volume, cfg) -> Result | None
    enable_field: Optional[str] = None       # cfg 开关字段名（None=总启用；如 "enable_triangle_bottom"）
    depth_override_field: Optional[str] = None  # cfg 深度覆写字段名（None=用 cfg.max_pattern_depth；如 "hs_max_pattern_depth"）
    extra_output: dict = field(default_factory=dict)  # candidate 额外字段名 -> Result 属性名（如 {"pattern_height": "edge_height"}）
```

### 5.2 `PATTERNS`（显式 list）

```python
from caisen.patterns.w_bottom import detect as w_detect
from caisen.patterns.head_shoulder import detect as hs_detect
from caisen.patterns.triangle_bottom import detect as tri_detect

PATTERNS: list[PatternMeta] = [
    PatternMeta(name="w_bottom", detect=w_detect),
    PatternMeta(name="head_shoulder", detect=hs_detect,
                depth_override_field="hs_max_pattern_depth"),
    PatternMeta(name="triangle_bottom", detect=tri_detect,
                enable_field="enable_triangle_bottom",
                depth_override_field="triangle_max_pattern_depth",
                extra_output={"pattern_height": "edge_height"}),
]
```

### 5.3 `screener._screen_one` 改造（步骤 4/5/5b → 一段遍历）

替换原硬编码的 w/hs/tri 三段，改为：

```python
from caisen.patterns.registry import PATTERNS

# —— 遍历注册表：对每个形态做 enable 过滤 + depth 覆写 + detect + 命中收集 ——
hits: list[tuple[PatternMeta, object]] = []
for meta in PATTERNS:
    # enable 开关过滤（None=总启用）
    if meta.enable_field is not None and not getattr(self.cfg, meta.enable_field, True):
        continue
    # depth 覆写：声明了 depth_override_field 则 model_copy 替换 max_pattern_depth（hs/tri 宽阈值）
    detect_cfg = self.cfg
    if meta.depth_override_field is not None:
        detect_cfg = self.cfg.model_copy(
            update={"max_pattern_depth": getattr(self.cfg, meta.depth_override_field)}
        )
    # 单形态异常隔离：一个形态 detect 抛错只跳过该形态，不影响同 symbol 其他形态
    try:
        res = meta.detect(close, pivots, high, low, volume, detect_cfg)
    except Exception as exc:
        _logger.debug("形态 %s detect 异常 symbol=%s：%s", meta.name, symbol, exc)
        continue
    if res is not None and res.is_valid:
        hits.append((meta, res))

if not hits:
    return None

# 多形态命中取 depth 更大者（满足空间更大；逻辑不变）
meta, res = max(hits, key=lambda h: h[1].depth)

# —— candidate 构造（通用字段 + extra_output 额外字段）——
candidate = {
    "symbol": symbol,
    "pattern_type": meta.name,
    "formed_at": formed_at,
    "breakout_price": float(close.iloc[-1]),
    "neckline_price": float(res.neckline_price),
    "bottom_price": float(res.bottom_price),
    "depth": float(res.depth),
    "tension": float(res.tension),
    "amount30d": amount30d,
    "is_valid": True,
}
for out_field, res_attr in meta.extra_output.items():
    candidate[out_field] = float(getattr(res, res_attr))
return candidate
```

> `formed_at` / `amount30d` / `breakout_price` 的计算逻辑与原实现完全一致（在遍历前/后不变），上述仅展示遍历段。

## 6. 数据流（不变）

```
price_data → liquidity_filter → micro_filter → causal_pivots(+ATR)
          → 【遍历 PATTERNS：enable 过滤 → depth 覆写 → detect → 命中收集】
          → 多形态 depth 择优 → candidate(含 extra_output) → amount30d 排序输出
```

`screen` / `screen_with_pivots`（pivots 复用性能路径）都走改造后的 `_screen_one`，两条路径行为一致。

## 7. 错误处理（强化）

- **现有**：`screen` / `screen_with_pivots` 外层 try/except 隔离「单 symbol 异常」（一个标的抛错跳过，不影响其他标的）。
- **新增**：`_screen_one` 遍历内每个 `meta.detect` 包 try/except，隔离「单形态异常」——一个形态 detect 抛错（如数据脏值致某形态内部 KeyError）只跳过该形态，同 symbol 的其他形态仍正常识别。粒度更细，诊断更准（debug 日志标形态名）。
- **防御**：`getattr(self.cfg, meta.enable_field, True)`、`getattr(self.cfg, meta.depth_override_field)` —— 字段缺失时 enable 默认 True（放行）、depth_override 缺失则不覆写（用 max_pattern_depth），不抛。

## 8. 测试策略

### 8.1 新增 `tests/caisen/test_registry.py`
- `PATTERNS` 含且仅含 3 项（w_bottom/head_shoulder/triangle_bottom）。
- 各 `PatternMeta` 字段正确：
  - w_bottom：`enable_field=None, depth_override_field=None, extra_output={}`
  - head_shoulder：`depth_override_field="hs_max_pattern_depth"`
  - triangle_bottom：`enable_field="enable_triangle_bottom", depth_override_field="triangle_max_pattern_depth", extra_output={"pattern_height":"edge_height"}`

### 8.2 `tests/caisen/test_screener.py`（现有）—— **零回归验收线**
现有全部用例必须全过（同 cfg 同输入同输出）。这是纯重构的核心验收。

### 8.3 新增注册表驱动测试（`test_screener.py` 追加）
注册一个「假形态」到临时 PATTERNS（monkeypatch），验证 screener：
- 调用了它（detect 被调用）；
- `enable_field` 指向的 cfg 开关=False 时跳过它；
- `depth_override_field` 生效（detect 收到的 cfg.max_pattern_depth == 覆写值）；
- `extra_output` 提取正确（candidate 含对应字段）；
- 假形态 detect 抛异常时被隔离，不影响其他形态。

## 9. 兼容性与回归保护

- **screener 对外签名不变**：`screen(price_data, date)` / `screen_with_pivots(price_data, pivots_map, hv_map, date)` / `_screen_one(symbol, df, pivots, hv_win)`。
- **candidate DataFrame schema 不变**：`symbol/pattern_type/formed_at/breakout_price/neckline_price/bottom_price/depth/tension/amount30d/is_valid`(+`pattern_height` for triangle)。`screen` 空命中返回的列名 list 不变。
- **consumer 零改**：`plan.py`（按 pattern_type 消费 neckline/bottom/pattern_height）、`backtest_replay.py`、`caisen_service`、前端候选表全部不动。
- **回归保护**：现有 run_checks 5 gate（644 后端测试 + 前端类型/单测）必须全绿；screener/plan/replay 全链路测试零失败。

## 10. 扩展点（为 B2 铺路，本轮不做）

未来加破底翻（多头形态）：
1. 写 `caisen/patterns/bottom_flip.py` + `detect(...)`；
2. `registry.py` 加一行 `PatternMeta("bottom_flip", bf_detect, enable_field="enable_bottom_flip", ...)`；
3. screener **零改**，自动遍历到。

破头锅（`enable_pot_breakout`）/假突破（`false_breakout_*`）同理。本轮注册表只搬现有 3 形态，不含未实现形态。

## 11. 验收标准

- [ ] `caisen/patterns/registry.py` 新建，含 `PatternMeta` + `PATTERNS`（3 项）。
- [ ] `screener._screen_one` 改为遍历 `PATTERNS`，原硬编码 w/hs/tri 三段移除。
- [ ] `tests/caisen/test_registry.py` 新建并通过。
- [ ] `tests/caisen/test_screener.py` 现有用例**全过（零回归）**。
- [ ] 注册表驱动测试（enable/depth_override/extra_output/异常隔离）通过。
- [ ] `python scripts/run_checks.py` 5 gate 全绿（后端 644+ 测试 0 新增失败）。
- [ ] screener 对外签名 / candidate schema / consumer 零改（代码审查确认）。

## 12. Follow-up（后续周期，非本 spec 范围）

- **B1 min_rr 定标**：诊断近年 W底 avg_rr=-0.65 亏损归因 → screener 增量 pivot 性能优化（全市场 replay 可行）→ entry min_rr 多阈值参数扫描。独立 spec。
- **B2 多头形态扩展**：破底翻 / 破头锅 / 破底翻W底 / 下偏镰形等，经本注册表接入。每形态独立 spec（含方法学校准 + 识别算法 + 测试）。
- **空头形态**：用户已明确不做（A 股做空成本太高），白皮书招 5/6/7/8/9/11 排除。
