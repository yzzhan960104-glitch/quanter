# -*- coding: utf-8 -*-
"""research/committee/kb.py —— 委员会知识库（三源加载 + Tier 0 谱面闸）。

三源（2026-09-06 嵌入设计 §3）：
  1. 策展层 research/committee/kb/*.md——一主题一文件（stem=topic），人工可编辑、
     Git 可追溯；「越用越准」的人类维护面。
  2. 自动层——research_proposals.db 的 REJECTED 提案自动转先验（T3.3 学习回路
     复用：同形状提案死过一次，委员会先验在场）。
  3. 监控层（预留）——前向指标读数（above×年滚动期望等，待 digest 监控落地）。

Tier 0 谱面闸（零 LLM 代码闸，create_proposal 内嵌）：高精度规则——参数形状与
文本关键词命中否决知识时**只记录不拦截**（升级拦截归评审层 ESCALATE 语义）。
设计取舍：规则刻意保守（宁漏勿误），避免误报 ESCALATE 淹没信号。
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

KB_DIR = Path(__file__).parent / "kb"
_DB = ROOT / "logs" / "research_proposals.db"

_CACHE: dict[str, str] | None = None


def load_kb(refresh: bool = False) -> dict[str, str]:
    """三源合并加载（进程内缓存；测试或库更新后 refresh=True）。"""
    global _CACHE
    if _CACHE is not None and not refresh:
        return _CACHE
    kb: dict[str, str] = {}
    for f in sorted(KB_DIR.glob("*.md")):
        txt = f.read_text(encoding="utf-8").strip()
        if txt:
            kb[f.stem] = txt
    auto = _rejected_kb()
    if auto:
        kb["rejected_proposals"] = auto
    _CACHE = kb
    return kb


def _rejected_kb(limit: int = 30) -> str:
    """REJECTED 提案 → 先验文本（只读连接；库缺失/损坏=静默空，绝不炸调用方）。"""
    if not _DB.exists():
        return ""
    try:
        con = sqlite3.connect(f"file:{_DB.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT proposal_id, change_type, hypothesis, params_json,"
            " verification_json, note FROM research_proposal"
            " WHERE status='REJECTED' ORDER BY created_at DESC LIMIT ?",
            (limit,)).fetchall()
        con.close()
    except sqlite3.Error:
        return ""
    if not rows:
        return ""
    out = [f"自动挖掘最近 {len(rows)} 条 REJECTED 提案（新→旧；同形状参数已死过一次，"
           f"重提将被质证；T3.3 学习回路）："]
    for r in rows:
        try:
            reason = (json.loads(r["verification_json"] or "{}") or {}).get("reason", "")
        except (ValueError, TypeError):
            reason = ""
        out.append(
            f"- {r['proposal_id']} [{r['change_type']}] {str(r['hypothesis'])[:90]}"
            f"｜params={str(r['params_json'] or '')[:70]}"
            f"｜拒因：{reason or r['note'] or '—'}")
    return "\n".join(out)


# ─────────────────────── Tier 0：零 LLM 冲突扫描 ───────────────────────

_REJECTED_PARAM_RULES = {
    "momentum_gate": "momentum_gate 任何启用已被全窗口回测否决（0.15→年化 139%↓）"
                     "——七波否决潮",
}
_REJECTED_TEXT_PATTERNS = (
    "动量闸", "MA趋势闸", "MA 趋势闸", "MA200闸", "MA 闸", "多头排列",
    "板块过滤", "强势板块", "量能门槛", "MACD", "溢价分层",
)
# rr 当事前特征：rr + sizing/过滤语义共现（语料 rr=事后 R 倍数，09-06 定谳）
_RR_PATTERN = re.compile(r"rr.{0,10}(sizing|减配|加配|过滤|分层|条件化)", re.I)


def tier0_scan(params: dict | None, text: str) -> list[str]:
    """高精度否决知识冲突扫描（返回冲突描述列表；空=干净）。只记录不拦截。"""
    conflicts: list[str] = []
    for key, why in _REJECTED_PARAM_RULES.items():
        val = (params or {}).get(key)
        if val not in (None, 0):
            conflicts.append(f"参数 {key}={val}：{why}")
    t = text or ""
    for pat in _REJECTED_TEXT_PATTERNS:
        if pat in t:
            conflicts.append(f"方向疑与否决知识冲突：「{pat}」"
                             f"（七波否决潮/MA 两轮穷尽，见 kb/filters_7waves.md）")
    m = _RR_PATTERN.search(t)
    if m:
        conflicts.append("疑似把事后 rr 当事前特征（语料 rr=已实现 R 倍数，"
                         "strategy.py:188 定谳，见 kb/posthoc_rr.md）——需先重建信号时几何 rr")
    return conflicts
