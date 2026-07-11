# -*- coding: utf-8 -*-
"""蔡森视图 E2E：全栈链路 + 鉴权 token 注入的端到端验证（首条 E2E）。

物理定位（用户元诉求「在页面上验证每个交互的真实可用性」的首条落地）：
    此前鉴权 token 缺口的修复（web/src/api/client.ts 请求拦截器注入 Authorization）
    只由「代码审查 + vue-tsc 类型检查 + TestClient 后端契约」三段保障，唯独缺「真实
    浏览器发头」这一端到端事实。本脚本用 Playwright 起 chromium 真实加载 /caisen 页面，
    拦截 onMounted 自动发出的 GET /api/v1/caisen/plans 请求，断言其携带
    `Authorization: Bearer <VITE_API_TOKEN>` 头——补上最后一块端到端证据。

断言（核心三条，任一失败即 exit 1）：
    1. 页面 /caisen 能加载到 networkidle（前端不白屏、不崩）；
    2. GET /api/v1/caisen/plans 请求携带 Authorization: Bearer <TOKEN> 头（token 注入真实生效）；
    3. 该请求响应非 401（鉴权打通，业务端点可用）。
bonus（best-effort，失败不阻断主结论）：点击「触发扫描」，验证 POST /scan 同样带 token。

运行（由 with_server.py 起后端 + 前端，token 两端同字面量）：
    python <webapp-testing>/scripts/with_server.py \\
      --server "python -m uvicorn server.main:app --port 8000" --port 8000 \\
      --server "npm --prefix web run dev" --port 5173 --timeout 90 \\
      -- python tests/e2e/caisen_token_path.py
前置：web/.env.local 含 VITE_API_TOKEN=e2e-token（与后端 QUANTER_API_TOKEN 同字面量）。
"""
import sys

# Token 与 web/.env.local 的 VITE_API_TOKEN、后端 QUANTER_API_TOKEN 三处同字面量。
TOKEN = "e2e-token"
FRONT_URL = "http://localhost:5173/caisen"

# 捕获目标请求的鉴权头与响应状态（onMounted 的 listPlans 必发；scan 点击后发）
captured = {"plans_auth": None, "plans_status": None, "scan_auth": None}


def main() -> int:
    # 延迟 import：脚本被 with_server 调用时才装好的 playwright 才在 sys.path
    from playwright.sync_api import sync_playwright

    console_errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # 拦截响应：抓 /caisen/plans 的状态码（response 事件含 status）
        def on_response(resp):
            url = resp.url
            if "/api/v1/caisen/plans" in url and resp.request.method == "GET":
                captured["plans_status"] = resp.status

        # 拦截请求：抓 /caisen/plans 与 /caisen/scan 的 Authorization 头
        def on_request(req):
            if "/api/v1/caisen/plans" in req.url and req.method == "GET":
                captured["plans_auth"] = req.headers.get("authorization", "")
            if "/api/v1/caisen/scan" in req.url and req.method == "POST":
                captured["scan_auth"] = req.headers.get("authorization", "")

        page.on("response", on_response)
        page.on("request", on_request)
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))

        # 1) 加载页面至 networkidle（前端 JS 执行完毕，onMounted 已触发 listPlans）
        page.goto(FRONT_URL, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")

        # 留证截图（便于人工核对渲染，无候选数据时列表显示 empty-text）
        page.screenshot(path="tests/e2e/_caisen_e2e.png", full_page=True)

        # 2) bonus：点击顶部「触发扫描」，验证写操作 POST /scan 同样带 token
        try:
            page.get_by_role("button", name="触发扫描").first.click(timeout=5000)
            page.wait_for_timeout(2500)  # 等 scan 请求发出
        except Exception as exc:
            print(f"[info] 点击「触发扫描」未成功（不影响主断言）：{exc}")

        browser.close()

    # ============ 断言 ============
    print(f"捕获 GET /caisen/plans → Authorization: {captured['plans_auth']!r}, status={captured['plans_status']}")
    print(f"捕获 POST /caisen/scan → Authorization: {captured['scan_auth']!r}")

    ok = True
    # 断言 2：plans 请求必须带 Bearer 头（核心：token 注入真实生效）
    auth = captured.get("plans_auth")
    if not auth:
        print("FAIL: GET /caisen/plans 未携带 Authorization 头（请求拦截器未注入 token）")
        ok = False
    elif auth != f"Bearer {TOKEN}":
        print(f"FAIL: Authorization 头值不符，期望 'Bearer {TOKEN}'，实际 {auth!r}")
        ok = False
    else:
        print("PASS: GET /caisen/plans 正确携带 Authorization: Bearer 头（token 注入端到端生效）")

    # 断言 3：响应非 401（鉴权打通）
    status = captured.get("plans_status")
    if status is None:
        print("WARN: 未捕获到 GET /caisen/plans 响应状态（可能请求未发出）")
    elif status == 401:
        print(f"FAIL: GET /caisen/plans 返回 401（鉴权未打通，token 两端可能不一致）")
        ok = False
    else:
        print(f"PASS: GET /caisen/plans 响应 {status}（非 401，鉴权已打通）")

    # bonus：scan 若发出，验证同样带 token
    scan_auth = captured.get("scan_auth")
    if scan_auth is not None:
        if scan_auth == f"Bearer {TOKEN}":
            print("PASS(bonus): POST /caisen/scan 同样携带 Bearer 头")
        else:
            print(f"WARN(bonus): POST /caisen/scan 的 Authorization 为 {scan_auth!r}")

    # console 错误仅告警（不阻断——EP/lightweight-charts 偶有非致命 warning）
    if console_errors:
        print(f"WARN: 浏览器 console 有 {len(console_errors)} 条 error（前 3 条）：")
        for e in console_errors[:3]:
            print(f"  - {e}")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
