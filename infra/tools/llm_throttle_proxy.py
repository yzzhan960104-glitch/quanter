# -*- coding: utf-8 -*-
"""LLM 请求节流代理（透明反向代理）：claude ↔ z.ai 之间插一层，转发前按策略 sleep。

物理意图：z.ai coding plan 服务端在 agent 连续密集调 LLM（重自主任务）时返回
529 overloaded。本代理在每次转发前强制最小请求间隔（降 RPM，治主因）+ 检测到上游
529 时切更长 cooldown（自适应过载窗口），上游恢复后回退——把"撞墙"变成"主动让速"，
用"更慢"换"不撞墙"。只给机器人走（启动时注入 ANTHROPIC_BASE_URL 指向本代理），
本地 terminal 直连 z.ai 不受影响。

结构：
- ThrottlePolicy：纯逻辑节流策略（TDD 由 tests/test_llm_throttle_proxy.py 驱动）。
- aiohttp 流式反代 main：透传 header/body/SSE，转发前调 ThrottlePolicy；由集成冒烟
  infra/tools/smoke_throttle_proxy.py 验证（网络胶水用集成测试，节流内核用纯单测）。

启动：
  python infra/tools/llm_throttle_proxy.py [--port 8787] [--min-interval 8] \\
      [--overload-cooldown 20] [--upstream https://api.z.ai/api/anthropic]
机器人接入（重启 dws connect 时注入，不动全局 settings）：
  ANTHROPIC_BASE_URL=http://127.0.0.1:8787 dws dev connect --unified-app-id ... --channel claudecode ...
"""
from __future__ import annotations

import argparse
import asyncio
import os
import time

from aiohttp import ClientError, ClientSession, ClientTimeout, web


# ===================================== 节流策略 =====================================
class ThrottlePolicy:
    """LLM 请求节流策略：最小请求间隔 + 上游 529 过载退避 + 成功恢复。

    - next_sleep(now)：转发前调用，返回应 sleep 秒数（≥0）；首次请求 0。
    - record(now, upstream_status)：请求完成后调用，更新"上次发起时刻"与过载态。

    间隔口径=两次"发起时刻"之差（真实请求频率的倒数）。529 → 过载态用 overload_cooldown；
    任意非 529（含 200/4xx/其他 5xx）→ 解除过载回 min_interval（避免普通抖动误退避）。
    """

    def __init__(self, min_interval: float = 8.0, overload_cooldown: float = 20.0) -> None:
        self.min_interval = min_interval
        self.overload_cooldown = overload_cooldown
        self._last_call_ts: float | None = None
        self._in_overload: bool = False

    def next_sleep(self, now: float) -> float:
        """返回转发前应等待的秒数。首次请求或间隔已满足 → 0。"""
        if self._last_call_ts is None:
            return 0.0
        elapsed = now - self._last_call_ts
        interval = self.overload_cooldown if self._in_overload else self.min_interval
        return max(0.0, interval - elapsed)

    def record(self, now: float, upstream_status: int) -> None:
        """记录本次请求结果：now=发起时刻，upstream_status=上游 HTTP 状态码。"""
        self._last_call_ts = now
        self._in_overload = upstream_status == 529


# ===================================== 流式反代 =====================================
# 响应头剔除集：hop-by-hop（RFC 7230）+ 长度/编码类。StreamResponse 自管 chunked，
# 预设 content-length/transfer-encoding/content-encoding 会与流式写冲突，必须剔除。
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    "content-length", "content-encoding",
}

# 全局单例：节流是对上游 z.ai 的总 RPM 控制，跨所有 claude 子进程请求合并计数
# （这正是"降总密度"的本意——4 个机器人的 LLM 请求合起来受 min_interval 约束）。
_policy: ThrottlePolicy | None = None
_session: ClientSession | None = None
_UPSTREAM: str = ""


def _log(msg: str) -> None:
    """结构化单行日志（时间戳 + 消息），便于 grep 排查节流/透传/退避行为。"""
    print(f"[throttle-proxy] {time.strftime('%H:%M:%S')} {msg}", flush=True)


def _filter_resp_headers(headers) -> dict[str, str]:
    """剔除 hop-by-hop/长度编码类头，其余（含 content-type）原样透传。"""
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


async def _handle(request: web.Request) -> web.StreamResponse:
    """核心：节流 → 透传请求 → 流式回写响应 → 记录上游状态。"""
    # 健康端点（不节流、不转发）：供冒烟/重启前探活
    if request.path == "/__health":
        assert _policy is not None
        return web.json_response({"ok": True, "in_overload": _policy._in_overload})

    assert _policy is not None and _session is not None and _UPSTREAM

    # 1) 转发前节流：距上次发起不足间隔 → sleep 补足（529 过载态用更长 cooldown）
    sleep_s = _policy.next_sleep(time.time())
    if sleep_s > 0:
        _log(f"{request.method} {request.path} throttle sleep={sleep_s:.2f}s")
        await asyncio.sleep(sleep_s)

    # 2) 透传：method/path/query/body 全保留；仅剔除 Host（上游拒绝错位 Host，aiohttp 会重算）
    t_fire = time.time()
    upstream_url = _UPSTREAM + request.path_qs
    body = await request.read()
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}

    try:
        async with _session.request(request.method, upstream_url, data=body, headers=fwd_headers) as up:
            status = up.status
            _policy.record(t_fire, status)  # 529 → 下次切 overload_cooldown；非 529 → 解除退避
            _log(f"{request.method} {request.path} -> upstream status={status}")

            # 3) 流式回写（SSE/普通响应统一逐块转发，不 buffer 整包——claude 边收边处理）
            resp = web.StreamResponse(status=status, headers=_filter_resp_headers(up.headers))
            await resp.prepare(request)
            async for chunk in up.content.iter_any():
                await resp.write(chunk)
            await resp.write_eof()
            return resp
    except ClientError as e:
        # 上游不可达：记 502（非 529，不误触发过载退避），返 502 让 claude 按其重试逻辑处理
        _policy.record(t_fire, 502)
        _log(f"{request.method} {request.path} -> upstream ClientError: {e}")
        return web.json_response({"error": "upstream unreachable", "detail": str(e)}, status=502)


async def _on_startup(app: web.Application) -> None:
    """建全局 ClientSession（连接池复用，存活到 cleanup）。"""
    global _session
    # total=None：流式响应不限总时长——claude 长任务（几分钟生成）不能被 proxy 超时切断
    _session = ClientSession(timeout=ClientTimeout(total=None))


async def _on_cleanup(app: web.Application) -> None:
    if _session is not None:
        await _session.close()


def build_app(*, upstream: str, min_interval: float, overload_cooldown: float) -> web.Application:
    """构造 aiohttp app（冒烟可经 test_utils 直连验证，不必绑端口）。

    Why 抽 build_app：把"app 装配"与"绑端口运行"分离——集成测试/冒烟可直接拿 app 用
    aiohttp 测试客户端打实，不必真起 socket（更快、更稳、可断言节流时序）。
    """
    global _policy, _UPSTREAM
    _policy = ThrottlePolicy(min_interval=min_interval, overload_cooldown=overload_cooldown)
    _UPSTREAM = upstream.rstrip("/")
    app = web.Application(client_max_size=1024 * 1024 * 64)  # 64MB：claude 大上下文请求体上限放宽（默认 1MB 会 413）
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    app.router.add_route("*", "/{tail:.*}", _handle)  # 通配所有路径（/v1/messages 等）转发到 upstream 同 path
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM 节流反向代理（claude ↔ z.ai）")
    parser.add_argument("--host", default=os.getenv("LLM_PROXY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("LLM_PROXY_PORT", "8787")))
    parser.add_argument("--upstream", default=os.getenv("LLM_PROXY_UPSTREAM", "https://api.z.ai/api/anthropic"))
    parser.add_argument("--min-interval", type=float, default=float(os.getenv("LLM_PROXY_MIN_INTERVAL", "8.0")))
    parser.add_argument("--overload-cooldown", type=float, default=float(os.getenv("LLM_PROXY_OVERLOAD_COOLDOWN", "20.0")))
    args = parser.parse_args()

    app = build_app(upstream=args.upstream, min_interval=args.min_interval, overload_cooldown=args.overload_cooldown)
    _log(f"listen={args.host}:{args.port} upstream={args.upstream} "
         f"min_interval={args.min_interval}s overload_cooldown={args.overload_cooldown}s")
    web.run_app(app, host=args.host, port=args.port, print=None)  # print=None 关 aiohttp 启动横幅（自管 _log）


if __name__ == "__main__":
    main()
