# -*- coding: utf-8 -*-
"""层级六·AI 复盘服务：Prompt 组装 + LLM 调用 + 降级。

设计哲学（Karpathy 极简 + ports & adapters）：
- LLM 调用经 infra/llm 工厂（get_llm_client().call），HTTP/凭证/端点细节内化到
  infra/llm/glm.py.GlmClient（仍用标准库 urllib，零新依赖、不引 openai/langchain 黑盒）；
  本模块只保留 review 业务：Prompt 组装 + 三级降级。
- 凭证 GLM_API_KEY/ZHIPU_API_KEY 走环境变量（.env），由 GlmClient 读取，绝不硬编码。
- 三级降级（绝不阻断）：
    1) GLM_API_KEY 缺失 → 返回结构化上下文摘要（报告可读，标注降级原因）。
    2) GLM 调用失败（网络/超时/限频）→ 同样降级为上下文摘要 + 失败原因。
    3) 无日志数据（csv_text/start-end 均无）→ ok=False 明确提示。

拷问三连（已显式处置）：
- Prompt 注入：用户提供的 csv_text 被限定在 ```csv 代码块内，且系统指令在前；
  策略名/参数经 json.dumps 序列化，降低注入风险。
- 超长上下文：csv_text 截断到 8000 字符（GLM context 上限保护），截断时标注原长度。
- 超时阻断：infra/llm 内 urlopen timeout=60s；超时走降级，不挂起请求。
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
from typing import Any, Dict, Optional

from infra.llm import get_llm_client
from infra.llm.base import LLMConfigError
from server.schemas.review import ReviewRequest, ReviewReport
from server.services.trading_service import export_trades

logger = logging.getLogger(__name__)

# LLM 调用走 infra/llm 外部依赖适配层（ports & adapters）：端点 GLM_URL / urllib 细节 /
# 凭证读取已下沉到 infra/llm/glm.py.GlmClient。本模块只保留 Prompt 组装 + 三级降级
# （review 业务）。原 z.ai /api/anthropic 端点路由说明亦随 _call_glm 一并下沉至 GlmClient。
# CSV/Prompt 截断阈值（防超长上下文 + 控成本）
_MAX_CSV_CHARS = 8000
_MAX_PROMPT_TAIL = 4000


def _assemble_prompt(
    csv_text: str,
    strategy_name: Optional[str],
    strategy_params: Dict[str, Any],
    metrics: Dict[str, Any],
) -> str:
    """组装复盘 Prompt（Markdown 结构化，要求 LLM 按固定三段输出）。"""
    params_str = json.dumps(strategy_params, ensure_ascii=False) if strategy_params else "（未提供）"
    metrics_str = json.dumps(metrics, ensure_ascii=False) if metrics else "（未提供）"
    # CSV 截断：超长时保留头部 + 标注原长度（GLM context 保护）
    if len(csv_text) > _MAX_CSV_CHARS:
        csv_block = csv_text[:_MAX_CSV_CHARS] + f"\n...（已截断，原文共 {len(csv_text)} 字符）"
    else:
        csv_block = csv_text
    return f"""你是一位资深量化策略风控官。请基于以下实盘交易日志与策略上下文，输出 **Markdown 格式** 的复盘诊断报告。

## 策略上下文
- 策略名：{strategy_name or '（未提供）'}
- 超参数：{params_str}
- 关键指标：{metrics_str}

## 实盘交易日志（CSV）
```csv
{csv_block}
```

## 输出要求（严格按此结构，使用 Markdown）
### 1. 做得好的地方
（识别正贡献的交易模式 / 因子表现稳健的区间）

### 2. 滑点 / 逻辑异常点
（异常成交价、频繁反向交易、疑似重复发单、止损触发过于密集等）

### 3. 策略超参数调整建议
（针对 observed 行为给出可落地的参数微调方向：止损带宽、调仓阈值、融合权重等）

请直接输出报告正文，不要重复输入数据。"""


def _degraded_report(prompt: str, reason: str) -> str:
    """降级报告：LLM 不可用时，把已组装的上下文摘要输出（供人工/外部 LLM 分析）。"""
    return (
        "## ⚠️ LLM 降级模式\n\n"
        f"**原因**：{reason}\n\n"
        "---\n\n以下为已组装的复盘上下文（可复制给外部 LLM 或人工分析）：\n\n"
        f"```\n{prompt[-_MAX_PROMPT_TAIL:]}\n```"
    )


def diagnose(req: ReviewRequest) -> ReviewReport:
    """端到端复盘：解析数据源 → 组装 Prompt → 调 LLM → 返回 Markdown 报告。

    LLM 不可用（缺凭证/调用失败）→ 降级返回上下文摘要（ok=True, degraded=True）。
    无日志数据 → ok=False 明确提示（非降级，是输入错误）。
    """
    # 1) 解析数据源：csv_text 优先，否则按 start/end 读 logs/live_trades.csv
    csv_text = req.csv_text
    if not csv_text and req.start and req.end:
        try:
            csv_text = export_trades(req.start, req.end)
        except Exception as exc:
            logger.warning("复盘读取实盘日志失败：%s", exc)
            csv_text = None
    if not csv_text or not csv_text.strip():
        return ReviewReport(
            ok=False,
            report="无交易日志可复盘。请提供 csv_text，或有效的 start/end 日期区间（且 logs/live_trades.csv 有数据）。",
            degraded=True,
            reason="无日志数据",
        )

    # 2) 组装 Prompt
    prompt = _assemble_prompt(csv_text, req.strategy_name, req.strategy_params, req.metrics)

    # 3) 调 LLM（走 infra/llm 工厂；凭证缺失/调用失败统一降级，绝不抛 500 阻断请求）。
    #    旧版前置 ``if not api_key`` 已合并为异常驱动：GlmClient.call 在缺凭证时抛
    #    LLMConfigError，此处同 try/except 捕获走降级——逻辑等价但分支收敛为一处。
    model = os.getenv("GLM_MODEL", "glm-4")
    try:
        report = get_llm_client().call(prompt)
        return ReviewReport(ok=True, degraded=False, model=model, report=report)
    except LLMConfigError as exc:
        reason = "GLM 凭证未配置"
        logger.info("复盘走降级模式（上下文摘要）：%s", exc)
        return ReviewReport(ok=True, degraded=True, model=None,
                            report=_degraded_report(prompt, "GLM_API_KEY / ZHIPU_API_KEY 未配置"),
                            reason=reason)
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
