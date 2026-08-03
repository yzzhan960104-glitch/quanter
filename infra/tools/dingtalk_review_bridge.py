# -*- coding: utf-8 -*-
"""dws dev connect 的 agent-cmd 桥：钉钉@消息(统一应用) → HTTP /training/review 或 /research/review。

根因（2026-07-16 实测确认）：审查应用 dingbabujxcelmssmdpn 是「统一应用」，老
dingtalk-stream SDK 的 ChatbotHandler 收不到@（代际不匹配；stream 连得上但@不推）。
改用 dws dev connect（统一应用新机制，已实测能收@）→ 本脚本 → HTTP 转发到 uvicorn
的 POST /api/v1/training/review → orchestrator.submit_review 唤醒人审关卡。

调用契约（dws agent-cmd stateless）：@文本作为最后一个 argv 追加；本脚本 stdout
作为回复推回钉钉。stdout 强制 UTF-8（避免 Windows GBK 控制台把中文@内容/回复写乱）。

Phase C（2026-08-03）：@文本含提案 id（p_xxxxxxxx）或「提案」关键词 → 路由到
POST /api/v1/research/review（研究提案审批）；否则走 training/review（训练 loop 审核）。

配合（常驻）：
    dws dev connect --unified-app-id e2695383-6fe9-4617-9439-2a8538af3107 \
        --channel custom \
        --agent-cmd ".venv310/Scripts/python.exe infra/tools/dingtalk_review_bridge.py"
"""
import json
import os
import sys
import urllib.error
import urllib.request

# stdout UTF-8：dws 读本脚本 stdout 转推钉钉，GBK 会把中文@内容/回复写乱（实测踩过）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# .env 凭证（QUANTER_API_TOKEN 若配了要带；服务端 review 端点 URL 可覆盖）
try:
    from dotenv import load_dotenv
    _PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))
except Exception:
    pass

_API = os.getenv("TRAINING_REVIEW_URL", "http://127.0.0.1:8000/api/v1/training/review")
_RESEARCH_API = os.getenv("RESEARCH_REVIEW_URL",
                          "http://127.0.0.1:8000/api/v1/research/proposals/review")
_TOKEN = os.getenv("QUANTER_API_TOKEN", "")


def main(argv=None) -> None:
    if argv is None:
        argv = sys.argv
    # dws 把@文本作为最后一个参数追加（本脚本无固定参数，argv[-1] 即@内容）
    text = (argv[-1] if argv else "").strip()
    if not text:
        print("⚠️ 收到空审核消息，忽略。")
        return
    body = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    # Phase C 路由：提案审批（含 p_xxxxxxxx / 「提案」）→ research；否则 training。
    is_proposal = bool(__import__("re").search(r"p_[0-9a-f]{8}|提案", text))
    req = urllib.request.Request(
        _RESEARCH_API if is_proposal else _API, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if _TOKEN:
        req.add_header("Authorization", f"Bearer {_TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        target = "研究提案" if is_proposal else "训练 loop"
        print(f"✅ 审核已提交{target}（{str(data.get('loop_id') or data.get('proposal_id') or '?')[:8]}…）：{text[:40]}")
    except urllib.error.HTTPError as exc:
        # 409=无活跃 loop；401=token；500=服务异常——都诚实回钉钉，不吞
        detail = exc.read().decode("utf-8", "ignore")[:120]
        print(f"❌ 提交失败 HTTP {exc.code}：{detail}")
    except Exception as exc:
        # 服务没起 / 连接拒绝——最常见，给明确提示
        print(f"❌ 提交异常（服务起了吗？）：{type(exc).__name__}：{exc}")


if __name__ == "__main__":
    main()
