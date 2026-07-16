# -*- coding: utf-8 -*-
"""层级六·AI 复盘服务：Prompt 组装 + GLM 调用 + 降级。

设计哲学（Karpathy 极简 + 反黑盒）：
- LLM 调用用标准库 urllib（零新依赖，不引 openai/langchain 黑盒），HTTP 细节显式可见。
- 凭证 GLM_API_KEY/ZHIPU_API_KEY 走环境变量（.env），绝不硬编码。
- 三级降级（绝不阻断）：
    1) GLM_API_KEY 缺失 → 返回结构化上下文摘要（报告可读，标注降级原因）。
    2) GLM 调用失败（网络/超时/限频）→ 同样降级为上下文摘要 + 失败原因。
    3) 无日志数据（csv_text/start-end 均无）→ ok=False 明确提示。

拷问三连（已显式处置）：
- Prompt 注入：用户提供的 csv_text 被限定在 ```csv 代码块内，且系统指令在前；
  策略名/参数经 json.dumps 序列化，降低注入风险。
- 超长上下文：csv_text 截断到 8000 字符（GLM context 上限保护），截断时标注原长度。
- 超时阻断：urlopen timeout=60s；超时走降级，不挂起请求。
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from server.schemas.review import ReviewRequest, ReviewReport
from server.services.trading_service import export_trades

logger = logging.getLogger(__name__)

# LLM 调用端点（2026-07-16 改走 z.ai Anthropic Messages 兼容端点）
# Why z.ai /api/anthropic 而非智谱官方 open.bigmodel.cn：同一智谱 key 两端点计费池隔离——
# paas/v4（智谱/OpenAI 格式）走按量余额池（已耗尽 code 1113），而 z.ai 的 /api/anthropic
# （Anthropic Messages 格式）走「coding plan」订阅额度（实测 glm-5.2 可用）。Claude Code
# 本身即经此端点跑 glm-5.2，故复用同条有余额的通路。请求格式见 _call_glm。
GLM_URL = "https://api.z.ai/api/anthropic/v1/messages"
# CSV/Prompt 截断阈值（防超长上下文 + 控成本）
_MAX_CSV_CHARS = 8000
_MAX_PROMPT_TAIL = 4000
_LLM_TIMEOUT = 60


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


def _call_glm(prompt: str, api_key: str, model: str, timeout: int = _LLM_TIMEOUT) -> str:
    """同步调用 LLM（z.ai Anthropic Messages 兼容端点）返回模型文本。

    端点路由（2026-07-16）：走 GLM_URL = z.ai /api/anthropic/v1/messages，复用「coding plan」
    订阅额度（非智谱官方按量余额池，后者已 code 1113 耗尽）。函数签名保持稳定，diagnose /
    analyze_round / parse_review 等调用方零感知。

    用 urllib（零新依赖，HTTP 细节显式可见）：
    - 认证双投 x-api-key + Authorization: Bearer：z.ai AUTH_TOKEN 认 Bearer、标准 Anthropic
      认 x-api-key，双投兼容两套鉴权约定。
    - anthropic-version 头为 Anthropic 协议必填（2023-06-01）。
    - max_tokens 必填（Anthropic 协议硬性要求），4096 覆盖训练报告/复盘正文长度。
    任何网络/解析异常向上抛，由 diagnose / analyze_round / parse_review 捕获走降级。
    """
    body = json.dumps({
        "model": model,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
    }).encode("utf-8")
    req = urllib.request.Request(GLM_URL, data=body, method="POST")
    req.add_header("x-api-key", api_key)
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")
    req.add_header("anthropic-version", "2023-06-01")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    # Anthropic Messages 响应：content=[{type:"text", text:"..."}]，取首块文本
    return data["content"][0]["text"]


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
