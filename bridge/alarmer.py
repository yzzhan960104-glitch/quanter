# -*- coding: utf-8 -*-
"""
bridge/alarmer.py
=================
高危工具调用实时告警（全放行模式的纵深防御③：事中知情）。

订阅 ClaudeProcess.ask 的 on_event 事件流，遇到工具调用（tool_use）且命中
敏感模式时，立即把「谁、调了什么工具、参数」推钉钉告警给用户自己。

Why 事后而非事前：bypassPermissions 下工具自动执行，stream-json 事件是
"正在执行/已完成"的通知，看到时已动手（除非用 SDK hook，本项目刻意不引）。
故本模块只能"事后审计变事中知情"——给用户一个及时的预警窗口，而非拦截。

字段依据：Task 7 Step 0 实测 claude CLI v2.1.190 stream-json 真实帧：
    {"type":"assistant",
     "message":{"content":[
         {"type":"thinking","thinking":"..."},
         {"type":"tool_use","id":"call_xxx","name":"Read",
          "input":{"file_path":"...","limit":1}}
     ]}}
工具名在 name，参数在 input。本模块据此实现 _extract_tool_use。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# 告警投递函数签名：(msg, level) -> Any。默认接 core.notifier。
# 注：返回值不用 await——core.notifier.fire_and_forget 在内部起 daemon 线程跑协程，
# 对调用方表现为同步返回。这样 alarmer.check_event 保持纯同步语义。
NotifyFn = Callable[[str, str], object]

# 高危模式（正则；命中任一即告警）。按类分组便于告警文案归类与后续扩展。
#
# Why 这些模式：覆盖"可能造成不可逆后果"的工具调用——
#   - 实盘交易路径：trading/、emt_gateway、qmt_、xtquant（实盘 SDK 模块名）
#     一旦被 claude 改动实盘代码/调用实盘下单接口，必须立即让用户知情。
#   - 凭证文件：.env（含钉钉加签 secret、API token，泄露即被冒充）
#   - 破坏性命令：rm、git push（推到远端不可撤回）、git reset、--force、mkfs、dd
#   - 网络外传：curl/wget/scp/nc（数据外泄通道）
#   - 下单函数：place_order/insert_order/activate_plan/order_place（业务下单入口）
# 命中即告警，不拦截——权衡：误报可接受（多一条告警），漏报不可接受（实盘被误操作）。
_DANGER_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("实盘交易路径", re.compile(r"trading/|emt_gateway|qmt_|xtquant", re.IGNORECASE)),
    # Why 精确化为「前后均非 [\w.]」：原 `\.env\b` 因 `.` 非词字符、`\b` 即边界，
    # 会误命中 `.env.example`/`.envrc`/`dotenv` 等无真值的模板或非凭证路径，
    # 读这些模板会误告警 → 告警疲劳 → 真高危被忽略。
    # 现仅在独立 `.env` 文件路径（path/.env、/.env、行首 .env）上命中。
    ("凭证文件", re.compile(r"(?<![\w.])\.env(?![\w.])", re.IGNORECASE)),
    ("破坏性命令", re.compile(r"\brm\b\s|git\s+push|git\s+reset|--force|mkfs|dd\s+if=", re.IGNORECASE)),
    # Why 双侧 \b：原 `scp\b|nc\b` 缺左侧 `\b`，`nc` 作为子串 + 右边界即命中
    # `sync`/`func`/`localStorage`/`javascript` 等普通代码（误告警）。
    # 现四词均加双侧 `\b`，只匹配独立命令名。
    ("网络外传", re.compile(r"\bcurl\b|\bwget\b|\bscp\b|\bnc\b", re.IGNORECASE)),
    ("下单函数", re.compile(r"place_order|insert_order|activate_plan|order_place", re.IGNORECASE)),
]


class Alarmer:
    """检查单条 stream-json 事件，命中高危模式即推告警。

    线程安全：无内部可变状态，可被多个 ClaudeProcess 的事件回调共享调用。
    """

    def __init__(self, notify: Optional[NotifyFn] = None) -> None:
        # 默认接 core.notifier 的 fire_and_forget + notify_risk_event
        # 延迟绑定：notify=None 时才走 _default_notify，便于测试注入 mock
        if notify is None:
            notify = _default_notify
        self._notify = notify

    def check_event(self, event: dict, sender_staff_id: str) -> None:
        """检查一个事件。命中高危即异步推告警（不阻塞 claude 主流程）。

        Args:
            event: claude stream-json 的一行解析后的 dict。
            sender_staff_id: 触发者的钉钉 staff_id（谁在驱动 claude）。
        """
        tool_name, tool_input = _extract_tool_use(event)
        if tool_name is None:
            return  # 非工具调用帧（thinking/result/system），忽略
        # 把工具参数序列化为文本供模式匹配——命令/路径/函数名都在 input 里，
        # 把 tool_name 也拼进来覆盖"Bash 工具名本身即危险"的极端场景。
        blob = f"{tool_name} " + json.dumps(tool_input or {}, ensure_ascii=False)
        for label, pattern in _DANGER_PATTERNS:
            if pattern.search(blob):
                self._fire(sender_staff_id, tool_name, tool_input, label, blob)
                return  # 一个工具调用告一次即可，不重复刷屏

    def _fire(
        self,
        sender: str,
        tool: str,
        tool_input: dict,
        label: str,
        blob: str,
    ) -> None:
        """构造告警文案并投递。notify 异常被吞，保护 claude 主流程。"""
        # 文案截断到 300 字符——钉钉 Markdown 单条有长度上限，
        # 且过长参数（如整个文件内容）会刷屏遮蔽关键信息。
        msg = (
            f"⚠️ 钉钉桥检测到 claude 执行高危操作\n"
            f"触发者: {sender}\n"
            f"风险类: {label}\n"
            f"工具: {tool}\n"
            f"参数: {blob[:300]}"
        )
        try:
            # notify 默认走 fire_and_forget，本身不阻塞
            self._notify(msg, "WARN")
        except Exception:  # noqa: BLE001
            # 告警投递失败不能拖垮 claude 主流程——记日志即可
            logger.exception("高危告警投递失败")


def _extract_tool_use(event: dict) -> tuple[Optional[str], Optional[dict]]:
    """从 assistant 帧取首个 tool_use 项的 (name, input)。无则 (None, None)。

    字段以 Task 7 Step 0 实测为准：content 数组里
        {"type":"tool_use","id":"call_xxx","name":"Read","input":{...}}
    一个 assistant 帧可能同时含 thinking + tool_use（实测如此），故遍历找首个
    type==tool_use 的 block，而非假设它在固定下标。
    """
    content = event.get("message", {}).get("content", [])
    if not isinstance(content, list):
        return None, None
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            return block.get("name"), block.get("input")
    return None, None


def _default_notify(msg: str, level: str) -> None:
    """默认投递：复用 core.notifier 的 fire_and_forget + notify_risk_event。

    Why 不在 __init__ 直接 import core.notifier：
      1) 测试注入 notify mock 时，core.notifier 完全不被加载（隔离网络副作用）；
      2) 延迟 import 打破潜在的循环依赖（core.notifier 是底层基建，被多方引用）。
    fire_and_forget 内部起 daemon 线程跑 asyncio.run，对调用方表现为同步返回。
    """
    try:
        from core.notifier import fire_and_forget, NotificationManager
        mgr = NotificationManager.get_default()
        # notify_risk_event 是 async，fire_and_forget 把它后台调度不阻塞
        fire_and_forget(mgr.notify_risk_event(msg, level=level))  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        # notifier 装配失败（如缺凭证）不能让 alarmer 崩——告警丢失记日志即可
        logger.exception("notifier 装配失败，告警丢失")
