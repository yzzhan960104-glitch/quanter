# -*- coding: utf-8 -*-
"""research/committee/review.py —— 委员会评审服务（质证工序统一入口）。

设计（docs/research/2026-09-06-committee-embedding-design.md）：
  - 三档：Tier 0 谱面闸（kb.tier0_scan，零 LLM）/ Tier B 轻量质证（单质证官带工具，
    5-8 calls）/ Tier A scoped 委员会（空头质证→多头辩护→风控官→PM，8-12 calls）；
  - 附签制：verdict ∈ PASS|NOTES|ESCALATE|UNAVAILABLE；唯一 fail-closed 消费点在
    proposals.publish_proposal（ESCALATE→NEEDS_HUMAN）；
  - fail-open：端点不可用/超预算/解析失败 → UNAVAILABLE（NOTES 兜底），绝不炸调用方；
  - 防锚定：所有评审产物强制携带 disclaimer=「本评审仍为假设，部署裁决权在回测闸
    与人审」；委员会自身的结论不过委员会（防递归）。

产物：logs/committee_reviews/{kind}_{ts}.json（含 verdict/事实核查/知识库冲突/
证据等级/完整 transcript）。台账：llm_calls 计数入产物与 digest 评审台。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from research.committee import kb as KB
from research.committee import strat_tools as T
from research.committee.llm_tools import GlmToolsSession
from research.committee.prompts import BASIS

REVIEWS_DIR = ROOT / "logs" / "committee_reviews"
VERDICTS = ("PASS", "NOTES", "ESCALATE", "UNAVAILABLE")
DISCLAIMER = "本评审仍为假设，部署裁决权在回测闸与人审"

_SCHEMA_HINT = ('{"verdict": "PASS|NOTES|ESCALATE", "fact_checks": '
                '[{"claim": "被核断言", "status": "命中|漂移|不可验"}], '
                '"kb_conflicts": ["与知识库冲突点"], '
                '"evidence_grade": "实证|推断|猜想", "notes": "一句话总评"}')

_REVIEWER_SYS = BASIS + """

【你的角色：质证官（Tier B 轻量质证）】
对给定结论做反方质证，四步：
① 事实核查：抽核结论引用的关键数字（可用工具直查；引用含糊则标「不可验」）；
② 知识库核查：query_knowledge 检索相关主题，标记与已定稿结论的冲突；
③ 证据等级：结论的证据是「实证」（工具数据直接支持）/「推断」/「猜想」；
④ 裁决：PASS（无实质问题）/ NOTES（有瑕疵但不影响结论方向）/ ESCALATE
   （数字编造/与已否决方向冲突/证据等级造假——必须拦下的那种）。
控制在 500 字内；最后输出单个 ```json 代码块（schema 见用户消息末尾），
verdict 只能取 PASS/NOTES/ESCALATE（不可用/超时由系统层处理，不由你给）。"""

_TIER_A_ROLES = [
    ("a_bear", BASIS + """

【你的角色：空头质证人（Tier A 第 1 席）】
对给定结论发起最强质证：数字是否可复现（工具核）、机制是否成立、是否与已否决
知识同型、证据等级是否虚标。400 字内。"""),
    ("a_bull", BASIS + """

【你的角色：多头辩护人（Tier A 第 2 席）】
针对质证人的攻击做最强辩护：哪些指控不成立或代价被夸大；无可辩处诚实承认。
400 字内。"""),
    ("a_risk", BASIS + """

【你的角色：风控官（Tier A 第 3 席）】
综合攻防双方：过拟合风险评级（高/中/低+理由）、需要的证据等级、若采纳的
最大风险。300 字内。"""),
]
_TIER_A_PM_SYS = BASIS + """

【你的角色：PM 裁决人（Tier A 终席）】
综合质证/辩护/风控三方意见，对结论做终审：500 字内裁决书 + 单个 ```json
代码块（schema 见用户消息末尾），verdict ∈ PASS/NOTES/ESCALATE。"""


def gate_enabled() -> bool:
    """COMMITTEE_GATE=0 全局关闭（测试/紧急停用口）。"""
    return os.getenv("COMMITTEE_GATE", "1") != "0"


def _extract_json(text: str) -> dict | None:
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S)
    if not m:
        m = re.search(r'\{\s*"verdict".*?\}\s*$', text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


def _coerce(doc: dict | None, raw: str) -> dict:
    """模型输出 → 标准 verdict 结构（不可解析=NOTES 兜底，fail-open）。"""
    if not isinstance(doc, dict):
        return {"verdict": "NOTES", "fact_checks": [], "kb_conflicts": [],
                "evidence_grade": "", "notes": f"评审 JSON 解析失败，原文见 artifact。{raw[:200]}"}
    v = str(doc.get("verdict", "NOTES")).upper().strip()
    if v not in ("PASS", "NOTES", "ESCALATE"):
        v = "NOTES"
    return {"verdict": v,
            "fact_checks": doc.get("fact_checks") or [],
            "kb_conflicts": doc.get("kb_conflicts") or [],
            "evidence_grade": doc.get("evidence_grade", ""),
            "notes": str(doc.get("notes", ""))[:500]}


def _artifact(kind: str, payload: dict) -> Path:
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    path = REVIEWS_DIR / f"{kind}_{datetime.now():%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str),
                    encoding="utf-8")
    return path


def _tier0_of(doc: str, subject: dict | None) -> list[str]:
    return KB.tier0_scan((subject or {}).get("params"), doc)


def review_conclusion(kind: str, doc: str, *, tier: str = "A",
                      subject: dict | None = None,
                      timeout_s: int | None = None) -> dict:
    """评审一条结论文档（fail-open：任何异常→UNAVAILABLE，绝不抛给调用方）。

    kind：产物命名（如 loser_review_2026-09-08 / proposal_p_xxx / explore_2026-09-08）。
    doc：结论文本（含数字引用与机制叙述）。
    tier："B" 质证官单席 / "A" scoped 四席委员会。
    subject：结构化上下文（params 等供 Tier 0 扫描）。
    """
    t0 = time.time()
    if not gate_enabled():
        return {"verdict": "UNAVAILABLE", "kb_conflicts": [], "fact_checks": [],
                "evidence_grade": "", "notes": "COMMITTEE_GATE=0 评审关闭",
                "llm_calls": 0, "artifact": None, "disclaimer": DISCLAIMER,
                "tier0_conflicts": _tier0_of(doc, subject)}
    budget = timeout_s or (420 if tier == "B" else 900)
    try:
        out = _review(kind, doc, tier, subject or {}, t0, budget)
    except Exception as e:  # noqa: BLE001 —— fail-open 生存底线
        out = {"verdict": "UNAVAILABLE", "fact_checks": [], "kb_conflicts": [],
               "evidence_grade": "", "notes": f"{type(e).__name__}: {e}"[:300],
               "llm_calls": 0, "transcripts": []}
    out.update({"kind": kind, "tier": tier, "disclaimer": DISCLAIMER,
                "tier0_conflicts": _tier0_of(doc, subject),
                "elapsed_s": round(time.time() - t0, 1)})
    out["artifact"] = str(_artifact(kind, out))
    return out


def _review(kind: str, doc: str, tier: str, subject: dict,
            t0: float, budget: float) -> dict:
    user = (f"## 待评审结论（{kind}）\n{doc}\n\n## 附加上下文\n"
            f"{json.dumps(subject, ensure_ascii=False, default=str)[:800]}\n\n"
            f"---\n请按角色要求质证，最后输出单个 ```json 代码块：\n{_SCHEMA_HINT}")
    transcripts: list[dict] = []
    calls = 0
    if tier == "B":
        s = GlmToolsSession(_REVIEWER_SYS, T.build_tools(), max_tool_rounds=4)
        text = s.run(user, max_tokens=6144, reasoning_effort="low")
        calls += s.call_count
        transcripts.append({"node": "reviewer", "messages": s.messages,
                            "tool_log": s.tool_log})
        return {**_coerce(_extract_json(text), text), "raw_text": text,
                "llm_calls": calls, "transcripts": transcripts}
    # Tier A：scoped 四席（质证→辩护→风控→PM）
    ctx = ""
    for node, sysp in _TIER_A_ROLES:
        if time.time() - t0 > budget * 0.6:
            return {"verdict": "UNAVAILABLE", "fact_checks": [], "kb_conflicts": [],
                    "evidence_grade": "", "notes": f"Tier A 超时预算（{budget}s）中止",
                    "llm_calls": calls, "transcripts": transcripts}
        s = GlmToolsSession(sysp, T.build_tools(), max_tool_rounds=3)
        text = s.run(f"{user}\n\n## 此前攻防\n{ctx or '（你是第一席）'}",
                     max_tokens=6144, reasoning_effort="low")
        calls += s.call_count
        transcripts.append({"node": node, "messages": s.messages,
                            "tool_log": s.tool_log})
        ctx += f"### {node}\n{text}\n\n"
    s = GlmToolsSession(_TIER_A_PM_SYS, T.build_tools(), max_tool_rounds=2)
    text = s.run(f"{user}\n\n## 三席攻防记录\n{ctx}\n---\n请终审（裁决书+json）。",
                 max_tokens=6144, reasoning_effort="high")
    calls += s.call_count
    transcripts.append({"node": "pm", "messages": s.messages, "tool_log": s.tool_log})
    return {**_coerce(_extract_json(text), text),
            "raw_text": text, "llm_calls": calls, "transcripts": transcripts}


def review_proposal_for_publish(db_path: str, proposal_id: str,
                                tier: str = "A") -> dict:
    """publish 闸专用：从提案库组装结论文档 → review_conclusion。"""
    import sqlite3
    con = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT * FROM research_proposal WHERE proposal_id=?", (proposal_id,)).fetchone()
    con.close()
    if row is None:
        return {"verdict": "UNAVAILABLE", "notes": f"提案 {proposal_id} 不存在",
                "llm_calls": 0}
    try:
        ver = json.loads(row["verification_json"] or "{}") or {}
    except ValueError:
        ver = {}
    doc = (
        f"研究提案 {proposal_id}（{row['change_type']} 档）拟 publish DRAFT：\n"
        f"- 假设：{row['hypothesis']}\n- 参数：{row['params_json']}\n"
        f"- 预期：{row['expected_effect'] or '—'}\n- 风险自述：{row['risk'] or '—'}\n"
        f"- 回测验证：{ver.get('verdict', '—')}（{ver.get('reason', '—')}）\n"
        f"- inner：{json.dumps(ver.get('proposal') or {}, ensure_ascii=False)[:300]}\n"
        f"- outer：{json.dumps(ver.get('proposal_outer') or {}, ensure_ascii=False)[:300]}")
    return review_conclusion(f"proposal_{proposal_id}", doc, tier=tier,
                             subject={"params": json.loads(row["params_json"] or "{}")})


def recent_reviews(days: int = 2, limit: int = 12) -> list[dict]:
    """digest 评审台数据源：近 N 日评审产物摘要（新→旧）。"""
    if not REVIEWS_DIR.exists():
        return []
    out: list[dict] = []
    now = time.time()
    for f in sorted(REVIEWS_DIR.glob("*.json"), reverse=True)[:limit * 2]:
        try:
            if now - f.stat().st_mtime > days * 86400:
                continue
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"kind": d.get("kind", f.stem), "tier": d.get("tier"),
                    "verdict": d.get("verdict"), "llm_calls": d.get("llm_calls"),
                    "notes": str(d.get("notes", ""))[:120],
                    "kb_conflicts": d.get("kb_conflicts") or []})
        if len(out) >= limit:
            break
    return out
