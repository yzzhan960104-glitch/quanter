# -*- coding: utf-8 -*-
"""Phase C 研究提案工作流（2026-08-03 · Agent 长周期多轮交互的数据地基）。

物理定位：discovery 解决"参数自动探索"，training loop 解决"人审多轮调参"，
本模块补上中间的"Agent 提案 → 自动验证 → 人审放行"闭环：
    1. Agent（LLM）基于研究摘要/实验历史生成结构化提案（change_type A/B/C）；
    2. A 档（参数）自动验证：evaluate_replay 跑基线（当前 ACTIVE）vs 提案，
       门槛判定（inner 改善 + outer 不显著劣化）→ APPROVED/REJECTED；
    3. B/C 档（过滤器开关/结构进化）需要代码变更，MVP 只落 PENDING 待人工评估；
    4. 钉钉审核（通过/否决 proposal_xxx）→ 状态迁移；
    5. APPROVED → publish 到 experiment DRAFT（promote 仍留人审，红线不破）。

依赖：experiment（ACTIVE 基线/版本）、discovery（evaluate_replay/freeze）、
infra.llm（提案生成）。零 trading.engine 依赖（观测/研究层只读）。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from discovery.objective import evaluate_replay
from discovery.snapshot import freeze
from experiment.resolver import resolve_active
from infra.llm import get_llm_client

logger = logging.getLogger(__name__)

_DEFAULT_DB = "logs/research_proposals.db"

# ── A 档验证门槛（MVP 阈值，后续用历史验证结果标定）──
MIN_HITS = 30            # inner 最少成交笔数（样本不足直接否决）
WIN_RATE_IMPROVE = 0.02  # inner 胜率提升 ≥2pp
AVG_RR_IMPROVE = 0.05    # inner 均 rr 提升 ≥0.05
ANN_IMPROVE = 0.01       # inner 年化提升 ≥1pp
OUTER_DD_WORSE_LIMIT = 0.05  # outer 回撤劣化 ≤5pp
OUTER_ANN_FLOOR = -0.30      # outer 年化下限（OOS 不能崩）

# 状态机（合法迁移表）
_LEGAL = {
    ("PENDING", "VERIFYING"),
    ("PENDING", "NEEDS_HUMAN"),
    ("VERIFYING", "APPROVED"),
    ("VERIFYING", "REJECTED"),
    ("APPROVED", "PUBLISHED"),
    ("PENDING", "APPROVED"),    # 人审直接放行（B/C 档人工评估后）
    ("PENDING", "REJECTED"),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_proposal (
  proposal_id       TEXT PRIMARY KEY,
  created_at        TEXT NOT NULL,
  change_type       TEXT NOT NULL,     -- A=参数档 / B=过滤器开关 / C=结构进化
  hypothesis        TEXT NOT NULL,
  params_json       TEXT,              -- A 档参数快照（NecklineConfig 值域校验过）
  expected_effect   TEXT,
  risk              TEXT,
  status            TEXT NOT NULL,     -- PENDING/VERIFYING/APPROVED/REJECTED/PUBLISHED/NEEDS_HUMAN
  verification_json TEXT,
  experiment_id     TEXT,
  note              TEXT);
CREATE INDEX IF NOT EXISTS idx_proposal_status ON research_proposal(status);
"""


def _connect(db_path: str):
    """SQLite 连接上下文（WAL + Row，仿 discovery.store 范式）。"""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db(db_path: str = _DEFAULT_DB) -> None:
    """幂等建表。"""
    with _connect(db_path) as con:
        con.executescript(_SCHEMA)


def create_proposal(db_path: str, *, change_type: str, hypothesis: str,
                    params: dict | None = None, expected_effect: str = "",
                    risk: str = "", note: str = "") -> str:
    """落一条 PENDING 提案（A 档 params 必须过 NecklineConfig 值域，B/C 可空）。"""
    if change_type not in ("A", "B", "C"):
        raise ValueError(f"非法 change_type={change_type!r}（A/B/C）")
    if params is not None:
        _validate_params(params)
    init_db(db_path)
    proposal_id = f"p_{uuid.uuid4().hex[:8]}"
    with _connect(db_path) as con:
        con.execute(
            "INSERT INTO research_proposal(proposal_id, created_at, change_type, hypothesis,"
            " params_json, expected_effect, risk, status, note)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (proposal_id, datetime.now().isoformat(timespec="seconds"), change_type,
             hypothesis, json.dumps(params or {}, ensure_ascii=False),
             expected_effect, risk, "PENDING", note))
    return proposal_id


def _validate_params(params: dict) -> None:
    """A 档参数值域护栏（NecklineConfig 全量校验，防 LLM/手工改非法字段）。"""
    from strategies.neckline.schema import NecklineConfig
    NecklineConfig(**params)


def get_proposal(db_path: str, proposal_id: str) -> dict | None:
    """按 id 读提案（不存在 → None）。"""
    init_db(db_path)
    with _connect(db_path) as con:
        row = con.execute(
            "SELECT * FROM research_proposal WHERE proposal_id=?", (proposal_id,)).fetchone()
    return dict(row) if row else None


def list_proposals(db_path: str, status: str | None = None) -> list[dict]:
    """列提案（created_at 降序；可按状态过滤）。"""
    init_db(db_path)
    with _connect(db_path) as con:
        if status:
            rows = con.execute(
                "SELECT * FROM research_proposal WHERE status=? ORDER BY created_at DESC",
                (status,)).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM research_proposal ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def _transition(db_path: str, proposal_id: str, new_status: str, note: str = "") -> None:
    """状态迁移（合法表校验；非法抛 ValueError）。"""
    p = get_proposal(db_path, proposal_id)
    if p is None:
        raise ValueError(f"提案不存在: {proposal_id}")
    if (p["status"], new_status) not in _LEGAL:
        raise ValueError(f"非法迁移: {p['status']}→{new_status}")
    with _connect(db_path) as con:
        con.execute(
            "UPDATE research_proposal SET status=?, note=COALESCE(?, note) WHERE proposal_id=?",
            (new_status, note or None, proposal_id))


def mark_verifying(db_path: str, proposal_id: str) -> None:
    _transition(db_path, proposal_id, "VERIFYING")


def mark_approved(db_path: str, proposal_id: str, note: str = "") -> None:
    _transition(db_path, proposal_id, "APPROVED", note)


def mark_rejected(db_path: str, proposal_id: str, note: str = "") -> None:
    _transition(db_path, proposal_id, "REJECTED", note)


def mark_published(db_path: str, proposal_id: str, experiment_id: str) -> None:
    """APPROVED → PUBLISHED（记 experiment_id 溯源）。"""
    p = get_proposal(db_path, proposal_id)
    if p is None or p["status"] != "APPROVED":
        raise ValueError(f"仅 APPROVED 可 publish（当前 {p['status'] if p else '不存在'}）")
    _transition(db_path, proposal_id, "PUBLISHED")
    with _connect(db_path) as con:
        con.execute("UPDATE research_proposal SET experiment_id=? WHERE proposal_id=?",
                    (experiment_id, proposal_id))


# ============================================================================
# A 档自动验证
# ============================================================================
def _judge(baseline: dict | None, proposal: dict) -> tuple[bool, str]:
    """门槛判定：inner 改善 + outer 不显著劣化 → (True, reason)；否则 (False, reason)。"""
    inner_p = proposal.get("inner") or {}
    inner_b = (baseline or {}).get("inner") or {}
    outer_p = proposal.get("outer") or {}
    outer_b = (baseline or {}).get("outer") or {}
    if inner_p.get("n_hits", 0) < MIN_HITS:
        return False, f"inner 样本不足（{inner_p.get('n_hits', 0)} < {MIN_HITS}）"
    improved = any([
        inner_p.get("win_rate", 0) >= inner_b.get("win_rate", 0) + WIN_RATE_IMPROVE,
        inner_p.get("avg_rr", 0) >= inner_b.get("avg_rr", 0) + AVG_RR_IMPROVE,
        inner_p.get("annualized_return", 0) >= inner_b.get("annualized_return", 0) + ANN_IMPROVE,
    ])
    if not improved:
        return False, "inner 无改善（胜率/均rr/年化均未达门槛）"
    dd_worse = (outer_p.get("max_drawdown", 0) or 0) - (outer_b.get("max_drawdown", 0) or 0)
    if dd_worse > OUTER_DD_WORSE_LIMIT:
        return False, f"outer 回撤劣化 {dd_worse:.1%} 超限（>{OUTER_DD_WORSE_LIMIT:.0%}）"
    if outer_p.get("annualized_return", 0) < OUTER_ANN_FLOOR:
        return False, f"outer 年化 {outer_p.get('annualized_return', 0):.1%} 低于下限"
    return True, "inner 改善且 outer 未显著劣化"


def verify_proposal(db_path: str, proposal_id: str, lake_start: str = "2025-01-01") -> bool:
    """A 档自动验证：基线（ACTIVE）vs 提案 → 门槛判定 → APPROVED/REJECTED + 结果落库。

    基线缺失（无 ACTIVE 实验）→ 只做绝对门槛（inner 达标 + outer 下限），
    MVP 保持保守：无基线时直接 REJECTED（研究侧不猜实盘口径）。
    """
    p = get_proposal(db_path, proposal_id)
    if p is None:
        raise ValueError(f"提案不存在: {proposal_id}")
    mark_verifying(db_path, proposal_id)
    from discovery.split import holdout_split
    split = holdout_split()
    universe, _ = freeze(lake_start)
    active = resolve_active()
    baseline_params = dict(active[0].params) if active else None
    if baseline_params is None:
        result = {"verdict": "rejected", "reason": "无 ACTIVE 基线，拒绝自动放行",
                  "baseline": None, "proposal": None}
        mark_rejected(db_path, proposal_id, note=result["reason"])
        _save_verification(db_path, proposal_id, result)
        return False
    res_base = evaluate_replay(baseline_params, universe, split)
    res_prop = evaluate_replay(json.loads(p["params_json"]), universe, split)
    ok, reason = _judge(res_base, res_prop)
    result = {
        "verdict": "approved" if ok else "rejected",
        "reason": reason,
        "baseline": res_base.get("inner"),
        "proposal": res_prop.get("inner"),
        "baseline_outer": res_base.get("outer"),
        "proposal_outer": res_prop.get("outer"),
    }
    _save_verification(db_path, proposal_id, result)
    if ok:
        mark_approved(db_path, proposal_id, note=reason)
    else:
        mark_rejected(db_path, proposal_id, note=reason)
    return ok


def _save_verification(db_path: str, proposal_id: str, result: dict) -> None:
    with _connect(db_path) as con:
        con.execute(
            "UPDATE research_proposal SET verification_json=? WHERE proposal_id=?",
            (json.dumps(result, ensure_ascii=False, default=str), proposal_id))


# ============================================================================
# Agent 提案生成（LLM + schema 护栏）
# ============================================================================
def generate_proposal(db_path: str, digest_md: str, history: list[dict],
                      max_pending: int = 2) -> str | None:
    """LLM 基于研究摘要 + 历史生成一条提案 → 落 PENDING；GLM 不可用/已有足量 PENDING → None。

    max_pending：PENDING 提案已达上限则不再生成（防 Agent 刷屏/重复实验）。
    """
    pending = [p for p in list_proposals(db_path, status="PENDING")]
    if len(pending) >= max_pending:
        logger.info("已有 %d 条 PENDING 提案（上限 %d），跳过生成", len(pending), max_pending)
        return None
    from strategies.neckline.schema import NecklineConfig
    fields = ", ".join(NecklineConfig.model_fields.keys())
    history_str = json.dumps(history[-5:], ensure_ascii=False, default=str)
    prompt = f"""你是量化策略研究员。基于今日研究摘要与近期实验历史，生成一条**可执行的**
参数探索提案（A 档：只改参数，无需改代码）。必须严格输出 JSON，不要任何解释。

## 今日研究摘要
{digest_md}

## 近期实验/提案历史（防重复）
{history_str}

## 合法参数字段（只能改这些）
{fields}

## 输出格式
{{"change_type": "A", "hypothesis": "一句话假设", "params": {{字段: 新值}},
  "expected_effect": "预期效果", "risk": "风险"}}

规则：
- change_type 只能是 "A"（MVP 只自动验证 A 档；B/C 档由人工后续评估）；
- params 只改 1-3 个字段，值必须在合理范围（参考 NecklineConfig 约束）；
- 不要重复历史里已试过的相同参数组合。"""
    try:
        raw = get_llm_client().call(prompt)
    except Exception as exc:
        logger.warning("提案生成 LLM 调用失败（降级不生成）：%s", exc)
        return None
    raw = raw.strip().strip("`")
    if raw.lower().startswith("json"):
        raw = raw[4:].strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("提案生成 LLM 返回非 JSON（丢弃）：%s", exc)
        return None
    params = parsed.get("params") or {}
    if params:
        try:
            _validate_params(params)
        except Exception as exc:
            logger.warning("提案参数值域非法（丢弃）：%s", exc)
            return None
    return create_proposal(
        db_path,
        change_type=parsed.get("change_type", "A"),
        hypothesis=parsed.get("hypothesis", "")[:200] or "（无假设）",
        params=params or None,
        expected_effect=str(parsed.get("expected_effect", ""))[:200],
        risk=str(parsed.get("risk", ""))[:200],
        note="agent",
    )


# ============================================================================
# 钉钉审核解析（规则式，不依赖 LLM——快、稳、可单测）
# ============================================================================
_REVIEW_RE = re.compile(
    r"(通过|同意|approve|ok)\s*(?:提案)?\s*(p_[0-9a-f]{8})", re.IGNORECASE)
_REJECT_RE = re.compile(
    r"(否决|拒绝|reject|no)\s*(?:提案)?\s*(p_[0-9a-f]{8})", re.IGNORECASE)


def parse_review(text: str) -> dict | None:
    """解析钉钉审核文本 → {action: approve/reject, proposal_id}；无法解析 → None。"""
    m = _REVIEW_RE.search(text)
    if m:
        return {"action": "approve", "proposal_id": m.group(2)}
    m = _REJECT_RE.search(text)
    if m:
        return {"action": "reject", "proposal_id": m.group(2)}
    return None


def submit_review(db_path: str, text: str) -> dict:
    """钉钉审核入口：parse → 状态迁移 → 返回结果（供 API/bridge 回显）。"""
    parsed = parse_review(text)
    if parsed is None:
        return {"ok": False, "message": "没识别到提案 id，请用「通过 p_xxxxxxxx」或「否决 p_xxxxxxxx」"}
    pid = parsed["proposal_id"]
    p = get_proposal(db_path, pid)
    if p is None:
        return {"ok": False, "message": f"提案不存在: {pid}"}
    try:
        if parsed["action"] == "approve":
            # VERIFYING/REJECTED 恢复？MVP：仅 PENDING/VERIFYING 可 approve
            mark_approved(db_path, pid, note="钉钉人审通过")
        else:
            mark_rejected(db_path, pid, note="钉钉人审否决")
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "proposal_id": pid, "action": parsed["action"],
            "status": get_proposal(db_path, pid)["status"]}


# ============================================================================
# publish 桥（APPROVED → experiment DRAFT，promote 仍留人审）
# ============================================================================
def _create_experiment_draft(params: dict, source: str) -> str:
    """调 experiment.store 建 DRAFT（weight=0），返回 experiment_id。

    独立函数便于测试 patch；与 discovery.publish 的建桥逻辑同源（DRAFT 不自动
    promote——过拟合参数若直冲 ACTIVE 会绕开人审，红线在 test 钉死）。
    """
    from datetime import datetime as _dt
    from experiment.models import ExperimentStatus, ExperimentVersion
    from experiment.store import (_DEFAULT_DB as _EXP_DB, create_version,
                                  init_db as _init_exp, list_versions)
    _init_exp()
    today = _dt.now().strftime("%Y%m%d")
    existing = [v.version for v in list_versions(_EXP_DB) if v.strategy_name == "neckline"]
    experiment_id = f"neckline_prop_{today}_{source[-6:]}"
    version = ExperimentVersion(
        experiment_id=experiment_id, strategy_name="neckline", params=params,
        weight=0.0, status=ExperimentStatus.DRAFT,
        version=max(existing) + 1 if existing else 1,
        source=f"research_proposal:{source}", note="Phase C 提案自动验证通过",
        created_at=_dt.now().isoformat(timespec="seconds"))
    create_version(_EXP_DB, version, operator="research:proposal")
    return experiment_id


def publish_proposal(db_path: str, proposal_id: str) -> str:
    """APPROVED 提案 → experiment DRAFT → PUBLISHED（返回 experiment_id）。"""
    p = get_proposal(db_path, proposal_id)
    if p is None or p["status"] != "APPROVED":
        raise ValueError(f"仅 APPROVED 可 publish（当前 {p['status'] if p else '不存在'}）")
    params = json.loads(p["params_json"])
    exp_id = _create_experiment_draft(params, proposal_id)
    mark_published(db_path, proposal_id, exp_id)
    return exp_id
