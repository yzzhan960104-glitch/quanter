# -*- coding: utf-8 -*-
"""播报出站：subprocess 调 dws chat message send-by-bot（spec §5.3 · 零自写加签）。

物理意图：dws 全权处理 OAuth 凭证 / 加签 / errcode 校验，本模块只组装命令 + 超时 + 退出码判断。
凭证（robot_code / group_id）由调用方从 .env 传入，本模块不读环境（保持可单测）。

鲁棒性（spec §6）：
- dws 不在 PATH（FileNotFoundError）/ 超时（TimeoutExpired）/ returncode≠0（含 errcode 业务失败）
  → 返 False，绝不抛（由 __main__ 捕获后不写 last_broadcast，下次触发重试）。
"""
from __future__ import annotations

import logging
import subprocess
import sys

logger = logging.getLogger(__name__)


def push_brief(
    title: str,
    markdown: str,
    *,
    robot_code: str,
    group_id: str,
    dry_run: bool = False,
    timeout: int = 30,
) -> bool:
    """调 dws send-by-bot 推一条 Markdown 到群。

    返回：成功 True；缺凭证 / 超时 / dws 不存在 / returncode≠0 → False（不抛）。
    dry_run=True：只打印 markdown 不调 dws，返 True（样例审阅用）。
    """
    if dry_run:
        # Windows 控制台默认 GBK 无法编码 emoji(📈🔺🔻) → 切 UTF-8；
        # capsys 等已替换 stdout（无 reconfigure 方法）→ except 跳过，print 仍正常工作。
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        print(markdown)
        return True
    if not robot_code or not group_id:
        logger.error("push_brief 缺凭证（robot_code/group_id 为空），跳过推送")
        return False
    cmd = [
        "dws", "chat", "message", "send-by-bot",
        "--robot-code", robot_code,
        "--group", group_id,
        "--title", title,
        "--text", markdown,
        "-y",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        logger.error("dws 不在 PATH，推送失败")
        return False
    except subprocess.TimeoutExpired:
        logger.error("dws send-by-bot 超时(>%ss)", timeout)
        return False
    if r.returncode != 0:
        logger.error(
            "dws send-by-bot 失败 returncode=%s stderr=%s",
            r.returncode, (r.stderr or "")[:300],
        )
        return False
    return True
