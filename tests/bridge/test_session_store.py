# -*- coding: utf-8 -*-
"""SessionStore 持久化测试：读写/清空/原子写/文件不存在时的容错。"""
import json
from pathlib import Path

from bridge.session_store import SessionStore


def test_get_returns_none_when_missing(tmp_path):
    """文件不存在时 get 返回 None（首次启动常态，不报错）。"""
    store = SessionStore(str(tmp_path / "nope.json"))
    assert store.get("convA") is None


def test_set_then_get_roundtrip(tmp_path):
    """set 后 get 能取回；同一会话覆写更新。"""
    path = tmp_path / "s.json"
    store = SessionStore(str(path))
    store.set("convA", "sid-1")
    assert store.get("convA") == "sid-1"
    # 覆写
    store.set("convA", "sid-2")
    assert store.get("convA") == "sid-2"


def test_set_persists_to_disk(tmp_path):
    """落盘可被新实例读到（进程重启后 session_id 不丢，--resume 可续）。"""
    path = tmp_path / "s.json"
    SessionStore(str(path)).set("convA", "sid-1")
    # 新实例从同一文件读
    assert SessionStore(str(path)).get("convA") == "sid-1"
    # 文件内容是合法 JSON
    assert json.loads(path.read_text(encoding="utf-8")) == {"convA": "sid-1"}


def test_clear_removes_mapping(tmp_path):
    """clear 清掉单个会话映射（/new 指令用），不影响其他会话。"""
    store = SessionStore(str(tmp_path / "s.json"))
    store.set("convA", "sid-a")
    store.set("convB", "sid-b")
    store.clear("convA")
    assert store.get("convA") is None
    assert store.get("convB") == "sid-b"


def test_corrupt_file_tolerated(tmp_path):
    """文件损坏（手改/截断）时不炸，按空映射启动（优于启动失败）。"""
    path = tmp_path / "s.json"
    path.write_text("{坏了的 json", encoding="utf-8")
    store = SessionStore(str(path))
    assert store.get("anything") is None
