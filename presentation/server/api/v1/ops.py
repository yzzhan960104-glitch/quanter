# -*- coding: utf-8 -*-
"""B2-3：进程拓扑观测端点 GET /api/v1/ops/processes（spec §4.2）。

物理意图：进程/网关/miniQMT 三域问题反复出现，但没有任何一屏视图能同时看到
「引擎 pid / 端口属主 / pid 文件 / session 锁 / 客户端 / 队列大小 / 网关态」——
漂移只能靠翻日志。本端点把 B1 supervisor 的三合一拓扑 + 客户端探测 + 队列大小 +
网关四态合到一屏，漂移即见（drifts 字段）。
"""
from __future__ import annotations

import glob
import os

from fastapi import APIRouter

# W1-A/T2：trading_service 已下沉至 trading/gateway_service.py（切断 presentation 反查）。
# 保局部名 trading_service 以最小化本文件调用方改动（get_status 等），行为零变更。
from trading import gateway_service as trading_service

router = APIRouter(tags=["ops"])


def _queue_size(userdata: str | None = None) -> int:
    """down_queue_win_* 残留队列总字节数（W1.3 清理效果的观测腿）。"""
    userdata = userdata or os.environ.get("QMT_USERDATA_PATH", "")
    total = 0
    if userdata and os.path.isdir(userdata):
        for f in glob.glob(os.path.join(userdata, "down_queue_win_*")):
            try:
                total += os.path.getsize(f)
            except OSError:
                continue
    return total


@router.get("/processes", summary="进程拓扑一屏视图")
async def processes() -> dict:
    """返回 supervisor.status() + 队列大小 + 网关态（同步薄包装，无 IO 阻塞点）。"""
    # 延迟 import：避免路由导入期拉起 ops/trading_supervisor 的 dotenv/subprocess 链
    from ops import trading_supervisor
    st = trading_supervisor.status()
    st["queue_size"] = _queue_size()
    st["gateway_mode"] = trading_service.get_status()
    return st
