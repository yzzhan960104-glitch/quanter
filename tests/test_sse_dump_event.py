"""sse_dumps（SSE 序列化统一出口）单元测试。

守住机制层契约：
- 正常事件 → 合法 SSE 帧（data: {...}\\n\\n）
- 含 NaN/Inf 的事件 → 返回 None（让调用方降级），绝不产出含字面 NaN/Infinity 的字符串
- pydantic 模型经 jsonable_encoder 正确序列化为结构化 JSON
"""
from pydantic import BaseModel, ConfigDict, Field

from server.api.v1._sse import sse_dumps


# ============ 测试用桩模型（原 server.schemas.portfolio.NavPoint 的等价替身） ============
# 注：portfolio（HMM 组合回测）已在蔡森专精化 Phase 1·Task 5 整体删除；此处的测试
# 本意只验证 sse_dumps 对「pydantic 模型」的结构化序列化契约，模型来源无关紧要，
# 故就地内联一个等价小模型（含 alias + populate_by_name，覆盖真实字段形态），
# 使本测试不再依赖任何已删的 portfolio schema。
class _NavPointStub(BaseModel):
    """sse_dumps 序列化桩：字段与原 NavPoint 等价（return_ 用 alias 输出 "return"）。"""
    model_config = ConfigDict(populate_by_name=True)

    date: str
    nav: float
    return_: float = Field(alias="return")
    cumulative_return: float


def test_sse_dumps_normal_event_format():
    """正常事件产出标准 SSE 帧格式。"""
    frame = sse_dumps({"type": "progress", "nav": 1.0})
    assert frame is not None
    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")
    assert '"progress"' in frame


def test_sse_dumps_nan_returns_none():
    """NaN 必须令序列化失败 → None（机制层早暴露，不流前端）。"""
    assert sse_dumps({"type": "result", "nav": float("nan")}) is None


def test_sse_dumps_inf_returns_none():
    """Inf 同理必须 → None。"""
    assert sse_dumps({"type": "result", "nav": float("inf")}) is None


def test_sse_dumps_no_nan_or_infinity_literal():
    """任何成功序列化的帧绝不含字面 NaN/Infinity。"""
    frame = sse_dumps({"type": "trade", "price": 10.5, "cost": 5.0})
    assert frame is not None
    assert "NaN" not in frame
    assert "Infinity" not in frame


def test_sse_dumps_pydantic_model_structured():
    """pydantic 模型经 jsonable_encoder 转为结构化 JSON（result 帧场景）。

    回归：原裸 default=str 会把整个模型 str() 成一坨字符串，前端拿不到对象。
    此处仅验证 sse_dumps 对 pydantic 模型的结构化序列化契约，模型来源无关紧要
    （portfolio schema 已随 Task 5 删除，桩模型 _NavPointStub 等价替身）。
    """
    frame = sse_dumps({
        "type": "result",
        "data": _NavPointStub(date="2023-01-01", nav=1.0, return_=0.0, cumulative_return=0.0),
    })
    assert frame is not None
    # 结构化字段可被解析（而非 str(model)）
    assert '"nav": 1.0' in frame
    assert '"date": "2023-01-01"' in frame
