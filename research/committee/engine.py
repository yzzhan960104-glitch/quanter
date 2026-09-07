# -*- coding: utf-8 -*-
"""research/committee/engine.py —— 策略评审委员会编排引擎 + CLI。

流水线（TA 辩论协议移植）：
  dossier（确定性简报）→ 分析师×3（带工具）→ 多空辩论 2 轮（带工具）
  → 风控三辩 → PM 终审（结构化裁决 + JSON 提案）。

工程要点：
  - 每相位独立 GlmToolsSession（上下文经 prompt 注入，消息史不跨相位）；
  - 断点续跑：每相位产物落盘 logs/committee_{day}/，--from 指定起始相位；
  - 降级：单相位失败（重试耗尽）记录 error 并以已有产物继续（分析侧不炸全链）；
  - 配额台账：全程统计 LLM 调用次数与工具调用分布。
"""
from __future__ import annotations

import argparse
import json
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

from research.committee import prompts as P
from research.committee.llm_tools import GlmToolsSession
from research.committee import strat_tools as T

PHASES = ["analysts", "debate", "risk", "pm"]
_OUT: Path | None = None
_T0 = time.time()


def _out_dir() -> Path:
    global _OUT
    if _OUT is None:
        _OUT = ROOT / "logs" / f"committee_{datetime.now():%Y%m%d}"
        _OUT.mkdir(parents=True, exist_ok=True)
    return _OUT


def _log(msg: str) -> None:
    print(f"[{time.time() - _T0:7.0f}s] {msg}", flush=True)


def _save(key: str, text: str, session: GlmToolsSession | None) -> None:
    d = _out_dir()
    (d / f"{key}.md").write_text(text or "(空产出——相位降级)", encoding="utf-8")
    if session is not None:
        (d / f"{key}.transcript.json").write_text(json.dumps({
            "messages": session.messages, "tool_log": session.tool_log,
            "llm_calls": session.call_count,
        }, ensure_ascii=False, indent=1), encoding="utf-8")


def _run_node(key: str, system: str, user: str, tools: bool = True,
              max_tokens: int = 8192, effort: str = "high") -> str:
    """单相位执行：失败降级为 error 文本，不炸全链。"""
    try:
        s = GlmToolsSession(system, T.build_tools() if tools else {})
        text = s.run(user, max_tokens=max_tokens, reasoning_effort=effort)
        _save(key, text, s)
        _log(f"{key}: 完成，LLM调用 {s.call_count} 次，工具 "
             f"{len(s.tool_log)} 次，产出 {len(text)} 字")
        return text
    except Exception as e:  # noqa: BLE001 —— 分析侧降级
        err = f"（{key} 相位失败降级：{type(e).__name__}: {e}）"
        _save(key, err, None)
        _log(f"{key}: 失败——{err}")
        return err


# ─────────────────────── dossier（确定性，零 LLM） ───────────────────────

def build_dossier() -> str:
    parts = [
        "## 策略简报（主席提供，全部为工具同源事实）",
        T.tool_query_config(),
        "",
        "### 全期与分年",
        T.tool_query_trades(group_by="year"),
        "### 出场结构",
        T.tool_query_trades(group_by="exit_reason"),
        "### 持有期分桶",
        T.tool_query_trades(group_by="holding_bin"),
        "### Regime 三区",
        T.tool_query_regime(),
        "### 实盘持仓（调用时点快照）",
        T.tool_query_positions(),
    ]
    txt = "\n\n".join(parts)
    (_out_dir() / "dossier.md").write_text(txt, encoding="utf-8")
    return txt


# ─────────────────────── 各相位 ───────────────────────

def run_analysts(dossier: str) -> dict[str, str]:
    roles = {"perf": P.ANALYST_PERF, "risk": P.ANALYST_RISK,
             "exec": P.ANALYST_EXEC, "fund": P.ANALYST_FUND}
    asks = {
        "perf": "请开始绩效结构分析。先调工具核实关键数字，再按必答三问输出。",
        "risk": "请开始风险与 regime 分析。先调工具核实关键数字，再按必答三问输出。",
        "exec": "请开始执行与结构分析。先调 query_config 核对参数语义，"
                "再用数据佐证，按必答三问输出。",
        "fund": "请开始基本面分析。先调 query_valuation/query_financials/"
                "query_earnings 核实持仓标的数据，再按必答三问输出"
                "（红线：基本面=风险上下文，非过滤信号）。",
    }
    out: dict[str, str] = {}
    for k, sysp in roles.items():
        user = f"{dossier}\n\n---\n以上为简报。{asks[k]}"
        out[k] = _run_node(f"analyst_{k}", sysp, user)
    return out


def run_debate(dossier: str, analysts: dict[str, str]) -> dict[str, str]:
    digest = "\n\n".join(f"### 分析师报告·{k}\n{v}" for k, v in analysts.items())
    turns: list[tuple[str, str]] = [
        ("bull_r1", P.BULL, "多头研究员第一轮发言：为当前设计做最强辩护。"),
        ("bear_r1", P.BEAR, "空头研究员第一轮发言：发起最强攻击。"),
        ("bull_r2", P.BULL, "多头研究员第二轮：回应空头的攻击，修正或坚持。"),
        ("bear_r2", P.BEAR, "空头研究员第二轮：回应辩护，给出最终攻击。"),
    ]
    out: dict[str, str] = {}
    for key, sysp, ask in turns:
        prior = "\n\n".join(f"### {k}\n{v}" for k, v in out.items())
        user = (f"{digest}\n\n### 此前辩论\n{prior or '（你是第一个发言者）'}\n\n"
                f"---\n{ask}")
        out[key] = _run_node(key, sysp, user, tools=True)
    return out


def run_risk(dossier: str, analysts: dict[str, str],
             debate: dict[str, str]) -> dict[str, str]:
    digest = ("\n\n".join(f"### 分析师·{k}\n{v}" for k, v in analysts.items())
              + "\n\n" + "\n\n".join(f"### 辩论·{k}\n{v}"
                                     for k, v in debate.items()))
    out: dict[str, str] = {}
    for k, sysp in P.RISK_Trio.items():
        user = (f"{digest}\n\n---\n请以你的视角对辩论暴露的问题清单与改进方向"
                f"发言（先抓出辩论中双方实际交锋出的候选改进方向，再逐一评）。")
        out[k] = _run_node(f"risk_{k}", sysp, user)
    return out


def run_pm(dossier: str, analysts: dict[str, str], debate: dict[str, str],
           risk: dict[str, str]) -> str:
    ctx = ("\n\n".join(f"### 分析师·{k}\n{v}" for k, v in analysts.items())
           + "\n\n".join(f"### 辩论·{k}\n{v}" for k, v in debate.items())
           + "\n\n".join(f"### 风控·{k}\n{v}" for k, v in risk.items()))
    user = (f"{ctx}\n\n---\n请做出终审裁决（两部分：markdown 裁决书 + 严格 JSON）。"
            f"注意：JSON 中 params 必须用 NecklineConfig 真实参数名；每条提案须标注"
            f"保守辩手的过拟合质疑如何逃逸；提案前用 query_knowledge 复核方向未死。")
    return _run_node("pm_verdict", P.PM, user, tools=True,
                     max_tokens=12288, effort="max")


def _load_phase(name: str) -> str | None:
    f = _out_dir() / f"{name}.md"
    return f.read_text(encoding="utf-8") if f.exists() else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="frm", default="dossier",
                    choices=["dossier"] + PHASES, help="断点续跑起始相位")
    args = ap.parse_args()
    _log(f"委员会会战启动 out={_out_dir()}")

    if args.frm == "dossier":
        dossier = build_dossier()
        _log(f"dossier 就绪 {len(dossier)} 字")
    else:
        dossier = (_out_dir() / "dossier.md").read_text(encoding="utf-8")

    order = PHASES[PHASES.index("analysts"):]
    start = 0 if args.frm == "dossier" else PHASES.index(args.frm)

    analysts = None
    debate = None
    risk = None
    for i, ph in enumerate(order):
        if i < start:
            continue
        if ph == "analysts":
            analysts = run_analysts(dossier)
        elif ph == "debate":
            analysts = analysts or {k: _load_phase(f"analyst_{k}") or "(缺失)"
                                    for k in ("perf", "risk", "exec")}
            debate = run_debate(dossier, analysts)
        elif ph == "risk":
            debate = debate or {k: _load_phase(k) or "(缺失)"
                                for k in ("bull_r1", "bear_r1", "bull_r2", "bear_r2")}
            risk = run_risk(dossier, analysts, debate)
        elif ph == "pm":
            risk = risk or {k: _load_phase(f"risk_{k}") or "(缺失)"
                            for k in ("aggressive", "conservative", "neutral")}
            pm = run_pm(dossier, analysts, debate, risk)
            _log(f"PM 裁决完成 {len(pm)} 字——终产物 {_out_dir() / 'pm_verdict.md'}")
    _log("全部相位完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
