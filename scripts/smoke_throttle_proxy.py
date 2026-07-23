# -*- coding: utf-8 -*-
"""llm_throttle_proxy 集成冒烟：真起 proxy + 假上游，端到端验证透传/节流/529 退避。

物理意图：节流内核 ThrottlePolicy 已由纯单测覆盖；本脚本只验"网络胶水"——aiohttp 流式
透传是否正确、转发前 sleep 是否真执行、上游 529 是否真触发更长退避。用短间隔(0.3/0.8s)
快速验证时序，生产用 8/20s。

跑法：PYTHONUTF8=1 python scripts/smoke_throttle_proxy.py
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

import llm_throttle_proxy as proxy


async def _make_upstream(status_sequence: list[int]) -> web.Application:
    """假上游：按请求计数返回 status_sequence 循环状态，并回显请求体（验透传）。"""
    state = {"i": 0}

    async def handle(req: web.Request) -> web.Response:
        i = state["i"]
        state["i"] += 1
        status = status_sequence[i % len(status_sequence)]
        body = await req.text()
        return web.json_response({"i": i, "status_sent": status, "echo": body}, status=status)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handle)
    return app


async def _timed_post(client: ClientSession, url: str, payload: dict) -> tuple[int, float, dict]:
    """打一次 proxy，返回 (上游 status, 本次请求总耗时, body)。耗时含 proxy 的节流 sleep。"""
    t0 = time.time()
    async with client.post(url, json=payload) as r:
        status = r.status
        body = await r.json()
    return status, time.time() - t0, body


async def main() -> int:
    # 序列设计：[1]200 无历史不等 → [2]529 触发退避 → [3]200 此时仍处过载态应等更长
    upstream_app = await _make_upstream([200, 529, 200, 200])
    upstream_srv = TestServer(upstream_app)
    await upstream_srv.start_server()
    upstream_url = str(upstream_srv.make_url("")).rstrip("/")

    proxy_app = proxy.build_app(upstream=upstream_url, min_interval=0.3, overload_cooldown=0.8)
    proxy_srv = TestServer(proxy_app)
    await proxy_srv.start_server()

    failures: list[str] = []
    try:
        async with ClientSession() as cli:
            base = str(proxy_srv.make_url(""))
            s1, dt1, b1 = await _timed_post(cli, base + "/v1/messages", {"q": "a"})
            s2, dt2, b2 = await _timed_post(cli, base + "/v1/messages", {"q": "b"})
            s3, dt3, b3 = await _timed_post(cli, base + "/v1/messages", {"q": "c"})
            async with cli.get(base + "/__health") as h:
                health = await h.json()

        print(f"[1] status={s1} dt={dt1:.2f}s echo={b1['echo']!r}")
        print(f"[2] status={s2} dt={dt2:.2f}s echo={b2['echo']!r}")
        print(f"[3] status={s3} dt={dt3:.2f}s echo={b3['echo']!r}")
        print(f"[health] {health}")

        def chk(cond: bool, msg: str) -> None:
            print(("  [OK] " if cond else "  [FAIL] ") + msg)
            if not cond:
                failures.append(msg)

        chk(s1 == 200 and s2 == 529 and s3 == 200,
            f"透传上游状态序列 [200,529,200]，实得 [{s1},{s2},{s3}]")
        chk("a" in b1["echo"] and "b" in b2["echo"] and "c" in b3["echo"],
            "请求体经 proxy 透传到上游并回显（每次 body 不串扰）")
        chk(dt1 < 0.10, f"第1次无历史不节流 (dt1={dt1:.2f} 应 <0.10)")
        chk(0.25 <= dt2 <= 0.50, f"第2次按 min_interval≈0.3s 节流 (dt2={dt2:.2f})")
        chk(0.70 <= dt3 <= 1.00, f"第3次上游529触发 overload_cooldown≈0.8s 退避 (dt3={dt3:.2f})")
        chk(dt3 > dt2 + 0.2, f"退避时长 > 正常节流 (dt3={dt3:.2f} 应 > dt2={dt2:.2f}+0.2)")
        chk(health.get("ok") is True, "健康端点 /__health 返 ok")
    finally:
        await proxy_srv.close()
        await upstream_srv.close()

    if failures:
        print(f"\nSMOKE FAIL：{len(failures)} 项未过")
        for f in failures:
            print("  -", f)
        return 1
    print("\nSMOKE OK：透传/节流/529退避 全部符合预期")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
