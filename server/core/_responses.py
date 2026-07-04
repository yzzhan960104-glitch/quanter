# -*- coding: utf-8 -*-
"""严格 JSON 响应类（同步端点防 NaN/Inf 的机制层防线）。

背景：FastAPI 默认 JSONResponse 用标准库 json，allow_nan=True，对 NaN/Inf 不设防，
会输出字面 NaN（非法 JSON）。浏览器 JSON.parse 遇 NaN 必败，前端若 catch 静默吞
响应，则表现为结果不显示。本类用 allow_nan=False 让同步端点对 NaN 当场抛错
（早抛 500 + 中文错误），比前端静默吞强；service 层 _safe_float 已堵住正常路径，
本类为最后防线（任何漏标量化的路径在这里暴露）。

对称防线：server/api/v1/_sse.py 的 sse_dumps 守 SSE 流式端点，本类守同步端点。
"""
from __future__ import annotations

import json
from typing import Any

from fastapi.responses import JSONResponse


class StrictJSONResponse(JSONResponse):
    """allow_nan=False 的 JSONResponse：NaN/Inf 在 render 阶段抛 ValueError。

    挂到 FastAPI(default_response_class=StrictJSONResponse) 即对所有同步端点生效
    （见 server/main.py）。正常路径（service 层 _safe_float 已清洗数值）不会触发；
    一旦触发即说明有路径漏标量化，早抛 500 + 中文错误，便于定位，而非把非法
    JSON 推给前端静默吞。
    """

    def render(self, content: Any) -> bytes:
        # allow_nan=False：NaN/Inf 在此抛 ValueError，FastAPI 转 500 响应（早抛）。
        # ensure_ascii=False：与 SSE 路径（sse_dumps）一致，中文（symbol/错误信息）原样输出。
        return json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
