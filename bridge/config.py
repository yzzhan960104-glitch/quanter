# -*- coding: utf-8 -*-
"""
bridge/config.py
================
钉钉桥全部配置的单一来源。从 .env / 环境变量读取，强类型化。

凭证红线：本模块只读环境变量，绝不硬编码任何 token / secret。
启动期即校验致命前置条件（凭证缺失、白名单为空），失败快、失败响——
优于「跑起来后静默连不上钉钉」或「白名单空 = 谁都连不上 / 全放行无门」。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class BridgeConfig:
    """钉钉桥运行配置（不可变，启动时一次性装配）。"""

    # 钉钉企业内部应用凭证（Stream 接入必需，双值缺一即拒）
    app_key: str
    app_secret: str
    # 白名单 staffId 集合；全放行模式下唯一身份闸，空集 = 致命错
    allowed_staff_ids: frozenset[str]
    # claude CLI 可执行路径，默认 "claude"（走 PATH）
    claude_bin: str
    # claude 工作目录，默认项目根（与终端 claude 一致）
    workdir: str
    # 单轮超时（秒）：claude 卡住时 watchdog kill 的阈值
    ask_timeout: int
    # 空闲进程回收（秒）：超过未用的常驻进程主动 terminate，防进程数无限增长
    idle_ttl: int
    # 单用户每分钟消息上限（钉钉机器人频控 + 防刷）
    rate_limit_per_min: int
    # 会话映射 JSON 落盘路径（conversationId ↔ claude session_id）
    session_store_path: str
    # 全量审计 jsonl 落盘路径（全放行模式的事后追溯底线）
    audit_log_path: str
    # 运行日志路径
    log_path: str

    @classmethod
    def from_env(cls, project_root: str) -> "BridgeConfig":
        """从环境变量构造。致命前置条件不满足直接 ValueError。"""
        app_key = os.getenv("DINGTALK_APP_KEY", "").strip()
        app_secret = os.getenv("DINGTALK_APP_SECRET", "").strip()
        # 凭证缺失 = 无法建 Stream 连接，启动即拒
        if not app_key or not app_secret:
            raise ValueError(
                "DINGTALK_APP_KEY / DINGTALK_APP_SECRET 未配置（检查 .env）；"
                "钉钉企业内部应用凭证双值必填。"
            )

        # 白名单：逗号分隔 → 去空白 → 去空串 → frozenset
        raw_whitelist = os.getenv("DINGTALK_ALLOWED_STAFF_IDS", "")
        allowed = frozenset(
            s.strip() for s in raw_whitelist.split(",") if s.strip()
        )
        # 全放行模式下白名单是唯一身份闸，空集 = 无人能触发，视为配置错
        if not allowed:
            raise ValueError(
                "DINGTALK_ALLOWED_STAFF_IDS 白名单为空；全放行模式下这是唯一身份闸，"
                "必须至少配一个 staffId。"
            )

        # 可选项走默认；ASK_TIMEOUT=120s 留足 claude 思考 + 工具调用时间
        return cls(
            app_key=app_key,
            app_secret=app_secret,
            allowed_staff_ids=allowed,
            claude_bin=os.getenv("CLAUDE_BIN", "claude").strip() or "claude",
            workdir=os.getenv("CLAUDE_WORKDIR", project_root).strip() or project_root,
            ask_timeout=int(os.getenv("BRIDGE_ASK_TIMEOUT", "120")),
            idle_ttl=int(os.getenv("BRIDGE_IDLE_TTL", "900")),
            rate_limit_per_min=int(os.getenv("BRIDGE_RATE_LIMIT_PER_MIN", "10")),
            session_store_path=os.path.join(project_root, "logs", "dingtalk_sessions.json"),
            audit_log_path=os.path.join(project_root, "logs", "dingtalk_bridge_audit.jsonl"),
            log_path=os.path.join(project_root, "logs", "dingtalk_bridge.log"),
        )
