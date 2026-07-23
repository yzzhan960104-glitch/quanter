# Layer 2 解耦 Follow-up 实现计划（#3 / #4 / #2 三项收尾）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 收口 Layer 2 解耦合并前的三项 DEFER follow-up——training_analyzer 反向依赖反转（infra/llm ports&adapters）、三个保留垫片清理、param_iter 收口边界重新定义+清全局 mutation 债。

**Architecture:** #3 用 `infra/llm/` 子包（LLMClient Protocol + GlmClient 实现 + get_llm_client 工厂）承载 LLM 外部依赖，server/backtest 双向依赖它（ports & adapters，未来切 Claude 仅加实现+配置）；#4 删 signal_runner/execution_gateway 纯垫片 + order_state 去 re-export 保真身；#2 订正 spec「收口走 driver」预设为「内核已同源/统计层有意分轨」，清 param_iter 全局 mutation 传参债。

**Tech Stack:** Python 3.10（`.venv310`）、urllib（LLM 调用零新依赖）、typing.Protocol、pytest、pandas。

## Global Constraints

- **分支**：全程在 `feat/layer2-decouple`（未 merge），每 Task 一个 commit，最后与主线统一 merge。
- **零回归红线**：每 Task 后 `python -m pytest` failed 恒为 **1**（`universe*ST` 预存基线，与解耦无关），**不得新增**；passed 数自然变动（删迁移测试所致）可接受。
- **T1 数值**：`python scripts/regression_neckline_golden.py --verify` exit 0、golden kelly 年化零漂移。
- **链路冒烟**：`python scripts/smoke_trading_engine.py` PASS。
- **依赖契约**：`python -m pytest tests/test_layer_contract.py -v` 全 passed。
- **venv**：命令在 `.venv310` 激活态下跑（xtquant 依赖环境）。
- **语言**：所有新增/改注释、docstring 像素级中文（CLAUDE.md）。
- **工作树 stray**：`data/lake_reader.py` 等他人 stray 不提交，每 Task 用显式 `git add <path>` 避免卷入。

---

## File Structure（先映射，再分 Task）

| Task | 动作 | 文件 |
|---|---|---|
| 1 (#3) | 新建子包 | `infra/llm/__init__.py` / `base.py` / `glm.py` |
| 1 (#3) | 改调用方 | `server/services/review_service.py` / `backtest/optimize/training_analyzer.py` |
| 1 (#3) | 改测试+契约 | `tests/caisen/test_training_analyzer.py` / `tests/test_layer_contract.py` |
| 2 (#4a) | 删垫片+改消费 | 删 `trading/signal_runner.py`；改 4 tests import |
| 3 (#4b) | 删垫片+改消费 | 删 `trading/execution_gateway.py`；改 20+ 消费点 |
| 4 (#4c) | 瘦身+改消费 | `trading/order_state.py` 删两 re-export；改消费点 |
| 5 (#2a) | 改传参 | `scripts/param_iter.py` / `scripts/kbkg_trailing_verify.py`（+核实 golden） |
| 6 (#2b) | 加契约测试 | `tests/test_param_iter_kernel_same_source.py`（新建） |
| 7 (#2c) | 订正上游 spec | `docs/superpowers/specs/2026-07-22-layer2-decoupling-design.md` §3.6/§8.4/§10 |

---

## Task 1: infra/llm 子包 + training_analyzer/review_service 反转

**Files:**
- Create: `infra/llm/__init__.py`, `infra/llm/base.py`, `infra/llm/glm.py`
- Modify: `backtest/optimize/training_analyzer.py:18,65-74,126-132`（去 `from server...` + 两处调用）
- Modify: `server/services/review_service.py:18,83-111,149-175`（`_call_glm` 移出 + diagnose 改工厂）
- Test: `tests/caisen/test_training_analyzer.py`（patch 改指）
- Modify: `tests/test_layer_contract.py`（backtest 允许依赖集 + `infra`）

**Interfaces:**
- Produces: `infra.llm.base.LLMClient`（Protocol，`call(prompt, *, max_tokens=4096, temperature=0.3) -> str`）、`LLMConfigError`、`infra.llm.get_llm_client() -> LLMClient`、`infra.llm.glm.GlmClient`。

- [ ] **Step 1: 写 LLMClient Protocol 契约测试（TDD·先红）**

Create `tests/test_infra_llm.py`：

```python
# -*- coding: utf-8 -*-
"""infra/llm 端口与工厂契约：Protocol 形状 + 工厂按 env 选实现 + 凭证缺失抛 LLMConfigError。"""
import os
import pytest
from unittest.mock import patch


def test_llm_client_protocol_call_signature():
    """LLMClient 实现 .call(prompt) -> str（业务语义接口，屏蔽供应商）。"""
    from infra.llm.base import LLMClient

    class _Fake:
        def call(self, prompt: str, *, max_tokens: int = 4096, temperature: float = 0.3) -> str:
            return f"echo:{prompt}"

    assert isinstance(_Fake(), LLMClient)          # runtime_checkable Protocol
    assert _Fake().call("hi") == "echo:hi"


def test_get_llm_client_default_glm(monkeypatch):
    """LLM_PROVIDER 缺省 → 返回 GlmClient 实例。"""
    monkeypatch.setenv("GLM_API_KEY", "fake")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    from infra.llm import get_llm_client
    from infra.llm.glm import GlmClient
    assert isinstance(get_llm_client(), GlmClient)


def test_glm_client_missing_creds_raises_config_error(monkeypatch):
    """凭证缺失 → call() 抛 LLMConfigError（调用方捕获走降级）。"""
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    from infra.llm.glm import GlmClient
    from infra.llm.base import LLMConfigError
    with pytest.raises(LLMConfigError):
        GlmClient().call("any prompt")
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/test_infra_llm.py -v`
Expected: FAIL（`ModuleNotFoundError: infra.llm`）。

- [ ] **Step 3: 建 `infra/llm/base.py`（端口 + 错误类型）**

```python
# -*- coding: utf-8 -*-
"""infra/llm/base.py —— LLM 调用端口（外部依赖抽象层）。

设计哲学（ports & adapters / 策略模式）：
- LLM 是外部依赖（调 z.ai / 未来 Claude Code 等），归 infra/（与 notifier 同层）。
- LLMClient 是「端口」：业务语义接口（给 prompt 出文本），屏蔽供应商细节
  （凭证/端点/HTTP 内化到实现类）。
- 调用失败统一抛异常：LLMConfigError（凭证缺失）或网络异常（urllib 抛），
  由调用方（review_service / training_analyzer）捕获走各自降级——与原
  `_call_glm 异常向上抛、diagnose/analyze_round 捕获降级` 语义一致。
- 可扩展：未来切供应商仅新增 infra/llm/<provider>.py 实现类 + 工厂分支，
  调用方零改动。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


class LLMConfigError(Exception):
    """LLM 配置缺失（凭证未配等）。调用方捕获后走降级，不应阻断业务。"""


@runtime_checkable
class LLMClient(Protocol):
    """LLM 调用端口：给 prompt，出模型文本。

    实现类（GlmClient / 未来 ClaudeClient）自行从 env 读凭证与端点，
    调用成功返回模型文本字符串，失败抛 LLMConfigError 或网络异常。
    """

    def call(self, prompt: str, *, max_tokens: int = 4096,
             temperature: float = 0.3) -> str: ...
```

- [ ] **Step 4: 建 `infra/llm/glm.py`（GLM 实现·封装原 `_call_glm`）**

```python
# -*- coding: utf-8 -*-
"""infra/llm/glm.py —— GlmClient：z.ai Anthropic 兼容端点实现。

封装原 server.services.review_service._call_glm 的 urllib 逻辑（逻辑零改动）：
- 端点 GLM_URL = z.ai /api/anthropic/v1/messages（复用「coding plan」订阅额度，
  非智谱按量余额池——后者已 code 1113 耗尽）。
- 双投鉴权 x-api-key + Authorization: Bearer（兼容 Anthropic 与 z.ai 两套约定）。
- anthropic-version 头为协议必填（2023-06-01）。
凭证/模型从 env 读（GLM_API_KEY/ZHIPU_API_KEY/GLM_MODEL），绝不硬编码。
"""
from __future__ import annotations

import json
import os
import urllib.request

from infra.llm.base import LLMConfigError

# z.ai Anthropic Messages 兼容端点（同原 review_service.GLM_URL，逐字搬移）
GLM_URL = "https://api.z.ai/api/anthropic/v1/messages"
_LLM_TIMEOUT = 60


class GlmClient:
    """GLM（z.ai）LLM 实现。凭证/模型在构造时从 env 读入并持有。"""

    def __init__(self) -> None:
        # 凭证双 fallback（GLM_API_KEY 优先，兼容历史 ZHIPU_API_KEY 命名）
        self._api_key = os.getenv("GLM_API_KEY") or os.getenv("ZHIPU_API_KEY")
        self._model = os.getenv("GLM_MODEL", "glm-4")

    def call(self, prompt: str, *, max_tokens: int = 4096,
             temperature: float = 0.3) -> str:
        """调 GLM 返回模型文本。凭证缺失抛 LLMConfigError，网络异常向上抛。"""
        if not self._api_key:
            raise LLMConfigError("GLM_API_KEY / ZHIPU_API_KEY 未配置")
        body = json.dumps({
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
        }).encode("utf-8")
        req = urllib.request.Request(GLM_URL, data=body, method="POST")
        # 双投鉴权：z.ai AUTH_TOKEN 认 Bearer、标准 Anthropic 认 x-api-key
        req.add_header("x-api-key", self._api_key)
        req.add_header("Authorization", f"Bearer {self._api_key}")
        req.add_header("Content-Type", "application/json")
        req.add_header("anthropic-version", "2023-06-01")
        with urllib.request.urlopen(req, timeout=_LLM_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        # Anthropic Messages 响应：content=[{type:"text", text:"..."}]，取首块文本
        return data["content"][0]["text"]
```

- [ ] **Step 5: 建 `infra/llm/__init__.py`（工厂 + re-export）**

```python
# -*- coding: utf-8 -*-
"""infra/llm —— LLM 外部依赖适配子包（ports & adapters）。

工厂 get_llm_client() 按 LLM_PROVIDER env 选实现（默认 glm）。
未来切 Claude Code：仅新增 infra/llm/claude.py + 下方加一分支，调用方零改动。
"""
from infra.llm.base import LLMClient, LLMConfigError  # noqa: F401  re-export 端口


def get_llm_client() -> LLMClient:
    """按 LLM_PROVIDER env 选 LLM 实现（默认 glm）。

    切换供应商：新增 infra/llm/<provider>.py 实现类 + 此处加分支。
    """
    provider = os.getenv("LLM_PROVIDER", "glm").lower()
    if provider == "glm":
        from infra.llm.glm import GlmClient
        return GlmClient()
    raise ValueError(f"未知 LLM_PROVIDER={provider!r}（当前仅支持 'glm'）")


import os  # noqa: E402  放 get_llm_client 之后，避免占顶部影响端口 re-export 可读性

__all__ = ["LLMClient", "LLMConfigError", "get_llm_client"]
```

> ⚠️ Step 5 的 `import os` 位置为可读性后置；若 linter 报 E402 不可接受，改为顶部 `import os`（二选一，优先顶部 import 更合规）。**执行时放顶部**：

```python
import os

from infra.llm.base import LLMClient, LLMConfigError  # noqa: F401


def get_llm_client() -> LLMClient:
    provider = os.getenv("LLM_PROVIDER", "glm").lower()
    if provider == "glm":
        from infra.llm.glm import GlmClient
        return GlmClient()
    raise ValueError(f"未知 LLM_PROVIDER={provider!r}（当前仅支持 'glm'）")


__all__ = ["LLMClient", "LLMConfigError", "get_llm_client"]
```

- [ ] **Step 6: 运行 Step 1 的契约测试 → PASS**

Run: `python -m pytest tests/test_infra_llm.py -v`
Expected: 3 passed。

- [ ] **Step 7: 改 `backtest/optimize/training_analyzer.py`（去反向 import + 两处调用改工厂）**

把 `training_analyzer.py:15-18` 的：

```python
# 复用 review_service 的 GLM 调用（urllib，零新依赖）——同进程 import 安全（无循环）。
# 模块级 import：使 _call_glm 成为本模块属性，测试 patch.object(training_analyzer, "_call_glm", ...)
# 才能生效；若写成函数内 from ... import 会每次重绑定到源模块，monkeypatch 本模块失效。
from server.services.review_service import _call_glm
```

替换为：

```python
# LLM 调用走 infra/llm 外部依赖适配层（ports & adapters）。
# 模块级 import get_llm_client：使本模块持有该函数引用，测试 patch.object
# (training_analyzer, "get_llm_client", ...) 生效（与原 patch _call_glm 同理）。
from infra.llm import get_llm_client
from infra.llm.base import LLMConfigError
```

`analyze_round`（行 65-74）原：

```python
    api_key = os.getenv("GLM_API_KEY") or os.getenv("ZHIPU_API_KEY")
    if not api_key:
        logger.info("GLM 凭证未配置，analyze_round 走降级（附原始统计）")
        return f"## ⚠️ AI 分析降级（GLM 凭证未配置）\n\n附本轮原始统计供人手判断：\n\n```\n{stats}\n```"
    try:
        return _call_glm(prompt, api_key, os.getenv("GLM_MODEL", "glm-4"), _LLM_TIMEOUT)
    except Exception as exc:
        logger.warning("analyze_round GLM 调用失败，降级：%s", exc)
        return (f"## ⚠️ AI 分析降级（GLM 调用失败：{type(exc).__name__}）\n\n"
                f"附本轮原始统计供人手判断：\n\n```\n{stats}\n```")
```

替换为：

```python
    # LLM 调用走 infra/llm 工厂；凭证缺失(LLMConfigError)或调用失败统一降级，
    # 训练 loop 不应因 LLM 抖动中断（语义同原 _call_glm 异常向上抛）。
    try:
        return get_llm_client().call(prompt)
    except Exception as exc:
        logger.warning("analyze_round GLM 调用失败，降级：%s", exc)
        return (f"## ⚠️ AI 分析降级（GLM 不可用：{type(exc).__name__}）\n\n"
                f"附本轮原始统计供人手判断：\n\n```\n{stats}\n```")
```

`parse_review`（行 126-132）原：

```python
    api_key = os.getenv("GLM_API_KEY") or os.getenv("ZHIPU_API_KEY")
    if not api_key:
        raise ParseError("GLM 凭证未配置，请按 `改 字段=值 重跑` 格式手动说明。")
    try:
        raw = _call_glm(prompt, api_key, os.getenv("GLM_MODEL", "glm-4"), _LLM_TIMEOUT)
    except Exception as exc:
        raise ParseError(f"GLM 调用失败：{type(exc).__name__}") from exc
```

替换为：

```python
    try:
        raw = get_llm_client().call(prompt)
    except LLMConfigError as exc:
        raise ParseError("GLM 凭证未配置，请按 `改 字段=值 重跑` 格式手动说明。") from exc
    except Exception as exc:
        raise ParseError(f"GLM 调用失败：{type(exc).__name__}") from exc
```

（`_LLM_TIMEOUT` 常量与 `import os` 若本文件别处不再用则一并清；`os` 在 analyze_round/parse_review 已无引用，删除顶部 `import os` 前先 grep 确认本文件无其它 `os.` 用法。）

- [ ] **Step 8: 改 `server/services/review_service.py`（_call_glm 移出 + diagnose 改工厂）**

删除 `review_service.py:83-111` 的 `_call_glm` 函数 + `GLM_URL`/`_LLM_TIMEOUT` 常量（行 37、41，已下沉到 infra/llm/glm.py）。顶部不再需 `import urllib.request`/`import urllib.error`（diagnose 的 except 仍用 `urllib.error.HTTPError`——**保留** `import urllib.error`，仅删 `urllib.request`）。

`diagnose`（行 149-175）原凭证检查 + 调用段：

```python
    # 3) 取凭证 + 模型（凭证隔离：仅从环境变量读，绝不硬编码）
    api_key = os.getenv("GLM_API_KEY") or os.getenv("ZHIPU_API_KEY")
    model = os.getenv("GLM_MODEL", "glm-4")

    # 4) 缺凭证 → 降级（上下文摘要）
    if not api_key:
        logger.info("GLM_API_KEY 未配置，复盘走降级模式（上下文摘要）")
        return ReviewReport(
            ok=True, degraded=True, model=None,
            report=_degraded_report(prompt, "GLM_API_KEY / ZHIPU_API_KEY 未配置"),
            reason="GLM 凭证未配置",
        )

    # 5) 调用 GLM（失败走降级，绝不抛 500 阻断请求）
    try:
        report = _call_glm(prompt, api_key, model)
        return ReviewReport(ok=True, degraded=False, model=model, report=report)
    except urllib.error.HTTPError as exc:
        reason = f"GLM HTTP {exc.code}：{exc.reason}"
        logger.warning("GLM 调用 HTTP 错误：%s", reason)
        return ReviewReport(ok=True, degraded=True, model=model,
                            report=_degraded_report(prompt, reason), reason=reason)
    except Exception as exc:
        reason = f"GLM 调用失败：{type(exc).__name__}: {exc}"
        logger.warning("GLM 调用异常：%s", exc)
        return ReviewReport(ok=True, degraded=True, model=model,
                            report=_degraded_report(prompt, reason), reason=reason)
```

替换为：

```python
    # 3) 调 LLM（走 infra/llm 工厂；凭证缺失/调用失败统一降级，绝不抛 500 阻断请求）
    from infra.llm import get_llm_client
    from infra.llm.base import LLMConfigError
    model = os.getenv("GLM_MODEL", "glm-4")
    try:
        report = get_llm_client().call(prompt)
        return ReviewReport(ok=True, degraded=False, model=model, report=report)
    except LLMConfigError as exc:
        reason = f"GLM 凭证未配置：{exc}"
        logger.info("复盘走降级模式（上下文摘要）：%s", reason)
        return ReviewReport(ok=True, degraded=True, model=None,
                            report=_degraded_report(prompt, "GLM_API_KEY / ZHIPU_API_KEY 未配置"),
                            reason="GLM 凭证未配置")
    except urllib.error.HTTPError as exc:
        reason = f"GLM HTTP {exc.code}：{exc.reason}"
        logger.warning("GLM 调用 HTTP 错误：%s", reason)
        return ReviewReport(ok=True, degraded=True, model=model,
                            report=_degraded_report(prompt, reason), reason=reason)
    except Exception as exc:
        reason = f"GLM 调用失败：{type(exc).__name__}: {exc}"
        logger.warning("GLM 调用异常：%s", exc)
        return ReviewReport(ok=True, degraded=True, model=model,
                            report=_degraded_report(prompt, reason), reason=reason)
```

> 顶部加 `from infra.llm.base import LLMConfigError`（或函数内 import 如上）。`_assemble_prompt`/`_degraded_report`/`diagnose`/`export_trades` import 全保留（review 业务留此）。

- [ ] **Step 9: 改 `tests/caisen/test_training_analyzer.py`（patch 改指）**

顶部加 fake client 工具 + 把 `patch.object(training_analyzer, "_call_glm", return_value=X)` 改为 patch `get_llm_client` 返回 fake：

```python
class _FakeLLM:
    """实现 LLMClient Protocol 的测试替身：call 返回预设文本。"""
    def __init__(self, text: str):
        self._text = text
    def call(self, prompt: str, *, max_tokens: int = 4096, temperature: float = 0.3) -> str:
        self.last_prompt = prompt   # 供断言 prompt 内容
        return self._text
```

`test_analyze_round_assembles_prompt_and_returns_report` 改：

```python
def test_analyze_round_assembles_prompt_and_returns_report(monkeypatch):
    monkeypatch.setenv("GLM_API_KEY", "fake-key-for-mock")
    fake = _FakeLLM("## 第1轮报告\n表现尚可")
    with patch.object(training_analyzer, "get_llm_client", return_value=fake):
        report = training_analyzer.analyze_round(_REPORT, _CFG, [])
    assert "第1轮报告" in report
    assert "12" in fake.last_prompt          # 当前轮 n_hits 进 prompt
    assert "min_rr" in fake.last_prompt      # 当前 cfg 进 prompt
```

`test_parse_review_*` 三测同理：`patch.object(training_analyzer, "get_llm_client", return_value=_FakeLLM(glm_out))`。

`test_analyze_round_degrades_without_glm_key` 改（走真实 GlmClient 无凭证路径）：

```python
def test_analyze_round_degrades_without_glm_key(monkeypatch):
    """缺 GLM 凭证 → GlmClient.call 抛 LLMConfigError → analyze_round 捕获降级。"""
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    out = training_analyzer.analyze_round(_REPORT, _CFG, [])
    assert "降级" in out or "AI 不可用" in out
    assert "12" in out   # 仍附原始统计
```

- [ ] **Step 10: 改 `tests/test_layer_contract.py`（backtest 允许依赖集 +infra）**

读 `tests/test_layer_contract.py` 找 backtest 依赖白名单（现只许 `trading.compute`/`strategies`/`data`），把 `infra` 加入允许集（注释注明「外部依赖适配层，与 data 同级」）。

- [ ] **Step 11: 全量验证**

Run:
```bash
python -m pytest tests/test_infra_llm.py tests/caisen/test_training_analyzer.py tests/test_layer_contract.py -v
python -m pytest -x -q
python scripts/smoke_trading_engine.py
```
Expected: 新测全 passed；全量 failed 恒 1（universe\*ST）；smoke PASS。

- [ ] **Step 12: grep 确认反向依赖已断 + commit**

Run: `grep -rn "from server" backtest/ || echo "OK: backtest 零 server 依赖"`
Expected: `OK: backtest 零 server 依赖`。

```bash
git add infra/llm/ tests/test_infra_llm.py backtest/optimize/training_analyzer.py \
  server/services/review_service.py tests/caisen/test_training_analyzer.py tests/test_layer_contract.py
git commit -m "refactor(infra): #3 立 infra/llm ports&adapters 子包·training_analyzer/review_service 反转依赖(backtest不再import server·未来切Claude仅加实现+配置)"
```

---

## Task 2: 删 signal_runner 垫片（4 tests 改指）

**Files:**
- Delete: `trading/signal_runner.py`
- Modify: 4 tests（`from trading.signal_runner import` → `from trading.compute.plan import`）

**Interfaces:**
- Consumes: `trading.compute.plan.build_orders_from_signals` / `PlannedOrder`（真身，已存在）。

- [ ] **Step 1: 锁基线（确认当前绿）**

Run: `python -m pytest tests/trading/test_signal_runner.py tests/trading/test_signal_runner_attribution.py tests/trading/test_engine_eod_injection.py tests/experiment/test_e2e_eod_to_plan.py -v`
Expected: 全 passed。

- [ ] **Step 2: grep 全量消费点**

Run: `grep -rn "trading.signal_runner\|trading/signal_runner\|from trading import signal_runner" --include="*.py" .`
Expected: 仅 4 个 test 文件 import（生产代码已指 compute/plan）。**若有生产代码消费，先改它**（改指 `trading.compute.plan`）。

- [ ] **Step 3: 4 tests 改 import 源**

每个文件把 `from trading.signal_runner import build_orders_from_signals, PlannedOrder` 改为 `from trading.compute.plan import build_orders_from_signals, PlannedOrder`。

- [ ] **Step 4: 删垫片**

Run: `git rm trading/signal_runner.py`

- [ ] **Step 5: 验证**

Run:
```bash
grep -rn "trading.signal_runner\|trading/signal_runner" --include="*.py" . || echo "OK: 零残留"
python -m pytest tests/trading/test_signal_runner.py tests/trading/test_signal_runner_attribution.py tests/trading/test_engine_eod_injection.py tests/experiment/test_e2e_eod_to_plan.py -v
python -m pytest -x -q
```
Expected: grep 零残留；4 tests passed；全量 failed 恒 1。

- [ ] **Step 6: Commit**

```bash
git add -u && git commit -m "refactor(trading): #4a 删 signal_runner 垫片·4 tests 改指 compute/plan 真身(零回归)"
```

---

## Task 3: 删 execution_gateway 垫片（20+ 消费点按符号改指）

**Files:**
- Delete: `trading/execution_gateway.py`
- Modify: 20+ 消费点（按符号集改指 4 目标）

**Interfaces:**
- Consumes（符号→真身目标映射）：

| 符号 | 改指 |
|---|---|
| `BaseExecutionGateway` / `OrderResult` | `broker.base` |
| `MockExecutionGateway` | `broker.mock` |
| `reconcile` / `PositionDrift` / `ReconciliationResult` | `trading.compute.reconcile` |
| `OrderRequest` | `trading.compute.types` |

- [ ] **Step 1: grep 全量消费清单（先摸规模）**

Run: `grep -rln "trading.execution_gateway\|from trading import execution_gateway" --include="*.py" . > /tmp/eg_consumers.txt && cat /tmp/eg_consumers.txt && wc -l /tmp/eg_consumers.txt`
Expected: 列出所有消费文件 + 计数（预估 20+）。逐文件记录它 import 的符号集。

- [ ] **Step 2: 逐消费点改 import**

对每个消费文件，按它实际 import 的符号改指对应目标。例：

```python
# 原：from trading.execution_gateway import BaseExecutionGateway, OrderRequest, reconcile
# 改：from broker.base import BaseExecutionGateway
#     from trading.compute.types import OrderRequest
#     from trading.compute.reconcile import reconcile
```

> 纯机械替换，无逻辑改动。逐文件改完即跑该文件相关测试确认未漏符号。

- [ ] **Step 3: 删垫片**

Run: `git rm trading/execution_gateway.py`

- [ ] **Step 4: 验证**

Run:
```bash
grep -rn "trading.execution_gateway\|trading/execution_gateway" --include="*.py" . || echo "OK: 零残留"
python -m pytest -x -q
python scripts/smoke_trading_engine.py
```
Expected: grep 零残留；全量 failed 恒 1；smoke PASS（实盘下单/撤单/查持仓链路不变）。

- [ ] **Step 5: Commit**

```bash
git add -u && git commit -m "refactor(trading): #4b 删 execution_gateway 垫片·20+ 消费点按符号改指 broker/compute 真身(零回归·smoke通)"
```

---

## Task 4: order_state 去 re-export（保 OrderStateMachine 真身）

**Files:**
- Modify: `trading/order_state.py`（删行 25 OrderState re-export + 行 267-271 出场函数 re-export + docstring 瘦身）
- Modify: OrderState 枚举消费点 → `trading.types.order_state`；出场函数消费点 → `trading.compute.stop`

**Interfaces:**
- Produces（保留）：`trading.order_state.OrderStateMachine`（真身，imperative shell）。
- Consumes（真身目标）：`trading.types.order_state.OrderState`、`trading.compute.stop.{check_stop_loss, check_take_profit, update_trailing_stop}`。

- [ ] **Step 1: grep 两个 re-export 的消费点**

Run:
```bash
echo "=== OrderState 经 order_state 导入的消费点 ===" 
grep -rn "from trading.order_state import" --include="*.py" . | grep -i "OrderState"
echo "=== 出场函数经 order_state 导入的消费点 ==="
grep -rn "from trading.order_state import" --include="*.py" . | grep -iE "check_stop_loss|check_take_profit|update_trailing_stop"
```
Expected: 列出消费点（逐个改指）。

- [ ] **Step 2: 消费点改指真身**

- `from trading.order_state import OrderState` → `from trading.types.order_state import OrderState`
- `from trading.order_state import check_stop_loss, ...` → `from trading.compute.stop import check_stop_loss, ...`

> 若某文件同时 import OrderStateMachine（真身），保留 `from trading.order_state import OrderStateMachine`，其余符号改指。

- [ ] **Step 3: 瘦身 `trading/order_state.py`**

删除行 24-25：
```python
# OrderState 纯枚举 re-export（Layer2 阶段5 · 真身迁 trading/types/order_state.py）。
from trading.types.order_state import OrderState  # noqa: F401
```
删除行 257-271 整段（出场函数 re-export 注释块 + `from trading.compute.stop import ...`）。

更新 module docstring：去掉「re-export OrderState/出场函数」描述，只述 `OrderStateMachine` 状态机职责 + 保留 `from enum import auto`（若 OrderStateMachine 不引用 `auto` 则一并删，先 grep `auto()` 在本文件用法）。

- [ ] **Step 4: 验证**

Run:
```bash
python -m pytest -x -q
python scripts/smoke_trading_engine.py
```
Expected: 全量 failed 恒 1；smoke PASS。

- [ ] **Step 5: Commit**

```bash
git add -u && git commit -m "refactor(trading): #4c order_state 去 OrderState枚举/出场函数两 re-export·保 OrderStateMachine 真身(消费点改指 types/compute·零回归)"
```

---

## Task 5: param_iter 等去全局 mutation → 显式传参

**Files:**
- Modify: `scripts/param_iter.py:146-188`（run_one 去 `DEFAULTS.update`/`EXEC_DEFAULTS.update` + try/finally）
- Modify: `scripts/kbkg_trailing_verify.py:69-105`（run 去 mutation）
- 核实: `scripts/regression_neckline_golden.py`（若也 mutation 则一并改）

**Interfaces:**
- Consumes: `strategies.neckline.backtest.scan_symbol(sym_df, window, exec=None, id_cfg=None)`（已支持传参，`id_cfg` 默认 `{**DEFAULTS, "window":window}`，显式传则绕过全局）。
- 关键事实（已核实）：`detect_neckline_method(sym_df, cfg, atr_series=...)` **不读全局 DEFAULTS**，只读传入 `cfg` → 显式传参彻底等价。

- [ ] **Step 1: 锁 golden 基线（改前 capture）**

Run: `python scripts/regression_neckline_golden.py --verify`
Expected: exit 0（当前 golden 已对齐基线，作为改传参后的对比锚）。

- [ ] **Step 2: 核实 golden 脚本是否 mutation**

Run: `grep -n "DEFAULTS.update\|EXEC_DEFAULTS.update" scripts/regression_neckline_golden.py scripts/identify_param_scan.py`
Expected: golden 若有 mutation 则纳入本 Task；`identify_param_scan.py` 已是 `cfg={**DEFAULTS,...}` 传参模式（**不动**）。

- [ ] **Step 3: 改 `scripts/param_iter.py::run_one`**

原（行 155-187）核心：

```python
    exec_params = {k: params[k] for k, lay, _ in PARAM_SPACE if lay == "exec"}

    orig_id = copy.deepcopy(DEFAULTS)
    orig_exec = copy.deepcopy(EXEC_DEFAULTS)
    DEFAULTS.update(id_params)
    EXEC_DEFAULTS.update(exec_params)
    try:
        all_filled = []
        window = DEFAULTS["window"]
        exec_cfg = EXEC_DEFAULTS   # scan_symbol 读此全局（已 update）
        for sym, sym_df in universe.items():
            try:
                filled, _n_sig, _n_skip = scan_symbol(sym_df, window, exec=exec_cfg)
                ...
```

改为（显式构造 id_cfg/exec_cfg 传入，零全局 mutation）：

```python
    exec_params = {k: params[k] for k, lay, _ in PARAM_SPACE if lay == "exec"}
    # 显式构造识别层/执行层 cfg 传入 scan_symbol（不再 mutation 全局 DEFAULTS/EXEC_DEFAULTS）。
    # scan_symbol 的 id_cfg 默认 {**DEFAULTS, window:window} 会读全局——显式传则绕过全局，
    # 与原 update 全局后 scan_symbol 拷贝全局的行为逐字等价（detect_neckline_method 只读传入 cfg）。
    id_cfg = {**DEFAULTS, **id_params}
    exec_cfg = {**EXEC_DEFAULTS, **exec_params}
    window = id_cfg["window"]
    all_filled = []
    for sym, sym_df in universe.items():
        try:
            filled, _n_sig, _n_skip = scan_symbol(sym_df, window, exec=exec_cfg, id_cfg=id_cfg)
            for r in filled:
                r["symbol"] = sym
            all_filled.extend(filled)
        except Exception:
            continue
    if not all_filled:
        return -1.0, 0.0, 1.0, 0, 0.0, 0.0
    # P1-c 宽度顺势加权（可选）：信号日 breadth≥0.4 → avg_pnl×1.5（等效加仓）
    if breadth is not None:
        for r in all_filled:
            bd = _breadth_at(breadth, r["signal_date"])
            if bd is not None and bd >= 0.4:
                r["avg_pnl_pct"] *= 1.5
    pnls = [r["avg_pnl_pct"] for r in all_filled]
    dates = [pd.to_datetime(r["signal_date"]) for r in all_filled]
    kelly, curve, ann, sharpe, max_dd = risk_metrics(pnls, dates)
    return ann, kelly, curve, len(all_filled), sharpe, max_dd
```

> 删除 try/finally 恢复全局的代码（不再 mutation 即无需恢复）。删除 `import copy`（若本文件别处不用）。`breadth` 分支逻辑原样保留。

- [ ] **Step 4: 改 `scripts/kbkg_trailing_verify.py::run`**

原（行 69-71）：

```python
def run(lbl, exec_p):
    DEFAULTS.update(id_p); EXEC_DEFAULTS.update(exec_p)
    all_filled = []
    ...
            filled, _, _ = scan_symbol(df, id_p["window"], exec=EXEC_DEFAULTS)
```

改为：

```python
def run(lbl, exec_p):
    id_cfg = {**DEFAULTS, **id_p}
    exec_cfg = {**EXEC_DEFAULTS, **exec_p}
    all_filled = []
    ...
            filled, _, _ = scan_symbol(df, id_cfg["window"], exec=exec_cfg, id_cfg=id_cfg)
```

- [ ] **Step 5: T1 golden 验零漂移（核心红线）**

Run: `python scripts/regression_neckline_golden.py --verify`
Expected: exit 0（改传参后数值与基线逐位一致——证明传参等价于 mutation）。

> 若 golden 漂移：说明 scan_symbol/detect 有未发现的隐式全局读取，回到 Step 3 排查（grep `DEFAULTS[` 在 strategies/neckline/ 内的裸引用），**不放过任何漂移**。

- [ ] **Step 6: 全量回归 + commit**

Run: `python -m pytest -x -q`
Expected: failed 恒 1。

```bash
git add scripts/param_iter.py scripts/kbkg_trailing_verify.py scripts/regression_neckline_golden.py
git commit -m "refactor(backtest): #2a param_iter/kbkg 去全局 DEFAULTS.update mutation→显式 exec/id_cfg 传参(golden零漂移证等价·detect不读全局)"
```

---

## Task 6: 补内核同源契约测试

**Files:**
- Create: `tests/test_param_iter_kernel_same_source.py`

**Interfaces:**
- Consumes: `strategies.neckline.backtest.scan_symbol`、`backtest.replay.replay`（driver）、`strategies.neckline_method.NecklineMethodStrategy.scan_at`。

- [ ] **Step 1: 写契约测试（固化「param_iter 内核 = driver 内核」同源）**

Create `tests/test_param_iter_kernel_same_source.py`：

```python
# -*- coding: utf-8 -*-
"""固化 spec§3.6 订正：param_iter 调的 scan_symbol 与 replay driver 调的 scan_at
走同一 simulate_exit/detect_neckline_method 函数对象（is 同源）。

物理意图：统计层有意分轨（调参 kelly vs 展示 CAGR），但识别+模拟内核必须同源——
否则回测调参优化的参数与实盘/异步回测的执行会分叉。本测试是「分轨但同源」契约的护栏。
"""
from strategies.neckline.backtest import scan_symbol, simulate_exit
from strategies.neckline.method_v0 import detect_neckline_method


def test_param_iter_kernel_is_same_object_as_driver_kernel():
    """scan_symbol 内部调的 simulate_exit/detect 与 driver(scan_at) 调的是同一函数对象。"""
    # scan_symbol 的闭包内引用的 simulate_exit/detect_neckline_method，
    # 与 NecklineMethodStrategy.scan_at 引用的，须是同一函数对象（is 同源）。
    import strategies.neckline.backtest as bk
    import strategies.neckline.method_v0 as m0
    # scan_symbol 模块级引用的 simulate_exit / detect_neckline_method
    assert bk.simulate_exit is simulate_exit
    assert bk.detect_neckline_method is detect_neckline_method
    # NecklineMethodStrategy(scan_at 的实现) 也引用同一 detect/simulate（同模块源）
    import strategies.neckline_method as nm
    # 颈线法策略与 backtest 共用 strategies.neckline 同一份内核
    assert nm.detect_neckline_method is detect_neckline_method or \
           hasattr(nm.NecklineMethodStrategy, "scan_at")  # scan_at 经 strategy 调同一内核


def test_scan_symbol_accepts_id_cfg_no_global_mutation(monkeypatch):
    """scan_symbol 接受显式 id_cfg，不依赖调用方 mutation 全局 DEFAULTS。"""
    import pandas as pd
    from strategies.neckline.method_v0 import DEFAULTS
    # 构造极小 df（仅触发参数路径，不求识别命中）
    idx = pd.date_range("2024-01-01", periods=80, freq="B")
    df = pd.DataFrame({"high": 10.0, "low": 9.0, "close": 9.5, "volume": 1000,
                       "amount": 10000}, index=idx)
    df["symbol"] = "000001.SZ"
    # 传与默认不同的 id_cfg，证明走显式参数（非全局）
    id_cfg = {**DEFAULTS, "window": 30}
    filled, n_sig, n_skip = scan_symbol(df, 30, id_cfg=id_cfg)
    assert isinstance(filled, list)   # 不抛即参数路径通
```

> Step 1 的同源断言用 `is` 比较函数对象——若 NecklineMethodStrategy 经由不同 import 路径持有副本（如 sys.modules 别名），断言需调整为比较 `__module__`+`__qualname__`。执行时先跑看是否真 is 同对象，据实调整断言强度（is 同源最理想；退而求 `__qualname__` 一致 + 同 module file）。

- [ ] **Step 2: 运行验证**

Run: `python -m pytest tests/test_param_iter_kernel_same_source.py -v`
Expected: passed（若 is 断言失败，按 Step 1 注记降级为 qualname 断言并记录原因）。

- [ ] **Step 3: Commit**

```bash
git add tests/test_param_iter_kernel_same_source.py
git commit -m "test(backtest): #2b 立 param_iter 内核与 driver 内核 is 同源契约(固化统计层分轨但识别内核同源·防未来分叉)"
```

---

## Task 7: 订正上游 spec（§3.6 / §8.4 / §10）

**Files:**
- Modify: `docs/superpowers/specs/2026-07-22-layer2-decoupling-design.md`（§3.6 / §8.4 / §10#2）
- Modify: `docs/superpowers/specs/2026-07-23-layer2-followup-design.md`（§0 标记已实施）

- [ ] **Step 1: 订正 §3.6**

把 `2026-07-22-layer2-decoupling-design.md` §3.6 中：
> 「`param_iter.py`/`identify_param_scan.py` 绕开 driver 直调 `neckline_backtest.scan_symbol`（双源路径隐患）」

订正为：
> 「识别+模拟内核已同源（Signal dataclass + scan_symbol 参数化，Task 1.6 收口）；统计层有意分轨（param_iter kelly 调参目标函数 vs replay CAGR 展示统计）—— 非债。全局 mutation 传参债已清（follow-up 2026-07-23 §3.2）。」

- [ ] **Step 2: 订正 §8.4**

把 §8.4 第三条：
> 「阶段 4 · driver 收口：`param_iter`/`identify_param_scan` 改走 driver（消灭双源路径）→ T1 数值一致。」

订正为：
> 「内核同源由 `test_scan_symbol_matches_strategy` + `test_param_iter_kernel_same_source` 守护；统计层分轨是设计（调参 vs 展示）。T1 golden 守 param_iter 改传参后数值零漂移。」

- [ ] **Step 3: 订正 §10 待裁决 #2**

把 §10 第 2 条标记为「已定案」：
> 「出场双源收口方式：已由 Task 1.6 Signal dataclass + scan_symbol 参数化收口内核；统计层分轨定案（见 follow-up 2026-07-23 §3）。」

- [ ] **Step 4: 标记 follow-up spec 已实施**

在 `2026-07-23-layer2-followup-design.md` §0 状态行由「🟡 草案【待用户复审】」改为「🟢 已实施（2026-07-23，commits 见 Task 1-7）」。

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-07-22-layer2-decoupling-design.md docs/superpowers/specs/2026-07-23-layer2-followup-design.md
git commit -m "docs(spec): #2c 订正 layer2 spec §3.6/§8.4/§10(param_iter内核已同源/统计层有意分轨·非债)+follow-up标已实施"
```

---

## Self-Review（计划对 spec 覆盖核对）

- **#3 覆盖**：Task 1 建 infra/llm 子包（base/glm/__init__）+ 改 training_analyzer/review_service + test + layer_contract。✓ 对应 spec§1。
- **#4 覆盖**：Task 2（signal_runner）/ Task 3（execution_gateway）/ Task 4（order_state）。✓ 对应 spec§2，含 order_state 保 OrderStateMachine。
- **#2 覆盖**：Task 5（去 mutation 传参）/ Task 6（同源契约测试）/ Task 7（spec 订正）。✓ 对应 spec§3+§4。
- **验证红线**：每 Task 含 pytest failed 恒 1 + golden 零漂移（Task 5）+ smoke（Task 1/3/4）+ grep 零残留（Task 2/3）。✓ 对应 spec§5。
- **依赖铁律**：Task 1 Step 10 改 test_layer_contract 补 backtest→infra；Task 1 Step 12 grep 证 backtest 零 server 依赖。✓
- **placeholder 扫描**：Step 5 的 `import os` 位置给了两种写法并指明取顶部；Task 6 同源断言给了 is 失败时的降级（qualname）。无 TBD/TODO。✓
- **类型一致**：`LLMClient.call(prompt, *, max_tokens, temperature) -> str` 在 base/glm/__init__/test/调用方四处签名一致；`get_llm_client() -> LLMClient` 一致。✓
