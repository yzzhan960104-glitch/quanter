# -*- coding: utf-8 -*-
"""claude_events 解析测试：用真实抓到的帧结构做 fixture（见 Global Constraints）。"""
import json

from bridge.claude_events import (
    extract_assistant_text,
    extract_result_text,
    extract_session_id,
    is_result,
    make_user_frame,
    parse_event_line,
)

# 真实抓到的 assistant 帧（2026-07-12 实测）
ASSISTANT_FRAME = (
    '{"type":"assistant","message":{"content":'
    '[{"type":"text","text":"pong"}]},"session_id":"sid-abc","uuid":"u1"}'
)
# 真实抓到的 result 帧
RESULT_FRAME = (
    '{"type":"result","subtype":"success","is_error":false,'
    '"result":"pong","session_id":"sid-abc",'
    '"permission_denials":[]}'
)
# 噪音帧（必须被忽略，不解析为文本）
THINKING_FRAME = '{"type":"system","subtype":"thinking_tokens","estimated_tokens":99}'
INIT_FRAME = '{"type":"system","subtype":"init","cwd":"/p","session_id":"sid-abc"}'


def test_make_user_frame_is_valid_json_single_line():
    """构造的输入帧是合法 JSON、单行、含 user 类型。"""
    frame = make_user_frame("你好")
    assert "\n" not in frame
    obj = json.loads(frame)
    assert obj["type"] == "user"
    assert obj["message"]["role"] == "user"
    assert obj["message"]["content"][0]["text"] == "你好"


def test_parse_event_line_handles_garbage():
    """空行/非 JSON 返回 None（claude 偶发输出非 JSON 行不炸解析器）。"""
    assert parse_event_line("") is None
    assert parse_event_line("not json") is None
    assert parse_event_line(ASSISTANT_FRAME)["type"] == "assistant"


def test_is_result_only_true_for_result():
    assert is_result(parse_event_line(RESULT_FRAME)) is True
    assert is_result(parse_event_line(ASSISTANT_FRAME)) is False
    assert is_result(parse_event_line(THINKING_FRAME)) is False


def test_extract_result_text():
    ev = parse_event_line(RESULT_FRAME)
    assert extract_result_text(ev) == "pong"


def test_extract_session_id_from_any_frame():
    """session_id 在 assistant/result/init 帧顶层都有。"""
    assert extract_session_id(parse_event_line(ASSISTANT_FRAME)) == "sid-abc"
    assert extract_session_id(parse_event_line(RESULT_FRAME)) == "sid-abc"
    assert extract_session_id(parse_event_line(INIT_FRAME)) == "sid-abc"
    # 无 session_id 的帧返回 None
    assert extract_session_id(parse_event_line(THINKING_FRAME)) is None


def test_extract_assistant_text_concatenates_text_blocks():
    """assistant 帧的 content 可能有多个 text 项（文本+工具调用混合），全部拼接。"""
    ev = json.loads(
        '{"type":"assistant","message":{"content":'
        '[{"type":"text","text":"a"},{"type":"text","text":"b"}]}}'
    )
    assert extract_assistant_text(ev) == "ab"


def test_extract_assistant_text_ignores_non_text_content():
    """content 里的 tool_use 项不贡献文本（避免把工具调用 JSON 当文本回钉钉）。"""
    ev = json.loads(
        '{"type":"assistant","message":{"content":['
        '{"type":"text","text":"看这个文件："},'
        '{"type":"tool_use","name":"Read","input":{"path":"x.py"}}'
        ']}}'
    )
    assert extract_assistant_text(ev) == "看这个文件："


def test_extract_result_text_null_returns_empty():
    """is_error turn 的 result:null 不应回字面量 'None'。"""
    ev = {"type": "result", "result": None, "is_error": True}
    assert extract_result_text(ev) == ""
