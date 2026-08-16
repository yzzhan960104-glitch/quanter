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
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# npm 全局安装时 dws 真身相对垫片的固定位置（npm 布局约定）：
# <npm_prefix>\dws.CMD（垫片）+ <npm_prefix>\node_modules\dingtalk-workspace-cli\bin\dws.js（真身）
_DWS_PACKAGE_SUBPATH = ("node_modules", "dingtalk-workspace-cli", "bin", "dws.js")


def _resolve_dws_cmd(markdown: str) -> list[str]:
    """解析 dws 启动命令：Windows npm .cmd 垫片 → 直调 [node, dws.js]。

    根因（2026-08-16 实证）：dws 经 npm 全局安装为 .CMD 批处理垫片，垫片尾部
    ``"%_prog%" "...dws.js" %*`` 由 cmd.exe 展开——**批处理把参数中的换行当命令
    分隔符**，多行 markdown 经垫片后 node 只收到第一行（rc=0、stderr 空，静默
    截断），钉钉群收到正文仅一行标题 → 用户看到「消息内容是空的」。

    修复：垫片路径以 .cmd/.bat 结尾且真身 js + node.exe 均可解析时，绕过垫片
    直调 node——CreateProcess 按 UTF-16 原样传参，多行正文一字不差。
    回退：非 npm 布局（js/node 缺失）退回垫片路径（保留原报错语义）；若正文
    含换行则 WARNING 留观测痕迹（观测层纪律：截断风险不静默）。
    """
    dws_bin = shutil.which("dws") or "dws"
    if dws_bin.lower().endswith((".cmd", ".bat")):
        js = Path(dws_bin).parent.joinpath(*_DWS_PACKAGE_SUBPATH)
        node_bin = shutil.which("node")
        if js.is_file() and node_bin:
            return [node_bin, str(js)]
        if "\n" in markdown:
            logger.warning(
                "dws 走 .cmd 垫片且真身不可解析，多行正文可能被批处理截断（dws_bin=%s js=%s）",
                dws_bin, js,
            )
    return [dws_bin]


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
    # Windows 健壮性：dws 经 npm 全局安装为 .cmd shim，CreateProcess 搜 PATH 时不查
    # PATHEXT（只匹配 dws.exe），直接 Popen "dws" 会 FileNotFoundError [WinError 2]
    # （同源根因见 connect_manager.build_cmd 注释）。用 shutil.which 解析绝对路径
    # （含 .CMD 扩展名），失败回退 "dws" 保留原报错语义。
    # 多行 --text 的垫片截断防护见 _resolve_dws_cmd docstring（2026-08-16 实证）。
    cmd = _resolve_dws_cmd(markdown) + [
        "chat", "message", "send-by-bot",
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
