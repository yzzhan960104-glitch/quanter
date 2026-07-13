# 蔡森形态注册表架构 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `screener._screen_one` 硬编码的 3 形态识别改为遍历显式注册表 `PATTERNS`，纯重构零行为变更，为 B2 多头形态扩展铺路。

**Architecture:** 新建 `caisen/patterns/registry.py` 定义 `PatternMeta`（声明 enable/depth覆写/额外输出）+ `PATTERNS`（显式 list，3 项）。`screener._screen_one` 步骤 4/5/5b 三段硬编码 detect 收敛为遍历 `PATTERNS` 的单段数据驱动逻辑，并新增单形态异常隔离。形态模块（w_bottom/head_shoulder/triangle_bottom）零改。

**Tech Stack:** Python 3.10（`.venv310`）、pandas、pydantic（StrategyConfig.model_copy）、pytest。全中文注释（CLAUDE.md）。

**Spec:** `docs/superpowers/specs/2026-07-13-caisen-pattern-registry-design.md`

## Global Constraints

- Python 解释器固定 `.venv310/Scripts/python`（vnemttrader 绑 python310.dll，全项目测试用此 venv）。
- 所有新增/修改代码配像素级中文注释（CLAUDE.md「极简 + 显式 + Why」）。
- **screener 对外签名不变**：`screen(price_data, date)` / `screen_with_pivots(price_data, pivots_map, hv_map, date)` / `_screen_one(symbol, df, pivots, hv_win)`。
- **candidate DataFrame schema 不变**：`symbol/pattern_type/formed_at/breakout_price/neckline_price/bottom_price/depth/tension/amount30d/is_valid`（+`pattern_height` for triangle）。空命中返回的列名 list 不变。
- **consumer 零改**：`plan.py` / `backtest_replay.py` / `caisen_service` / 前端不动。
- 回归线：`python scripts/run_checks.py` 5 gate 全绿（后端 644+ 测试 0 新增失败）。
- 每步 commit message 结尾 `Co-Authored-By: Claude <noreply@anthropic.com>`。

---

## 文件结构

- **Create** `caisen/patterns/registry.py` —— `PatternMeta` dataclass + `PATTERNS` 显式 list（3 项）。单一职责：声明 screener 如何调用每个形态。
- **Create** `tests/caisen/test_registry.py` —— `PATTERNS` 内容 + `PatternMeta` 字段契约测试。
- **Modify** `caisen/patterns/screener.py` —— `_screen_one` 步骤 4/5/5b → 遍历 `PATTERNS`；顶部 import 调整（加 registry，移除直接 detect import）。
- **Modify** `tests/caisen/test_screener.py` —— 追加 4 个注册表驱动测试（enable/depth_override/extra_output/异常隔离）。

形态模块 `w_bottom.py` / `head_shoulder.py` / `triangle_bottom.py` **零改**（detect 签名不变，只是被 PATTERNS 引用）。

---

## Task 1: 形态注册表 `registry.py` + 契约测试

**Files:**
- Create: `caisen/patterns/registry.py`
- Test: `tests/caisen/test_registry.py`

**Interfaces:**
- Produces: `PatternMeta(name: str, detect: Callable, enable_field: str|None=None, depth_override_field: str|None=None, extra_output: dict=field(default_factory=dict))`（frozen dataclass）；`PATTERNS: list[PatternMeta]`（含 w_bottom/head_shoulder/triangle_bottom 三项）。Task 2 的 screener 经 `from caisen.patterns.registry import PATTERNS, PatternMeta` 消费。

- [ ] **Step 1: 写失败测试 `tests/caisen/test_registry.py`**

```python
# -*- coding: utf-8 -*-
"""形态注册表契约测试：PATTERNS 内容 + PatternMeta 字段（方案B 显式注册表）。

物理意图：锁死注册表的声明式契约——screener 据此数据驱动遍历，故 PATTERNS 的
每一项字段（name/detect/enable_field/depth_override_field/extra_output）必须精确
对应 screener 的调用逻辑，任一字段漂移都会让对应形态被误启用/误覆写/漏输出。
"""
import dataclasses

from caisen.patterns.registry import PATTERNS, PatternMeta
from caisen.patterns.w_bottom import detect as w_detect
from caisen.patterns.head_shoulder import detect as hs_detect
from caisen.patterns.triangle_bottom import detect as tri_detect


def test_patterns_contains_three_builtins():
    """PATTERNS 含且仅含现有 3 形态（未实现形态不入注册表，待 B2 实现 + 注册）。"""
    names = {m.name for m in PATTERNS}
    assert names == {"w_bottom", "head_shoulder", "triangle_bottom"}


def test_w_bottom_meta_defaults():
    """W 底：基线形态——无 enable 开关、无 depth 覆写、无额外输出。"""
    m = next(m for m in PATTERNS if m.name == "w_bottom")
    assert m.detect is w_detect
    assert m.enable_field is None
    assert m.depth_override_field is None
    assert m.extra_output == {}


def test_head_shoulder_meta_depth_override():
    """头肩底：depth 覆写 hs_max_pattern_depth（头部幅度天然更深，需宽阈值）。"""
    m = next(m for m in PATTERNS if m.name == "head_shoulder")
    assert m.detect is hs_detect
    assert m.enable_field is None              # 总启用（多头基础形态）
    assert m.depth_override_field == "hs_max_pattern_depth"
    assert m.extra_output == {}


def test_triangle_bottom_meta_full():
    """收敛三角形底：enable 开关 + depth 覆写 + 额外 pattern_height=edge_height。"""
    m = next(m for m in PATTERNS if m.name == "triangle_bottom")
    assert m.detect is tri_detect
    assert m.enable_field == "enable_triangle_bottom"
    assert m.depth_override_field == "triangle_max_pattern_depth"
    assert m.extra_output == {"pattern_height": "edge_height"}


def test_pattern_meta_is_frozen():
    """PatternMeta 不可变（防运行时误改注册项导致全市场扫描行为漂移）。"""
    assert dataclasses.is_dataclass(PatternMeta)
    m = PatternMeta(name="x", detect=lambda *a, **k: None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.name = "y"


# pytest import（test_pattern_meta_is_frozen 用 pytest.raises）
import pytest
```

- [ ] **Step 2: 跑测试验证失败（模块未建）**

Run: `.venv310/Scripts/python -m pytest tests/caisen/test_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'caisen.patterns.registry'`（collection 阶段 import 失败）。

- [ ] **Step 3: 写 `caisen/patterns/registry.py` 最小实现**

```python
# -*- coding: utf-8 -*-
"""蔡森形态注册表（方案B·显式注册表）。

物理定位（CLAUDE.md 极简 + 显式至上 + 拒绝黑盒）：
    把 screener 原硬编码的「enable 开关 / depth 覆写 / 额外输出字段」三类形态差异
    收敛为声明式数据（PatternMeta），screener 用统一遍历逻辑处理所有形态。
    新形态扩展（B2：破底翻/破头锅等）只在本文件 PATTERNS 加一行，screener 零改。

为何不用装饰器 + importlib 自动扫描（方案A）：
    自动扫描是「魔法」——形态清单不直观、调试时来源难追，违背「显式至上、拒绝黑盒」。
    显式 list 的成本（加形态改 2 行）本身是合理的显式工程动作，且形态清单一目了然。

PatternMeta 字段物理意图：
    name:                 pattern_type 标识，与 candidate.pattern_type / plan.py 消费一致；
    detect:               detect(close, pivots, high, low, volume, cfg) -> Result | None；
    enable_field:         cfg 开关字段名（None=总启用；如 "enable_triangle_bottom"）；
    depth_override_field: cfg 深度覆写字段名（None=用 cfg.max_pattern_depth；
                          如 "hs_max_pattern_depth"——头部/边长幅度天然深于 W底颈线高度比，
                          需分类型宽阈值，screener model_copy 替换 max_pattern_depth）；
    extra_output:         candidate 额外字段名 -> Result 属性名
                          （如 triangle: {"pattern_height": "edge_height"}，供 plan.py 满足点用）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from caisen.patterns.w_bottom import detect as w_detect
from caisen.patterns.head_shoulder import detect as hs_detect
from caisen.patterns.triangle_bottom import detect as tri_detect


@dataclass(frozen=True)
class PatternMeta:
    """形态注册元信息：声明 screener 如何调用本形态的 detect（不可变值对象）。"""

    name: str
    detect: Callable
    enable_field: Optional[str] = None
    depth_override_field: Optional[str] = None
    extra_output: dict = field(default_factory=dict)


# 显式注册表：现有 3 形态。新形态（B2 破底翻等）在此追加一行即可，screener 零改。
# 未实现的 enable_pot_breakout/enable_bottom_flip/false_breakout_* 开关待对应形态
# 实现后再入此表（本轮只搬现有 3 形态，不含未实现形态）。
PATTERNS: list[PatternMeta] = [
    PatternMeta(name="w_bottom", detect=w_detect),
    PatternMeta(
        name="head_shoulder",
        detect=hs_detect,
        depth_override_field="hs_max_pattern_depth",
    ),
    PatternMeta(
        name="triangle_bottom",
        detect=tri_detect,
        enable_field="enable_triangle_bottom",
        depth_override_field="triangle_max_pattern_depth",
        extra_output={"pattern_height": "edge_height"},
    ),
]
```

- [ ] **Step 4: 跑测试验证通过**

Run: `.venv310/Scripts/python -m pytest tests/caisen/test_registry.py -v`
Expected: PASS — 5 用例全绿（PATTERNS 3 项 + 字段契约 + frozen）。

- [ ] **Step 5: Commit**

```bash
git add caisen/patterns/registry.py tests/caisen/test_registry.py
git commit -m "feat(caisen): 形态注册表 registry.py（方案B显式注册表，3形态）

PatternMeta 声明 enable/depth覆写/额外输出；PATTERNS 显式 list。
为 screener 数据驱动遍历铺路（Task 2）。零行为变更。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2: screener `_screen_one` 改遍历 + 注册表驱动测试

**Files:**
- Modify: `caisen/patterns/screener.py`（顶部 import + `_screen_one` 步骤 4/5/5b/6/candidate 段）
- Test: `tests/caisen/test_screener.py`（追加 4 个驱动测试）

**Interfaces:**
- Consumes: `from caisen.patterns.registry import PATTERNS, PatternMeta`（Task 1 产出）。
- Produces: screener 对外行为不变（同 cfg 同输入同输出）；新增单形态异常隔离。

- [ ] **Step 1: 追加 4 个注册表驱动测试到 `tests/caisen/test_screener.py` 文件末尾**

```python
# ---------------------------------------------------------------------------
# 注册表驱动测试（方案B）：验证 screener 经 PATTERNS 遍历的 enable/depth/extra/异常隔离机制
# ---------------------------------------------------------------------------
def test_registry_driven_enable_filter(monkeypatch):
    """注册表驱动：enable_field 指向的 cfg 开关=False 时该形态被跳过（detect 不调用）。"""
    from caisen.patterns.registry import PatternMeta
    from caisen.patterns import screener as screener_mod

    called = []

    def fake_detect(close, pivots, high, low, volume, cfg):
        called.append("detect")
        return None   # 返 None 不命中，只验证是否被调用

    fake_meta = PatternMeta(name="fake", detect=fake_detect,
                            enable_field="enable_triangle_bottom")
    monkeypatch.setattr(screener_mod, "PATTERNS", [fake_meta])

    close, high, low, vol = _build_standard_w_bottom()
    df = _mk_price_df(close, high, low, vol, amount_per_bar=3e8)

    # enable=False → 假形态被跳过
    cfg_off = _mk_cfg(enable_triangle_bottom=False)
    sc_off = PatternScreener(cfg_off, RiskManager(cfg_off))
    sc_off.screen({"X": df}, date=None)
    assert called == [], "enable_field=False 时形态应被跳过，detect 不应被调用"

    # 对照：enable=True → detect 被调
    called.clear()
    cfg_on = _mk_cfg(enable_triangle_bottom=True)
    sc_on = PatternScreener(cfg_on, RiskManager(cfg_on))
    sc_on.screen({"X": df}, date=None)
    assert called == ["detect"], "enable_field=True 时 detect 应被调用"


def test_registry_driven_depth_override(monkeypatch):
    """注册表驱动：depth_override_field 声明的 cfg 字段值覆写 detect 收到的 max_pattern_depth。"""
    from caisen.patterns.registry import PatternMeta
    from caisen.patterns import screener as screener_mod

    received = []

    def fake_detect(close, pivots, high, low, volume, cfg):
        received.append(cfg.max_pattern_depth)
        return None

    fake_meta = PatternMeta(name="fake", detect=fake_detect,
                            depth_override_field="hs_max_pattern_depth")
    monkeypatch.setattr(screener_mod, "PATTERNS", [fake_meta])

    cfg = _mk_cfg(hs_max_pattern_depth=0.88)   # 显式设覆写值
    sc = PatternScreener(cfg, RiskManager(cfg))
    close, high, low, vol = _build_standard_w_bottom()
    df = _mk_price_df(close, high, low, vol, amount_per_bar=3e8)
    sc.screen({"X": df}, date=None)
    # detect 收到的 cfg.max_pattern_depth 应被覆写为 hs_max_pattern_depth=0.88
    assert received == [0.88]


def test_registry_driven_extra_output(monkeypatch):
    """注册表驱动：extra_output 声明的字段从 Result 属性提取进 candidate。"""
    from caisen.patterns.registry import PatternMeta
    from caisen.patterns import screener as screener_mod

    class _FakeResult:
        # 模拟一个命中形态的 Result（含通用字段 + 自定义属性）
        is_valid = True
        neckline_price = 11.0
        bottom_price = 7.5
        depth = 0.47
        tension = 0.5
        custom_metric = 42.0

    def fake_detect(close, pivots, high, low, volume, cfg):
        return _FakeResult()

    fake_meta = PatternMeta(name="fake", detect=fake_detect,
                            extra_output={"custom_field": "custom_metric"})
    monkeypatch.setattr(screener_mod, "PATTERNS", [fake_meta])

    cfg = _mk_cfg()
    sc = PatternScreener(cfg, RiskManager(cfg))
    close, high, low, vol = _build_standard_w_bottom()
    df = _mk_price_df(close, high, low, vol, amount_per_bar=3e8)
    result = sc.screen({"X": df}, date=None)
    assert len(result) == 1
    assert result.iloc[0]["pattern_type"] == "fake"
    assert result.iloc[0]["custom_field"] == pytest.approx(42.0)


def test_registry_driven_isolates_per_pattern_exception(monkeypatch):
    """注册表驱动：某形态 detect 抛异常时被隔离，不影响同 symbol 其他形态。"""
    from caisen.patterns.registry import PatternMeta
    from caisen.patterns import screener as screener_mod
    from caisen.patterns.w_bottom import detect as w_detect

    def boom_detect(close, pivots, high, low, volume, cfg):
        raise KeyError("脏值致形态内部异常")

    # boom（抛异常）+ 真实 w_bottom 并存：boom 被隔离，w_bottom 仍命中
    monkeypatch.setattr(screener_mod, "PATTERNS", [
        PatternMeta(name="boom", detect=boom_detect),
        PatternMeta(name="w_bottom", detect=w_detect),
    ])

    cfg = _mk_cfg()
    sc = PatternScreener(cfg, RiskManager(cfg))
    close, high, low, vol = _build_standard_w_bottom()
    df = _mk_price_df(close, high, low, vol, amount_per_bar=3e8)
    result = sc.screen({"X": df}, date=None)
    assert len(result) == 1, "boom 异常应被隔离，w_bottom 仍应命中"
    assert result.iloc[0]["pattern_type"] == "w_bottom"
```

- [ ] **Step 2: 跑测试验证 4 个新测试失败（screener 还硬编码，未遍历 PATTERNS）**

Run: `.venv310/Scripts/python -m pytest tests/caisen/test_screener.py -v -k registry_driven`
Expected: 4 个新测试 FAIL（`screener_mod.PATTERNS` 不存在→AttributeError，或假形态未被调用→assert 失败）；现有 9 个测试仍 PASS（未被 -k 选中）。

Run: `.venv310/Scripts/python -m pytest tests/caisen/test_screener.py -v`
Expected: 现有 9 个 PASS，4 个新 `registry_driven` FAIL。

- [ ] **Step 3: 改 `caisen/patterns/screener.py`——import 调整**

替换顶部 import 区（原直接 import 三个 detect）为从 registry 消费：

old（screener.py 顶部 import 段）:
```python
from caisen.patterns.zigzag_causal import causal_pivots, compute_atr
from caisen.patterns.w_bottom import detect as w_detect
from caisen.patterns.head_shoulder import detect as hs_detect
from caisen.patterns.triangle_bottom import detect as tri_detect
```

new:
```python
from caisen.patterns.zigzag_causal import causal_pivots, compute_atr
from caisen.patterns.registry import PATTERNS, PatternMeta
```

> 说明：screener 不再直接调 w_detect/hs_detect/tri_detect，改遍历 PATTERNS（每项 meta.detect 即对应 detect 函数）。三个形态模块零改。

- [ ] **Step 4: 改 `caisen/patterns/screener.py`——`_screen_one` 步骤 4/5/5b/6/candidate 段改为遍历**

替换 `_screen_one` 中从「步骤 4. W 底识别」到 `return candidate` 的整段（原硬编码 w/hs/tri 三段 detect + 命中收集 + candidate 构造）为下面的遍历段。**步骤 0（列完整性）/1（流动性）/2（micro_filter）/3（causal_pivots）保留不变**：

```python
        # —— 4. 遍历形态注册表：enable 过滤 + depth 覆写 + detect + 命中收集 ——
        # 注册表驱动（方案B）：把原硬编码的 w/hs/tri 三段 detect + 三套 cfg 覆写收敛为
        # 数据驱动的遍历。新形态只改 caisen/patterns/registry.py 的 PATTERNS，screener 零改。
        hits: list[tuple[PatternMeta, object]] = []
        for meta in PATTERNS:
            # enable 开关过滤（meta.enable_field=None 表示总启用，如 W底/头肩底）
            if meta.enable_field is not None and not getattr(self.cfg, meta.enable_field, True):
                continue
            # depth 覆写：声明了 depth_override_field 则 model_copy 替换 max_pattern_depth
            # （hs 头部幅度 / tri 边长比天然深于 W底颈线高度比，需分类型宽阈值，否则误否决）。
            detect_cfg = self.cfg
            if meta.depth_override_field is not None:
                detect_cfg = self.cfg.model_copy(
                    update={"max_pattern_depth": getattr(self.cfg, meta.depth_override_field)}
                )
            # 单形态异常隔离：一个形态 detect 抛错只跳过该形态，不影响同 symbol 其他形态
            # （粒度细于外层单 symbol 异常隔离，诊断更准——debug 日志标形态名）。
            try:
                res = meta.detect(close, pivots, high, low, volume, detect_cfg)
            except Exception as exc:
                _logger.debug("形态 %s detect 异常 symbol=%s：%s", meta.name, symbol, exc)
                continue
            if res is not None and res.is_valid:
                hits.append((meta, res))

        if not hits:
            return None   # 所有形态均未命中，跳过

        # 多形态命中：取 depth 更大者（满足空间更大；逻辑与原实现一致）
        meta, res = max(hits, key=lambda h: h[1].depth)

        # —— amount30d：近 30 日均成交额（排序键，与流动性过滤同源数据）——
        amount30d = float(df["amount"].tail(30).mean())

        # —— formed_at：形态形成日 = DataFrame index 末值（pivot 末点的交易日）——
        # 用 index 末值而非 pivot 末点 idx，因为 causal_pivots 末尾 confirm_bars 内的 pivot
        # 被丢弃，但形态的"当前形成时点"就是数据末根（T 日收盘看 T-1 及之前，合法）。
        formed_at = df.index[-1]

        # —— breakout_price：颈线突破价（统一用 close.iloc[-1] 代表当前突破状态）——
        # 各形态突破 pivot idx 可能不等于末根（causal_pivots 末尾 confirm_bars 丢弃），
        # 下游 plan.py 计算满足点时用 res.neckline_price + H 重新精算，此处仅排序展示。
        breakout_price = float(close.iloc[-1])

        # —— candidate 构造：通用字段 + extra_output 声明的额外字段 ——
        candidate = {
            "symbol": symbol,
            "pattern_type": meta.name,
            "formed_at": formed_at,
            "breakout_price": breakout_price,
            "neckline_price": float(res.neckline_price),
            "bottom_price": float(res.bottom_price),   # 谷底价由形态直接给出（Bug3 契约）
            "depth": float(res.depth),
            "tension": float(res.tension),
            "amount30d": amount30d,
            "is_valid": True,
        }
        # extra_output：candidate 字段名 → Result 属性名（如 triangle: pattern_height=edge_height，
        # 供 plan.py 满足点计算；W底/头肩底 extra_output={} 无额外字段）。
        for out_field, res_attr in meta.extra_output.items():
            candidate[out_field] = float(getattr(res, res_attr))
        return candidate
```

> 同时更新 `_screen_one` 与 `screen`/`screen_with_pivots` 的 docstring 中「w_bottom/head_shoulder/triangle_bottom」相关的步骤描述，改为「遍历 PATTERNS 注册表」（保持文档与实现一致；空命中返回的列名 list 不变）。

- [ ] **Step 5: 跑 screener 全量测试验证（4 新测试 PASS + 9 现有测试零回归）**

Run: `.venv310/Scripts/python -m pytest tests/caisen/test_screener.py -v`
Expected: PASS — 13 用例全绿（9 现有 + 4 新 `registry_driven`）。任一现有用例失败=重构引入回归，必须修复后再继续。

- [ ] **Step 6: 跑 run_checks 5 gate 全量验证（确认全链路零回归）**

Run: `.venv310/Scripts/python scripts/run_checks.py`
Expected: 5 gate 全绿（① 端口 ② 契约 ③ 后端单测 644+ ④ 前端类型 ⑤ 前端单测）。后端测试数应 ≥ 原 644（含新增 test_registry 5 + test_screener 4 = 9 个新测试），0 新增失败。

- [ ] **Step 7: Commit**

```bash
git add caisen/patterns/screener.py tests/caisen/test_screener.py
git commit -m "refactor(caisen): screener _screen_one 改遍历形态注册表

硬编码 w/hs/tri 三段 detect → 遍历 PATTERNS（enable/depth覆写/extra_output 数据驱动）；
新增单形态异常隔离；纯重构零行为变更（9 现有测试零回归 + 4 注册表驱动测试）。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review（计划写完后自检）

**1. Spec 覆盖**：
- PatternMeta + PATTERNS（spec §5.1/§5.2）→ Task 1 ✅
- screener 遍历 + enable/depth/extra/异常隔离（spec §5.3/§7）→ Task 2 Step 3/4 ✅
- test_registry（spec §8.1）→ Task 1 ✅
- screener 现有测试零回归（spec §8.2）→ Task 2 Step 5 ✅
- 注册表驱动测试 enable/depth/extra/异常隔离（spec §8.3）→ Task 2 Step 1 四测试 ✅
- run_checks 5 gate（spec §11 验收）→ Task 2 Step 6 ✅
- 兼容性（签名/schema/consumer 不变）（spec §9）→ Global Constraints + Task 2 验收 ✅
- 扩展点（B2 破底翻等，本轮不做）（spec §10）→ 非目标，registry.py 注释已说明 ✅

**2. 占位符扫描**：无 TBD/TODO/「类似 Task N」/「适当错误处理」——所有步骤含完整代码与精确命令。✅

**3. 类型一致性**：`PatternMeta` 字段名（name/detect/enable_field/depth_override_field/extra_output）在 Task 1 定义、Task 2 screener 消费、test_registry/test_screener 断言中完全一致；`PATTERNS` 在 Task 1 产出、Task 2 `from caisen.patterns.registry import PATTERNS` 消费。✅

**4. Scope**：聚焦单一子系统（形态注册表），2 个 task 各自独立可测，不混杂 B1/B2。✅
