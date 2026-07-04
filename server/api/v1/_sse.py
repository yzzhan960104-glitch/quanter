# -*- coding: utf-8 -*-
"""SSE 事件序列化统一出口（防 NaN/Inf 流入前端的机制层防线）。

背景（2026-07「K 线不显示」根因）：json.dumps 默认 allow_nan=True，对 NaN/Inf
不设防，会输出字面 NaN（非法 JSON）。浏览器 JSON.parse 遇 NaN 必败，前端
useTerminalState 的 catch{return} 静默吞帧，表现为「K 线/买卖点不显示」等
极难定位的空白。本模块用 allow_nan=False 让 NaN 在后端序列化时当场抛
ValueError，由调用方决定降级（result 帧→error 帧 / 日志帧→跳过），绝不产出
非法 JSON 到前端。

对称防线：server/core/_responses.py 的 StrictJSONResponse 守同步端点，
本模块守 SSE 流式端点。两道防线共同保证「NaN 不可能以字面形式到达浏览器」。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi.encoders import jsonable_encoder

logger = logging.getLogger(__name__)

# SSE 帧结尾标记（双换行），EventSource 据此分帧；抽常量避免调用方拼错
_FRAME_SUFFIX = "\n\n"


def sse_dumps(ev: Any, log: Optional[logging.Logger] = None) -> Optional[str]:
    """把单个 SSE 事件序列化为 `data: {...}\\n\\n` 字符串。

    机制层核心：allow_nan=False —— 等价浏览器 JSON.parse 严格模式，让 NaN/Inf
    在后端当场抛 ValueError（而非流到前端被静默吞）。

    参数：
        ev: 任意可被 jsonable_encoder 处理的事件（dict / pydantic 模型 / 原生类型）。
        log: 可选日志器；序列化失败时记 error（含事件类型 + 原因），便于本地定位。
            缺省用本模块 logger。

    返回：
        合法 SSE 帧字符串；若 ev 含 NaN/Inf 或不可序列化，返回 None（调用方降级）。

    降级契约（调用方各自处理）：
        - backtest result 帧 → 返回 None 时转 {"type":"error",...} 帧（前端明确报错）
        - logs 日志帧 → 返回 None 时 continue（跳过该帧，不崩日志流）
    """
    lg = log or logger
    try:
        # jsonable_encoder：把 pydantic 模型 / numpy 标量 / datetime / Decimal 递归
        # 转成 JSON 原生类型（dict/list/str/float），否则裸 default=str 会把整个
        # 模型 str() 成一坨字符串，前端拿不到结构化对象。
        payload = jsonable_encoder(ev)
        # allow_nan=False：NaN/Inf 在此抛 ValueError —— 机制层防线，与浏览器 JSON.parse 对齐
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError) as e:
        # 失败必须留痕：事件类型 + 原因，下次同类问题本地日志一眼定位
        ev_type = ev.get("type") if isinstance(ev, dict) else type(ev).__name__
        lg.error(
            "SSE 事件序列化失败（含 NaN/Inf 或不可序列化）：事件类型=%s，原因=%s",
            ev_type,
            e,
        )
        return None
    return f"data: {body}{_FRAME_SUFFIX}"
