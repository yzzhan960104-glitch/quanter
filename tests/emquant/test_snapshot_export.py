# -*- coding: utf-8 -*-
"""参数/universe 快照静态产物测试（emquant 试点 Task 3 · spec FR1 / AC4）。

物理定位：
    钉死 emquant/config/params_snapshot.json + universe.json 两份【定稿产物】的形状与
    自洽性——它们是掘金单文件 §0 参数区的唯一数据源，产物变质（漏键/指纹漂移/
    universe 混入非创板科创标的）= pilot 参数错档 = 双轨对照事故，必须在 CI 期暴露。

    ⚠️ 静态产物测试红线：本文件【不重跑导出器】（emquant/export_snapshot.py）——
    重跑会依赖 data_lake 时效 / 实验 DB 状态 / .env，测试就不再是对"已定稿快照"
    的守卫而成对环境的依赖（flaky）。导出器自身的正确性由其内置的导出期断言
    （键集/C9 漏键/pos_cap 实弹/universe 约束/指纹 round-trip）承担。

口径注记（universe 断言方向的勘误）：
    spec FR1 / plan Task 3 原文写「剔 300/301/688/689 前缀」，与代码真身方向相反——
    引擎实盘 load_universe（trading/data_ctx.py:37-56）与回测 _filter_chuangke_kechuang
    （strategies/neckline/backtest.py:122-131）均为【只保留】创板科创前缀（主板/北交所
    不在可交易池，tests/test_neckline_core.py:421 钉死同口径）。本测试按代码真身断言
    all(创板科创)，勘误全文留档于快照 notes.universe_semantics_erratum。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

# 项目根 = tests/emquant/ 上两级（tests/conftest.py 已注入 sys.path，此处只为定位产物）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_PARAMS_JSON = _PROJECT_ROOT / "emquant" / "config" / "params_snapshot.json"
_UNIVERSE_JSON = _PROJECT_ROOT / "emquant" / "config" / "universe.json"

# —— 快照定型键集（字面量钉死：上游 DEFAULTS/EXEC_DEFAULTS/_trade_cfg 加键属有意
#    变更，应显式更新本表并重导快照，而非测试静默跟随）——
# 识别层 12 键（method_v0.DEFAULTS 11 + momentum_gate R4-H1）
ID_KEYS = {
    "window", "min_touches", "min_suppression", "local_extrema_window", "min_bottoms",
    "breakout_vol_mult", "min_rr", "max_h_atr", "stop_atr_mult", "tp_h_mult", "decay_tau",
    "momentum_gate",
}
# 执行层 16 键（EXEC_DEFAULTS：生命周期/挂单/止盈/trailing 三件 + 费率三键 +
# R6-5 腿 A/B 三键——2026-08-26 R6-8 冠军快照换代时显式更新本表）
EXEC_KEYS = {
    "max_holding", "max_wait", "cooldown", "buy_limit_atr_mult",
    "tp1_h_mult", "tp1_portion", "cancel_thresh_mult",
    "trailing_grace", "trailing_step", "trailing_floor",
    "commission_rate", "stamp_rate", "transfer_rate",
    "chase_entry", "timeout_extend_days", "timeout_extend_min_pnl",
    "tp_adapt_h_atr", "tp_adapt_scale",   # R6-10 B3
}
# .env 实弹 14 键（trading/critical.py:187 _trade_cfg 全键）
TRADE_KEYS = {
    "pos_cap", "stop_atr_mult", "tp_h_mult", "max_wait",
    "tp1_h_mult", "tp1_portion", "cancel_thresh_mult",
    "grace", "step", "floor", "max_holding",
    "sizing_mode", "kelly_fraction", "kelly_hat",
}
# C9 漏键防御的最小必备子集（spec AC4 点名：buy_limit_atr_mult/cooldown/trailing 三件）
EXEC_REQUIRED = {"buy_limit_atr_mult", "cooldown",
                 "trailing_grace", "trailing_step", "trailing_floor"}

# 创板科创前缀池（与 data_ctx.load_universe / backtest._filter_chuangke_kechuang 同款）
CK_PREFIXES = ("300", "301", "688", "689")
# ts 六位代码.市场后缀 格式（data_lake MultiIndex symbol level 的既定形状）
_TS_FMT = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


def _load_params() -> dict:
    return json.loads(_PARAMS_JSON.read_text(encoding="utf-8"))


def _load_universe() -> dict:
    return json.loads(_UNIVERSE_JSON.read_text(encoding="utf-8"))


def _recompute_fingerprint(params: dict) -> str:
    """按快照 notes.fingerprint_formula 单源公式重算指纹（Task 4+ 审计同式复用）。

    公式（plan Task 3 明文）：sha256(json.dumps({id,exec,trade,universe_symbols},
    sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()[:16]——
    刻意排除 exported_at/sources/notes（时间戳/注记变化不得误伤指纹 = 幂等红线）。
    """
    canon = {
        "id_params": params["id_params"],
        "exec_params": params["exec_params"],
        "trade_cfg": params["trade_cfg"],
        "universe_symbols": params["universe_symbols"],
    }
    return hashlib.sha256(
        json.dumps(canon, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]


# ============================================================================
# ① params_snapshot.json：schema / 键集 / 实弹锚
# ============================================================================
def test_params_snapshot_schema():
    """顶层 schema：七区块齐全（brief Task 3 Produces 契约）。"""
    d = _load_params()
    assert set(d) == {
        "id_params", "exec_params", "trade_cfg", "universe_symbols",
        "fingerprint", "exported_at", "sources", "notes",
    }


def test_id_params_key_set():
    """识别层 11 键定型（多键=脏 override、少键=合并吞键，键集不允许漂移）。"""
    assert set(_load_params()["id_params"]) == ID_KEYS


def test_exec_params_key_set_and_required():
    """执行层 13 键定型 + C9 必备子集点名（漏 buy_limit_atr_mult/cooldown/trailing
    任一件 = pilot §0 对应行为静默退默认，AC4 红线）。"""
    d = _load_params()
    assert set(d["exec_params"]) == EXEC_KEYS
    assert EXEC_REQUIRED <= set(d["exec_params"])


def test_exec_cost_keys_present():
    """费率三键必须在（回测/实盘成本口径单源；实验 schema 不含它们=恒默认值）。"""
    d = _load_params()
    for k in ("commission_rate", "stamp_rate", "transfer_rate"):
        assert k in d["exec_params"]


def test_trade_cfg_key_set_and_live_pos_cap():
    """trade_cfg 14 键 + pos_cap=0.05 实弹锚（.env 实弹值，导出期已自检，此处钉产物）。"""
    d = _load_params()
    assert set(d["trade_cfg"]) == TRADE_KEYS
    assert d["trade_cfg"]["pos_cap"] == 0.05


# ============================================================================
# ② fingerprint：稳定公式重算 + exported_at 排除证明
# ============================================================================
def test_fingerprint_recompute_equal():
    """对同一 json 按公示公式重算指纹 == 落盘值（快照未被篡改/序列化无漂移）。"""
    d = _load_params()
    assert _recompute_fingerprint(d) == d["fingerprint"]


def test_fingerprint_excludes_exported_at():
    """exported_at 参与排除证明：篡改时间戳后重算指纹不变（幂等红线——时间戳/
    注记变化不得误伤指纹）。"""
    d = _load_params()
    tampered = dict(d, exported_at="1970-01-01")
    assert _recompute_fingerprint(tampered) == d["fingerprint"]


# ============================================================================
# ③ universe.json：规模 / 口径 / 格式 / 跨文件一致
# ============================================================================
def test_universe_schema_and_cap():
    """schema 三键（brief Produces 契约）+ ≤300（spec FR1 试点规模闸）。"""
    u = _load_universe()
    assert set(u) == {"symbols", "source", "exported_at"}
    assert len(u["symbols"]) <= 300
    assert u["source"].strip()  # 来源口径必须非空留档（含勘误全文）


def test_universe_all_chuangke_kechuang():
    """全部创板科创前缀（引擎 load_universe 只保留口径；spec『剔』为措辞勘误，
    见模块 docstring 注记）+ ts 六位代码格式 + 无重复。"""
    syms = _load_universe()["symbols"]
    assert all(str(s).split(".")[0][:3] in CK_PREFIXES for s in syms)
    assert all(_TS_FMT.match(str(s)) for s in syms)
    assert len(set(syms)) == len(syms)


def test_universe_matches_params_snapshot():
    """跨文件一致：params_snapshot 的指纹参与项与 universe.json 同源同序。"""
    assert _load_params()["universe_symbols"] == _load_universe()["symbols"]


# ============================================================================
# ④ notes/sources：cooldown 结论与来源留档（plan FR1 明文要求）
# ============================================================================
def test_notes_cooldown_semantics_documented():
    """cooldown 结论必须有据：conclusion 非空 + file:line 证据列表 + 实弹值与
    exec_params.cooldown 一致（引擎 _eod 跨日去重口径，pilot 复刻依据）。"""
    notes = _load_params()["notes"]
    cd = notes["cooldown_semantics"]
    assert cd["conclusion"].strip()
    assert isinstance(cd["evidence"], list) and len(cd["evidence"]) >= 3
    assert all(".py:" in ev for ev in cd["evidence"])  # 逐条 file:line 证据
    assert cd["live_cooldown_days"] == _load_params()["exec_params"]["cooldown"]


def test_sources_documented():
    """四路来源留档齐全（id/exec/trade/universe 各自 file:line 级口径说明）。"""
    sources = _load_params()["sources"]
    for k in ("id_params", "exec_params", "trade_cfg", "universe"):
        assert k in sources and str(sources[k]).strip()
