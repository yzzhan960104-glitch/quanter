# -*- coding: utf-8 -*-
"""training_analyzer 训练 loop 的 AI 分析/解析（Spec 3 §6）。

LLM 调用走 infra/llm 外部依赖适配层（ports & adapters）：本模块与
server.services.review_service 均改为依赖 infra（合法单向），不再反向 import server。
- analyze_round：当前轮统计 + 当前 cfg + 历史几轮摘要 → LLM → 自然语言 Markdown 报告。
- parse_review：你的审核文本 + 当前 cfg + 字段 schema → LLM → {cfg_override, action}。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

# LLM 调用走 infra/llm 外部依赖适配层（ports & adapters）。
# 模块级 import get_llm_client：使本模块持有该函数引用，测试 patch.object
# (training_analyzer, "get_llm_client", ...) 生效（与原 patch _call_glm 同理）。
from infra.llm import get_llm_client
from infra.llm.base import LLMConfigError

logger = logging.getLogger(__name__)


def _stats_block(report: Dict[str, Any]) -> str:
    """从 ReplayReport dict 抽关键统计摘要（喂 GLM 看趋势，不带完整 trades 撑爆 context）。"""
    fields = ("n_hits", "win_rate", "avg_rr", "max_drawdown", "annualized_return",
              "avg_holding_bars", "pattern_dist")
    return json.dumps({k: report.get(k) for k in fields if k in report},
                      ensure_ascii=False, default=str)


def analyze_round(report: Dict[str, Any], cfg: Dict[str, Any],
                  history: List[Dict[str, Any]]) -> str:
    """分析单轮回测 → Markdown 报告（表现评估/问题诊断/调参建议）。

    LLM 不可用（缺凭证/调用失败）→ 降级返回「AI 不可用 + 附原始统计」文本，不抛异常。
    训练 loop 不应因 LLM 抖动中断，故任何 GLM 异常都被吞并降级。
    """
    stats = _stats_block(report)
    cfg_str = json.dumps(cfg, ensure_ascii=False, default=str)
    history_str = json.dumps(history[-5:], ensure_ascii=False, default=str)  # 最近 5 轮看趋势
    prompt = f"""你是一位资深量化策略研究员。请基于蔡森形态学策略本轮回测结果与历史趋势，输出 Markdown 训练报告。

## 当前轮回测统计
{stats}

## 当前生效参数（cfg）
{cfg_str}

## 历史轮次统计摘要（看趋势，最近 5 轮）
{history_str}

## 输出要求（严格 Markdown 三段）
### 1. 本轮表现评估
（胜率/盈亏比/回撤是否健康，对比历史趋势是改善还是退化）

### 2. 问题诊断
（亏损来源：哪种形态/哪个参数导致？样本是否足够？）

### 3. 下轮调参建议
（给出具体字段+数值方向，如「min_rr_ratio 提到 2.0」「max_holding_bars 放宽到 20」，但不要给死命令，由人审决定）

请直接输出报告正文。"""
    # LLM 调用走 infra/llm 工厂；凭证缺失(LLMConfigError)或调用失败统一降级，
    # 训练 loop 不应因 LLM 抖动中断（语义同原 _call_glm 异常向上抛）。
    try:
        return get_llm_client().call(prompt)
    except Exception as exc:
        logger.warning("analyze_round GLM 调用失败，降级：%s", exc)
        return (f"## ⚠️ AI 分析降级（GLM 不可用：{type(exc).__name__}）\n\n"
                f"附本轮原始统计供人手判断：\n\n```\n{stats}\n```")


from pydantic import ValidationError


class ParseError(Exception):
    """审核文本解析失败（GLM 返回非 JSON / 字段非法 / 超值域）。

    loop 据此回显报错并回 AWAITING_REVIEW 重等你审核。
    """


# 合法 action 白名单（防 GLM 幻觉造动作）
_ACTIONS = ("rerun", "stop", "reset")


def parse_review(text: str, cfg: Dict[str, Any], strategy_name: str = "neckline") -> Dict[str, Any]:
    """解析你的审核文本 → {cfg_override, action}。

    值域护栏：cfg_override 经策略对应 config_schema 整体校验——
    非法字段名/超 ge/le 抛 ParseError（防 GLM 改不存在的字段或给越界值）。
    strategy_name: "neckline"→NecklineConfig(18维)。
    Task 1.3（caisen 形态退役）：caisen 分支（StrategyConfig 33维）已删，当前仅颈线法。
    GLM 不可用 → 降级抛 ParseError（message 含「请按 改 字段=值 重跑 格式」提示）。
    """
    # 阶段C：按策略选 config_schema（Task 1.3：仅颈线法 NecklineConfig 18 维）
    if strategy_name == "neckline":
        from strategies.neckline_schema import NecklineConfig
        schema = NecklineConfig
    else:
        raise ParseError(
            f"未知 strategy_name={strategy_name!r}（caisen 形态已退役，当前仅支持 'neckline'）")

    prompt = f"""你是参数解析器。把用户的中文审核意图解析为严格 JSON。

## 当前生效参数（cfg，含所有合法字段名与当前值）
{json.dumps(cfg, ensure_ascii=False, default=str)}

## 合法字段名清单（只能改这些字段）
{', '.join(schema.model_fields.keys())}

## 用户审核文本
{text}

## 输出要求（只输出 JSON，不要任何解释）
{{"cfg_override": {{字段名: 新值}}, "action": "rerun"}}

规则：
- cfg_override 只能含上面合法字段名；不改的字段不要出现在 cfg_override 里。
- action 只能是 "rerun"（改参重跑）、"stop"（停止训练）、"reset"（重置回基准 cfg 重跑）。
- 若用户只说停止，cfg_override 给空 {{}}。"""
    try:
        raw = get_llm_client().call(prompt)
    except LLMConfigError as exc:
        raise ParseError("GLM 凭证未配置，请按 `改 字段=值 重跑` 格式手动说明。") from exc
    except Exception as exc:
        raise ParseError(f"GLM 调用失败：{type(exc).__name__}") from exc

    # 1) 解析 JSON（容错：剥可能的 ```json 代码块围栏）
    raw_stripped = raw.strip().strip("`")
    if raw_stripped.lower().startswith("json"):
        raw_stripped = raw_stripped[4:].strip()
    try:
        parsed = json.loads(raw_stripped)
    except json.JSONDecodeError as exc:
        raise ParseError(f"GLM 未返回合法 JSON（{exc.msg}），请重新说明审核意图。") from exc

    action = parsed.get("action", "rerun")
    if action not in _ACTIONS:
        raise ParseError(f"非法 action={action}（合法：rerun/stop/reset）。")

    cfg_override = parsed.get("cfg_override") or {}
    if not isinstance(cfg_override, dict):
        raise ParseError("cfg_override 必须是字段对象。")

    # 2) 值域护栏：按策略 config_schema 校验字段名 + 合并后整体校验（含 ge/le）。
    valid_fields = set(schema.model_fields.keys())
    illegal = set(cfg_override) - valid_fields
    if illegal:
        raise ParseError(f"非法字段（不存在于 {schema.__name__}）：{illegal}")
    try:
        schema(**{**cfg, **cfg_override})
    except ValidationError as exc:
        raise ParseError(f"cfg_override 值域非法：{exc}") from exc

    return {"cfg_override": cfg_override, "action": action}
