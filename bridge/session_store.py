# -*- coding: utf-8 -*-
"""
bridge/session_store.py
=======================
钉钉 conversationId ↔ claude session_id 的持久化映射。

Why 必须落盘：常驻 claude 进程会死（崩溃/超时杀/空闲回收），但 claude 把
完整会话历史存在本地 ~/.claude/。只要拿着 session_id，下次 --resume <sid>
即可续上下文。故 session_id 必须独立于进程生命周期持久化——进程死、映射在，
上下文不丢。

原子写：写临时文件再 os.replace，防并发写坏 JSON（多个异步任务可能同时 set）。
文件锁：threading.Lock 保护内存 dict + 落盘的临界区。
损坏容错：手改/截断的 JSON 不应让桥启动失败，按空映射起步。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from typing import Optional

logger = logging.getLogger(__name__)


class SessionStore:
    """conversationId → claude session_id 映射，内存 dict + JSON 落盘。"""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._map: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        """启动时从磁盘载入；文件缺失/损坏按空映射起步。"""
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
            logger.warning("session_store 文件非 dict，按空映射起步：%s", self._path)
            return {}
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError) as e:
            # 损坏不致命：记日志 + 空映射，优于启动失败把整桥拉倒
            logger.warning("session_store 文件读取失败，按空映射起步：%s (%s)", self._path, e)
            return {}

    def get(self, conv_id: str) -> Optional[str]:
        with self._lock:
            return self._map.get(conv_id)

    def set(self, conv_id: str, session_id: str) -> None:
        with self._lock:
            self._map[conv_id] = session_id
            self._flush_locked()

    def clear(self, conv_id: str) -> None:
        """/new 指令用：清掉单个会话映射，下次走全新会话。"""
        with self._lock:
            self._map.pop(conv_id, None)
            self._flush_locked()

    def _flush_locked(self) -> None:
        """原子落盘：临时文件 → os.replace（持有锁时调用）。"""
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        # delete=False + 手动清理：Windows 上 NamedTemporaryFile 默认 delete=True
        # 会锁文件导致 os.replace 失败，故用 delete=False 再手动删
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self._path) or ".",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._map, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            # 落盘失败不回滚内存（下次 set 重试）；记日志，不阻断业务
            logger.exception("session_store 落盘失败：%s", self._path)
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
