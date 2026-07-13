"""StrictJSONResponse（同步端点 NaN 早抛防线）单元测试。

守住契约：NaN/Inf 在 render 阶段抛 ValueError（FastAPI 转 500 早抛），
而非把字面 NaN 推给前端。正常 dict 正常序列化，中文 ensure_ascii=False 原样。
"""
import json

import pytest

from server.core._responses import StrictJSONResponse


def test_strict_json_response_normal_dict():
    """正常 dict 序列化为合法 JSON。"""
    r = StrictJSONResponse(content={"a": 1.0, "b": "中文"})
    assert json.loads(r.body.decode("utf-8")) == {"a": 1.0, "b": "中文"}


def test_strict_json_response_rejects_nan():
    """含 NaN 的 content 必须在构造时抛 ValueError（早抛）。"""
    with pytest.raises(ValueError):
        StrictJSONResponse(content={"nav": float("nan")})


def test_strict_json_response_rejects_inf():
    """含 Inf 的 content 同理抛 ValueError。"""
    with pytest.raises(ValueError):
        StrictJSONResponse(content={"nav": float("inf")})


def test_strict_json_response_chinese_not_escaped():
    """中文原样输出（ensure_ascii=False，与 SSE 路径一致）。"""
    r = StrictJSONResponse(content={"msg": "回测完成"})
    assert "回测完成" in r.body.decode("utf-8")
    assert "\\u" not in r.body.decode("utf-8")  # 未被 ASCII 转义


def test_strict_json_response_handles_datetime():
    """#11：datetime 等非 JSON 原生类型经 jsonable_encoder 转 ISO 字符串，不抛 TypeError。

    物理意图：原 render 直接 json.dumps(content) 绕过 jsonable_encoder，端点返回 datetime/
    Timestamp 会抛 TypeError 致 500。加 jsonable_encoder 防御层后正常转 ISO 字符串。
    """
    import datetime
    r = StrictJSONResponse(content={"ts": datetime.datetime(2024, 1, 2, 3, 4, 5)})
    body = json.loads(r.body.decode("utf-8"))
    assert body["ts"] == "2024-01-02T03:04:05"   # jsonable_encoder：datetime → ISO 字符串
