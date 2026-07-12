# -*- coding: utf-8 -*-
"""
bridge/__main__.py
=================
钉钉桥入口：`python -m bridge`。

职责（装配顺序，每步的 Why 见行内注释）：
  1. load_dotenv           — 从 .env 读凭证（凭证隔离红线：绝不硬编码进源码）
  2. BridgeConfig.from_env — 启动期即校验致命前置条件（凭证缺失/白名单空 = 快失败）
  3. _setup_logging        — 控制台 + 文件双路日志（运维观测 + 事后追溯）
  4. Windows ProactorEventLoop — asyncio.subprocess 在 Windows 需 Proactor 才能拉子进程
  5. build_and_run         — 装配 dingtalk-stream 客户端 + Handler 并阻塞运行

入口与 stream_client.build_and_run 的边界：本文件只做"环境与配置"装配，
不感知 dingtalk SDK / claude 细节；真正的运行循环在 build_and_run 里。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

# 项目根（bridge/__main__.py → bridge/ → 项目根）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _setup_logging(cfg) -> None:
    """装配控制台 + 文件双路日志。

    Why 双路：
      - 控制台：开发期/前台跑时实时观测；
      - 文件：后台守护进程场景事后追溯（logs/dingtalk_bridge.log）。
    Why utf-8 FileHandler：Windows 控制台默认 GBK，写中文会乱码/抛 UnicodeError；
      FileHandler 强制 utf-8 保证审计/诊断文本可读、可跨平台。
    Why 先 makedirs：日志目录可能首次启动时尚不存在（与审计/会话 store 同一目录）。
    """
    fmt = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    log_dir = os.path.dirname(cfg.log_path)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    # 追加而非覆写：每次启动重置日志会丢上一轮的崩溃前线索
    file_handler = logging.FileHandler(cfg.log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(fmt))
    logging.getLogger().addHandler(file_handler)


def main() -> None:
    """入口主函数（被 __main__ 与 scripts/dingtalk_claude_bridge.py 共用）。"""
    # .env 加载（凭证隔离红线：绝不硬编码 token/secret）
    # 延迟 import dotenv：让 `python -c "from bridge..."` 单元冒烟不强制装 python-dotenv
    # （实际跑入口时 requirements.txt 已含 python-dotenv）
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")

    from bridge.config import BridgeConfig
    # from_env 在凭证缺失/白名单空时直接 ValueError——快失败优于跑起来静默连不上
    cfg = BridgeConfig.from_env(project_root=str(PROJECT_ROOT))

    _setup_logging(cfg)
    logger = logging.getLogger("bridge")
    logger.info(
        "钉钉桥配置完成：workdir=%s, 白名单=%d 人, 全放行模式(bypassPermissions)",
        cfg.workdir, len(cfg.allowed_staff_ids),
    )

    # Windows asyncio.subprocess 必须 ProactorEventLoop（Python 3.8+ Windows 默认即此）；
    # 显式 set_event_loop_policy 防止某些环境下 policy 被 SelectorEventLoop 覆盖
    # （SelectorEventLoop 不支持 create_subprocess_exec，会抛 NotImplementedError）。
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    from bridge.stream_client import build_and_run
    # build_and_run 内部 asyncio.run 阻塞——本函数到此即"主循环"
    build_and_run(cfg)


if __name__ == "__main__":
    main()
