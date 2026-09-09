# -*- coding: utf-8 -*-
"""research/committee/propose.py —— 委员会优化提案会议（2026-09-08 会战）。

区别于 review.py（评审既有结论）：本模块让委员会**主动提出**策略优化提案。
协议与 09-06 会战同源（TradingAgents 移植）：四分析师独立调研（perf/risk/
exec/fund，带工具）→ 多空辩论（各看对方最新论点）→ 风控三辩 → PM 终审
（问题清单 + JSON proposals ≤5 + 否决搁置项）。

产物：logs/committee_20260908/propose_round{N}.json（proposals + transcripts +
call 台账）。proposals 供整改回测侧消费；本模块不触碰回测/提案库——
执行与验证归驱动层（人/脚本），红线（止步 DRAFT）不变。
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from research.committee import strat_tools as T
from research.committee.llm_tools import GlmToolsSession
from research.committee.prompts import (ANALYST_EXEC, ANALYST_FUND,
                                        ANALYST_PERF, ANALYST_RISK, BEAR, BULL,
                                        PM, RISK_Trio)

OUT_DIR = ROOT / "logs" / "committee_20260908"

_PM_JSON_HINT = ('{"proposals": [{"id": "P1", "title": "…", '
                 '"change_type": "A|B|C", "params": {"参数名": "建议值或探索网格"}, '
                 '"mechanism": "机制假设一句话", "expected": "预期效应", '
                 '"risk": "主要风险", "validation": "验证方式", "priority": 1}], '
                 '"rejected": [{"title": "…", "reason": "…"}]}')

_HARD_RULES = """
【本轮硬约束（违反=提案作废）】
1. params 键必须是 NecklineConfig 真实参数名（用 query_config 核对），或明确
   标注「新键（B/C 档，需扩 schema）」。
2. 已被全窗口回测否决的方向不得重提：动量闸/MA 趋势闸/板块过滤/强势板块/
   量能门槛/MACD/溢价分层/板块涨跌比/时间止损（槽位等价替换实证）/
   弃过热Q5（滚动分位前视伪影已降级）。
3. 语料 rr 列 = 已实现 R 倍数（事后变量），不得作为事前特征提案。
4. 逐笔判别力 ≠ 可治疗性（H1/R6-2/L1 三案）——提案必须有组合口径可验证的
   机制路径，不能只援引逐笔分桶。
5. 实盘口径基线（keep-top5 + 20×5% + 21 种子中位）：主腿 ann +22.3%/dd -27.5%；
   纯收益栈 +22.6%/-28.9%；均衡栈 +19.3%/-10.6%/calmar 1.82/六年全正。
   提案预期效应以此为对照系，不得引用 oracle 口径数字（+400% 系先知调度保费）。
"""


def _extract_pm_json(text: str) -> dict | None:
    # 两个实弹坑:①嵌套数组对象——非贪婪 `{.*?}` 在首个内层 `}` 截断(r1);
    # ②裁决书超长 JSON 被	max_tokens 截断——闭合围栏匹配失败(r3)。
    # 对策:先取闭合围栏;失败则取 ```json 后残余文本做括号平衡修复。
    m = re.search(r"```json\s*(\{.*\})\s*```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except ValueError:
            pass
    m = re.search(r"```json\s*(\{.*)", text, re.S)
    if not m:
        return None
    frag = m.group(1).rstrip().rstrip("`")
    # 截断点通常在字符串值中间:砍掉最后一个完整键值对再补括号
    for _ in range(6):
        for closer in ('"}]', '"]}', '}'):
            try:
                doc = json.loads(frag + closer)
                if isinstance(doc, dict):
                    return doc
            except ValueError:
                continue
        frag = frag[:frag.rfind('"', 0, max(frag.rfind(","), 0))]
        if len(frag) < 20:
            return None
    return None


def run_proposal_round(brief: str, round_no: int, *, budget_s: int = 2400,
                       model: str | None = None) -> dict:
    """跑一轮提案会议。brief=本轮输入简报（现状/前轮复审意见/新证据）。"""
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    user = f"{_HARD_RULES}\n\n## 本轮主席简报\n{brief}\n\n---\n请按角色要求工作。"
    transcripts: list[dict] = []
    calls = 0
    stopped_early = None

    def budget_left() -> float:
        return budget_s - (time.time() - t0)

    # ── 第一层：四分析师独立调研 ──
    reports = ""
    for node, sysp in (("perf", ANALYST_PERF), ("risk", ANALYST_RISK),
                       ("exec", ANALYST_EXEC), ("fund", ANALYST_FUND)):
        if budget_left() < budget_s * 0.25:
            stopped_early = f"{node} 前预算不足"
            break
        s = GlmToolsSession(sysp, T.build_tools(), model=model,
                            max_tool_rounds=5)
        text = s.run(user, max_tokens=6144, reasoning_effort="high")
        calls += s.call_count
        transcripts.append({"node": node, "messages": s.messages,
                            "tool_log": s.tool_log})
        reports += f"### 分析师报告：{node}\n{text}\n\n"
        print(f"[propose r{round_no}] {node} done ({s.call_count} calls, "
              f"{time.time()-t0:.0f}s)", flush=True)

    # ── 第二层：多空辩论（各看对方最新论点，两轮） ──
    debate = ""
    for side, sysp in (("bull", BULL), ("bear", BEAR)):
        if budget_left() < budget_s * 0.15:
            stopped_early = stopped_early or f"{side} 前预算不足"
            break
        s = GlmToolsSession(sysp, T.build_tools(), model=model,
                            max_tool_rounds=3)
        text = s.run(f"{user}\n\n## 四分析师报告\n{reports}\n\n## 此前辩论\n"
                     f"{debate or '（你是第一个发言的辩手）'}",
                     max_tokens=6144, reasoning_effort="high")
        calls += s.call_count
        transcripts.append({"node": side, "messages": s.messages,
                            "tool_log": s.tool_log})
        debate += f"### {side}\n{text}\n\n"
        print(f"[propose r{round_no}] {side} done ({s.call_count} calls, "
              f"{time.time()-t0:.0f}s)", flush=True)

    # ── 第三层：风控三辩 ──
    risk_views = ""
    for side, sysp in RISK_Trio.items():
        if budget_left() < budget_s * 0.10:
            stopped_early = stopped_early or f"risk-{side} 前预算不足"
            break
        s = GlmToolsSession(sysp, T.build_tools(), model=model,
                            max_tool_rounds=2)
        text = s.run(f"{user}\n\n## 分析师报告\n{reports}\n## 多空辩论\n{debate}",
                     max_tokens=5120, reasoning_effort="high")
        calls += s.call_count
        transcripts.append({"node": f"risk_{side}", "messages": s.messages,
                            "tool_log": s.tool_log})
        risk_views += f"### 风控辩手 {side}\n{text}\n\n"
        print(f"[propose r{round_no}] risk_{side} done ({s.call_count} calls, "
              f"{time.time()-t0:.0f}s)", flush=True)

    # ── 终席：PM 裁决 ──
    s = GlmToolsSession(PM, T.build_tools(), model=model, max_tool_rounds=3)
    pm_text = s.run(f"{user}\n\n## 分析师报告\n{reports}\n## 多空辩论\n{debate}\n"
                    f"## 风控三辩\n{risk_views}\n---\n请终审。第二部分输出单个 "
                    f"```json 代码块（schema 如下，change_type A=参数值 B=开关规则 "
                    f"C=结构级）：\n{_PM_JSON_HINT}",
                    max_tokens=8192, reasoning_effort="high")
    calls += s.call_count
    transcripts.append({"node": "pm", "messages": s.messages,
                        "tool_log": s.tool_log})
    pm_json = _extract_pm_json(pm_text) or {"proposals": [], "rejected": []}
    print(f"[propose r{round_no}] PM done ({s.call_count} calls, "
          f"{time.time()-t0:.0f}s) proposals={len(pm_json.get('proposals') or [])}",
          flush=True)

    out = {"round": round_no, "started_at": f"{datetime.fromtimestamp(t0):%H:%M:%S}",
           "brief": brief[:4000], "proposals": pm_json.get("proposals") or [],
           "rejected": pm_json.get("rejected") or [],
           "pm_text": pm_text, "stopped_early": stopped_early,
           "llm_calls": calls, "elapsed_s": round(time.time() - t0, 1),
           "transcripts": transcripts}
    path = OUT_DIR / f"propose_round{round_no}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                    encoding="utf-8")
    slim = {k: out[k] for k in ("round", "proposals", "rejected", "llm_calls",
                                "elapsed_s", "stopped_early")}
    (OUT_DIR / f"propose_round{round_no}.slim.md").write_text(
        f"# 提案会议 Round {round_no}（{out['llm_calls']} calls / "
        f"{out['elapsed_s']}s）\n\n```json\n"
        + json.dumps(slim, ensure_ascii=False, indent=1) + "\n```\n",
        encoding="utf-8")
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--brief-file", required=True, help="本轮简报 md 路径")
    ap.add_argument("--round", type=int, required=True)
    ap.add_argument("--budget", type=int, default=2400)
    a = ap.parse_args()
    brief = Path(a.brief_file).read_text(encoding="utf-8")
    doc = run_proposal_round(brief, a.round, budget_s=a.budget)
    print(f"\nverdict: {len(doc['proposals'])} proposals → {OUT_DIR}"
          f"/propose_round{a.round}.json")
