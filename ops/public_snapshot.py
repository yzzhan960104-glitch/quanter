# -*- coding: utf-8 -*-
"""公网只读快照生成（yzzhan.xin · Cloudflare Pages 静态发布的唯一数据源）。

物理意图：把「查询面」从 server API 搬成静态 JSON——公开站点物理只读（写入
通路在结构上不存在、家宽零入站），发布链=本脚本 → vite 静态模式构建
（.env.public VITE_STATIC_DATA=1）→ ops/publish_public.py wrangler 直推。

数据源与 server 路由完全同源（ops 层读函数直调，不经 HTTP）：
  gm 家族  ← ops.gm_ops_common（7002 Bearer 只活在本进程，绝不进快照）
  ab/audit ← ops.emquant_ab_compare + experiments.db（file:…?mode=ro 单写者纪律）
  round    ← emquant/ab_round.json
  datasets ← presentation.server.services.data_service（与 GET /data/datasets 同源）

脱敏红线（公开=全世界可看，含搜索引擎）：
  - account_id → 前 8 位 + '…'（辨识腿即可，不泄全号）
  - detail/任意层键名含 token/secret 的字段剔除；account 字段值同掩码
  - 绝不入快照：掘金 token、次日计划预演（前跑风险红线）、内网路径、
    discovery 试验面（研究 IP：参数发现敏感性/搜索过程——与 P6 公开的
    研究提案/版本/队列不同源，见 2026-09-02 方案 §6 vs §〇.7：研究线
    三快照按方案明文裁决公开，discovery 仍收敛至鉴权后 P7 开放）

产物：presentation/web/public/data/*.json（vite 构建期拷入 dist/data/；目录
已 gitignore——快照是发布期再生产物，不是源码）。文件形状=前端 facade 静态
分支的「返回值」形状（static.ts 约定）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

OUT_DIR = ROOT / "presentation" / "web" / "public" / "data"
_AB_DB = ROOT / "experiment" / "experiments.db"

# 键名含这些子串的字段整键剔除（大小写不敏感）
_SENSITIVE_KEY_MARKS = ("token", "secret", "password")
# 键名含 account 的字段值掩码（7002 实测键名：account_id/account_name——保留键名、
# 值脱敏到前 8 位，辨识腿足够、不泄全号）
_ACCOUNT_KEY_MARK = "account"


def _mask_account(v):
    s = str(v or "")
    return s[:8] + "…" if len(s) > 8 else s


def sanitize(obj):
    """递归脱敏：敏感键剔除 + account* 键值掩码（公开红线的最后一道闸）。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if any(m in kl for m in _SENSITIVE_KEY_MARKS):
                continue
            if _ACCOUNT_KEY_MARK in kl:
                out[k] = _mask_account(v)
            else:
                out[k] = sanitize(v)
        return out
    if isinstance(obj, list):
        return [sanitize(x) for x in obj]
    return obj


def _write(name: str, payload) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{name}.json").write_text(
        json.dumps(sanitize(payload), ensure_ascii=False, default=str),
        encoding="utf-8")
    print(f"  ✓ {name}.json")


def _legs_payload():
    """镜像 server /gm/legs 逻辑（gc 单源），account_id 掩码在 sanitize 层。"""
    from ops import gm_ops_common as gc
    token = ""
    try:
        token = str(gc.runtime_config().get("token") or "")
    except Exception:
        pass
    _, strategies = gc.api_get("/v3/strategies", token, timeout=4.0)
    name_map = {s.get("strategy_id"): s.get("name")
                for s in ((strategies or {}).get("data") or []) if isinstance(s, dict)}
    out = []
    for leg in gc.active_legs():
        try:
            cfg = gc.runtime_config(gc.leg_strategy_dir(leg))
        except (OSError, ValueError):
            cfg = {}
        sid = str(cfg.get("strategy_id") or "")
        out.append({"key": leg.key, "label": leg.label,
                    "role": "incumbent" if leg.key == "main" else "challenger",
                    "account_id": str(cfg.get("account_id") or "") or None,
                    "strategy_id": sid or None,
                    "strategy_name": name_map.get(sid) or None})
    return out


def _audit_rows(day: str, account: str, limit: int = 500):
    """镜像 server /gm/audit 的 mode=ro 查询（单写者纪律：绝不写台账库）。"""
    if not _AB_DB.exists():
        return []
    uri = f"file:{_AB_DB.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as con:
        rows = con.execute(
            "SELECT ts, event, detail FROM terminal_audit"
            " WHERE account_id=? AND ts LIKE ? ORDER BY ts DESC LIMIT ?",
            (account, day + "%", limit)).fetchall()
    out = []
    for ts, ev, detail in rows:
        try:
            d = json.loads(detail) if detail else {}
        except ValueError:
            d = {"raw": detail}
        out.append({"ts": ts, "event": ev, "detail": d})
    return out


def build_snapshot(days_ab: int = 10, days_audit: int = 2) -> dict:
    """生成全量快照文件。返回摘要（发布脚本/测试消费）。"""
    from ops import emquant_ab_compare as ab
    from ops import gm_ops_common as gc

    n_files = 0
    t0 = datetime.now()

    def w(name, payload):
        nonlocal n_files
        _write(name, payload)
        n_files += 1

    # ── 腿目录 + overview（全局）──
    legs = _legs_payload()
    w("gm_legs", legs)
    token = str(gc.runtime_config().get("token") or "")
    s1, strategies = gc.api_get("/v3/strategies", token, timeout=4.0)
    s2, accounts = gc.api_get("/v3/account-statuses", token, timeout=4.0)
    w("gm_overview", {"strategies": (strategies or {}).get("data") or [],
                      "account_statuses": (accounts or {}).get("data") or []
                      if s2 == 200 else None} if s1 == 200 else
      {"strategies": [], "account_statuses": None, "degraded": f"7002 status={s1}"})

    # ── 每腿：asset/positions/orders/trades ──
    leg_cfgs = {}
    for leg in gc.active_legs():
        cfg = gc.runtime_config(gc.leg_strategy_dir(leg))
        leg_cfgs[leg.key] = (str(cfg.get("token") or token),
                             str(cfg.get("account_id") or ""))
        lt, la = leg_cfgs[leg.key]
        st, payload = gc.api_get(f"/v3/account-trade/cash/{la}", lt, timeout=4.0)
        # cash 端点 data 是 [row] 列表——写 row 对象（facade 契约 GmAsset；
        # 09-01 用户反馈"资产为空"根因=此前整列表落盘，DualAssetCard 取不到字段）
        w(f"gm_asset_{leg.key}",
          ((payload or {}).get("data") or [{}])[0] if st == 200 else {})
        st, payload = gc.api_get(f"/v3/account-trade/positions/{la}", lt, timeout=4.0)
        positions = (payload or {}).get("data") or [] if st == 200 else []
        _enrich_industry(positions)           # P5.2：行业列富化（环形图数据源）
        w(f"gm_positions_{leg.key}", positions)
        st, payload = gc.api_get(f"/v3/account-trade/orders/{la}", lt, timeout=4.0)
        w(f"gm_orders_{leg.key}", (payload or {}).get("data") or [] if st == 200 else [])
        st, payload = gc.api_get(f"/v3/account-trade/execrpts/{la}", lt, timeout=4.0)
        today_rows = (payload or {}).get("data") or [] if st == 200 else []
        w(f"gm_trades_{leg.key}", today_rows)
        # 成交史（2026-09-02 用户反馈"流水比持仓还少"）：柜台 execrpts 按自然日
        # 滚动只给当日，持仓是累积的——流水必须跨日才对得上。主源=state 订单史
        # （filled>0 的全部历史，12 笔与 8 持仓对账吻合）；今日柜台实况覆盖同单
        # （partial 成交以柜台 latest 为准）。行形状与 execrpts 对齐（前端零改动）。
        w(f"gm_trades_history_{leg.key}",
          _trade_history(gc.leg_strategy_dir(leg), today_rows))

    # ── ab 对照：近 N 日（AbHistoryCard 10 日循环全覆盖；缺日已隐含空窗）──
    main_leg = next((l for l in gc.active_legs() if l.key == "main"), None)
    exp_leg = next((l for l in gc.active_legs() if l.key == "exp"), None)
    for i in range(days_ab):
        day = f"{datetime.now() - timedelta(days=i):%Y-%m-%d}"
        if exp_leg is None or exp_leg not in gc.active_legs():
            w(f"gm_ab_{day}", {"day": day, "single_leg": True,
                               "detail": "实验腿未部署——无对照面"})
            continue
        main_acc = leg_cfgs.get("main", ("", ""))[1]
        exp_acc = leg_cfgs.get("exp", ("", ""))[1]
        payload = ab.compare(day, ab.fetch_rows(day, main_acc, _AB_DB),
                             ab.fetch_rows(day, exp_acc, _AB_DB), main_acc, exp_acc)
        payload["ok"] = not payload["flags"]
        w(f"gm_ab_{day}", payload)

    # ── audit 下钻：近 N 日 × 腿 ──
    for i in range(days_audit):
        day = f"{datetime.now() - timedelta(days=i):%Y-%m-%d}"
        for leg in gc.active_legs():
            acc = leg_cfgs.get(leg.key, ("", ""))[1]
            w(f"gm_audit_{leg.key}_{day}", _audit_rows(day, acc))

    # ── round 档案 ──
    rp = ROOT / "emquant" / "ab_round.json"
    if rp.exists():
        w("gm_round", json.loads(rp.read_text(encoding="utf-8")))

    # ── datasets（数据健康度）──
    try:
        from presentation.server.services import data_service
        ds = data_service.list_datasets()
        w("datasets", [d.model_dump() if hasattr(d, "model_dump") else d for d in ds])
    except Exception as e:
        print(f"  ⚠ datasets 降级空表：{type(e).__name__}: {e}")
        w("datasets", [])

    # ── meta ──
    w("meta", {"generated_at": f"{t0:%Y-%m-%d %H:%M:%S}",
               "mode": "public-readonly-snapshot",
               "note": "公网只读快照：静态发布、零写入通路；account 已掩码、"
                       "敏感键已剔除；数据时点=generated_at，非实时"})

    # ── 净值历史（可视化重构 P1：首页净值曲线族）──
    from ops import nav_history
    doc = nav_history.update()
    n_files += 1
    print(f"  ✓ nav_history.json（{len(doc['days'])} 天，era 起 {doc['era_start']}）")

    # ── 基准指数（需求④：上证/纳指/标普同图对比）──
    from ops import benchmarks as bm
    bdoc = bm.update()
    n_files += 1
    print(f"  ✓ benchmarks.json（轴 {len(bdoc['axis'])} 天 × {len(bdoc['series'])} 基准）")

    # ── TSB 机会观察（09-02：紫金×纽约金、美元指数×US10Y×纽约金）──
    from ops import tsb_data
    tdoc = tsb_data.update()
    n_files += 1
    print(f"  ✓ tsb.json（{ {k: len(v['dates']) for k, v in tdoc['series'].items()} }）")

    # ── OHLCV 快照（P2 静态半场：持仓+当日信号标的的 K 线回放数据）──
    _ohlcv_files(w)                     # w 闭包自增 n_files

    # ── 每日计划卡（09-02：今日实况+预演回看，前跑红线见函数注）──
    _plan_card(w)

    # ── 腿详情档案（需求②③：策略全量信息 + 手动风控参数）──
    _leg_detail_files(w)

    # ── 运维健康面板（P5.4：任务台账+告警时间线+进程拓扑）──
    _ops_health(w)

    # ── 研究线三快照（P6：提案流/版本演进/回测队列）──
    _research_files(w)

    # ── 亏损持仓 LLM 归因（09-03：读当日 loser_review 产物上站）──
    _loser_review(w)
    return {"files": n_files, "generated_at": f"{t0:%Y-%m-%d %H:%M:%S}"}


def _loser_review(w) -> int:
    """当日亏损归因上站（ops/loser_review.py 的产物透传）。

    生成本体在 16:15 cron（或手动 python -m ops.loser_review）——快照只读
    logs/loser_review_{day}.json，缺文件优雅降级 null（16:15 前的盘中发布
    自然看不到当日归因）。内容为 LLM 归因观点+持仓事实，无账户/凭证面。
    """
    today = f"{datetime.now():%Y-%m-%d}"
    src = ROOT / "logs" / f"loser_review_{today}.json"
    if not src.exists():
        w("loser_review", {"day": today, "legs": None,
                           "note": "当日亏损归因尚未生成（16:15 盘后 cron）"})
        return 0
    try:
        doc = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        w("loser_review", {"day": today, "legs": None,
                           "note": f"产物解析失败：{e}"})
        return 0
    w("loser_review", doc)
    return 1


def _research_files(w) -> int:
    """P6 研究线三快照：proposals / experiment_versions / backtest_queue。

    全读现有资产（方案 §五红线：不为可视化造新 DB）：
    - logs/research_proposals.db（mode=ro）：研究提案全生命周期
      （hypothesis/params/expected/risk/verdict）+ 状态管道统计
    - experiment/experiments.db 经 experiment.store.list_versions：版本演进
      （best_annual 从 note「outer ann=XX% calmar=YY」正则抽——store 无指标列）
    - data/replay_tasks.db（mode=ro）：回测队列近 20 行 + docs/research_digest.md
      关键行抽取（实盘 vs 回测期望——漂移对照条数据源）
    """
    import re as _re

    def _j(s):
        try:
            return json.loads(s) if s else None
        except ValueError:
            return {"raw": s}

    # ── 提案流 ──
    proposals: list[dict] = []
    pdb = ROOT / "logs" / "research_proposals.db"
    if pdb.exists():
        try:
            uri = f"file:{pdb.as_posix()}?mode=ro"
            with sqlite3.connect(uri, uri=True) as con:
                rows = con.execute(
                    "SELECT proposal_id, created_at, change_type, hypothesis,"
                    " params_json, expected_effect, risk, status, verification_json,"
                    " experiment_id, note FROM research_proposal"
                    " ORDER BY created_at DESC LIMIT 200").fetchall()
            for (pid, cat, ctype, hyp, pj, eff, risk, status, vj, eid, note) in rows:
                proposals.append({
                    "id": pid, "created_at": cat, "change_type": ctype,
                    "hypothesis": hyp, "params": _j(pj),
                    "expected": eff, "risk": risk, "status": status,
                    "verification": _j(vj), "experiment_id": eid, "note": note})
        except sqlite3.Error as e:
            print(f"  ⚠ proposals 降级空表：{e}")
    pipe: dict[str, int] = {}
    for p in proposals:
        pipe[p["status"]] = pipe.get(p["status"], 0) + 1
    w("proposals", {"generated_at": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
                    "pipeline": pipe, "rows": proposals,
                    "note": "研究提案流（logs/research_proposals.db 只读）：假设→"
                            "参数→预期→验证 verdict 全链留痕"})

    # ── 版本演进 ──
    versions: list[dict] = []
    try:
        from experiment.store import list_versions
        for v in list_versions(str(_AB_DB)):
            d = v if isinstance(v, dict) else {
                k: getattr(v, k) for k in ("experiment_id", "strategy_name",
                                           "params", "weight", "status", "version",
                                           "source", "note", "created_at")}
            note = str(d.get("note") or "")
            ann = _re.search(r"ann=([0-9.]+)%", note)
            cal = _re.search(r"calmar=([0-9.]+)", note)
            versions.append({
                "experiment_id": d.get("experiment_id"),
                "strategy_name": d.get("strategy_name"),
                "version": d.get("version"),
                "status": str(d.get("status") or "").split(".")[-1],  # 枚举短名
                "weight": d.get("weight"),
                "best_annual": float(ann.group(1)) if ann else None,
                "calmar": float(cal.group(1)) if cal else None,
                "note": note, "created_at": d.get("created_at"),
                "params": d.get("params") or {}})
    except Exception as e:
        print(f"  ⚠ experiment_versions 降级空表：{type(e).__name__}: {e}")
    w("experiment_versions", {
        "generated_at": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
        "rows": versions,
        "note": "版本演进（experiment.store.list_versions 只读）；best_annual/calmar"
                " 从版本 note「outer ann=% calmar=」抽取（store 无指标列）"})

    # ── 回测队列 + digest 对照 ──
    tasks: list[dict] = []
    rdb = ROOT / "data" / "replay_tasks.db"
    if rdb.exists():
        try:
            uri = f"file:{rdb.as_posix()}?mode=ro"
            with sqlite3.connect(uri, uri=True) as con:
                rows = con.execute(
                    "SELECT task_id, created_at, status, progress, start, end,"
                    " cfg_override, error, finished_at FROM replay_tasks"
                    " ORDER BY created_at DESC LIMIT 20").fetchall()
            for (tid, cat, status, prog, s0, s1, cfg, err, fin) in rows:
                tasks.append({"task": tid[:8], "created_at": cat,
                              "status": status, "progress": prog,
                              "window": f"{s0}~{s1}",
                              "cfg": _j(cfg) if isinstance(cfg, str) else cfg,
                              "error": err, "finished_at": fin})
        except sqlite3.Error as e:
            print(f"  ⚠ replay_tasks 降级空表：{e}")

    digest: dict = {}
    dmd = ROOT / "docs" / "research_digest.md"
    if dmd.exists():
        text = dmd.read_text(encoding="utf-8", errors="replace")
        # 只取首个「# 研究摘要」块（code-review J-13：五个正则若全文首匹配，
        # writer 改追加历史段后会静默钉在最早那天——锚定头块后追加无害）
        nxt = text.find("# 研究摘要", 1)
        text = text[:nxt] if nxt > 0 else text
        m = _re.search(r"# 研究摘要 (\d{4}-\d{2}-\d{2})", text)
        live_n = _re.search(r"实盘成交：(\S+)", text)
        live_wr = _re.search(r"胜率：(\S+)", text)
        live_rr = _re.search(r"均 rr：(\S+)", text)
        exp = _re.search(r"期望：成交 (\S+) 笔 / 胜率 (\S+) / 均 rr (\S+)", text)
        drift = _re.search(r"漂移对比\n- 状态：\*{0,2}([^*\n]+)", text)
        digest = {
            "day": m.group(1) if m else None,
            "live": {"trades": live_n.group(1) if live_n else None,
                     "win_rate": live_wr.group(1) if live_wr else None,
                     "avg_rr": live_rr.group(1) if live_rr else None},
            "expect": {"trades": exp.group(1) if exp else None,
                       "win_rate": exp.group(2) if exp else None,
                       "avg_rr": exp.group(3) if exp else None},
            "drift": drift.group(1).strip() if drift else None,
        }
    w("backtest_queue", {
        "generated_at": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
        "tasks": tasks, "digest": digest,
        "note": "回测队列（data/replay_tasks.db 只读近 20）+ digest 摘要"
                "（docs/research_digest.md 关键行抽取：实盘 vs 回测期望漂移对照）"})
    return 0


def _ops_health(w) -> int:
    """运维健康快照 ops_health.json（P5.4 /ops 三源）。

    a) job_run 近 14 日台账（logs/trading_job_run.db 只读——单写者纪律 mode=ro）；
    b) alerts.log 尾部解析（`ts | LEVEL | msg` 行；钉钉播报正文无时间戳行跳过），
       非 '-' 级取最近 200 条；
    c) 进程拓扑快照时点捕获：server（engine_processes）+ 掘金终端双探测
       （gm_terminal_status）+ 每腿策略 stage（7002 /v3/strategies）。
    任何一源失败降级空/None 不炸主链（探测失败≠进程不存在，None=未知）。
    """
    import re
    from datetime import timedelta as _td
    from ops import gm_ops_common as gc

    # a) job_run 台账
    runs: list[dict] = []
    db = ROOT / "logs" / "trading_job_run.db"
    if db.exists():
        try:
            since = f"{datetime.now() - _td(days=14):%Y-%m-%d}"
            uri = f"file:{db.as_posix()}?mode=ro"
            with sqlite3.connect(uri, uri=True) as con:
                rows = con.execute(
                    "SELECT job_name, business_date, status, started_at, finished_at,"
                    " message FROM job_run WHERE started_at >= ?"
                    " ORDER BY started_at DESC", (since,)).fetchall()
            runs = [{"job": j, "date": b, "status": s, "started_at": sa,
                     "finished_at": fa, "message": msg}
                    for j, b, s, sa, fa, msg in rows]
        except sqlite3.Error as e:
            print(f"  ⚠ job_run 台账降级空表：{e}")

    # b) alerts 尾部（600 行窗口 → 带时间戳行 → 最近 200）
    alerts: list[dict] = []
    log = ROOT / "logs" / "alerts.log"
    if log.exists():
        try:
            tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-600:]
            pat = re.compile(
                r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\s*\|\s*([A-Za-z]+)\s*\|\s*(.*)$")
            for line in reversed(tail):                # 新→旧收，够 200 止
                m = pat.match(line.strip())
                if not m or m.group(2) == "-":
                    continue
                alerts.append({"ts": m.group(1), "level": m.group(2),
                               "msg": m.group(3)[:300]})
                if len(alerts) >= 200:
                    break
        except OSError as e:
            print(f"  ⚠ alerts 解析降级空表：{e}")

    # c) 进程拓扑（快照时点）：server + 终端双探测 + 每腿策略 stage
    procs: dict = {"note": "快照时点探测（非实时）；None=探测失败（≠不存在）"}
    try:
        from ops import process_topology as pt
        eng = pt.engine_processes()
        procs["server"] = {"running": bool(eng), "pids": [p["pid"] for p in eng]}
        gm = pt.gm_terminal_status()
        procs["emgm3"] = gm.get("emgm3")               # 终端 UI 进程数
        procs["gateway"] = gm.get("gateway")           # 本地网关（7001-7004）
    except Exception as e:
        procs["probe_error"] = f"{type(e).__name__}: {e}"
    strategies: dict = {}
    try:
        token = str(gc.runtime_config().get("token") or "")
        st, payload = gc.api_get("/v3/strategies", token, timeout=4.0)
        if st == 200:
            for s in (payload or {}).get("data") or []:
                if isinstance(s, dict) and s.get("strategy_id"):
                    strategies[str(s["strategy_id"])] = str(s.get("stage") or "")
    except Exception:
        pass
    legs_stage = []
    for leg in gc.active_legs():
        try:
            cfg = gc.runtime_config(gc.leg_strategy_dir(leg))
        except (OSError, ValueError):
            cfg = {}
        sid = str(cfg.get("strategy_id") or "")
        legs_stage.append({"key": leg.key, "label": leg.label,
                           "stage": strategies.get(sid) or None})
    procs["legs"] = legs_stage

    w("ops_health", {
        "generated_at": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
        "job_runs": runs,
        "alerts": alerts,
        "processes": procs,
        "note": "任务台账=job_run 近 14 日（mode=ro 只读）；告警=alerts.log 尾部"
                " 200 条（WARN/CRITICAL 为主，INFO 灰显）；进程=快照时点探测",
    })
    return 0


def _leg_detail_files(w) -> int:
    """每腿策略全量档案 leg_detail_<leg>.json。

    数据源三路合一：①终端部署的 main.py（importlib 读 §0 常量——**运行真相**，
    不是仓库副本；main.py 有 __main__ 闸，import 不起策略）②state/ 下人工风控
    文件化身（RISK_BLOCK.flag 存在性 + CAP.txt 仓位上限）③/gm/legs 身份。
    ①失败（文件异常）→ 参数区缺省展示 error 字段，不炸快照主链。
    """
    import importlib.util as ilu
    from ops import gm_ops_common as gc

    identity = {l["key"]: l for l in _legs_payload()}
    for leg in gc.active_legs():
        leg_dir = gc.leg_strategy_dir(leg)
        detail = {"leg": identity.get(leg.key, {"key": leg.key, "label": leg.label})}
        try:
            spec = ilu.spec_from_file_location(f"pilot_detail_{leg.key}",
                                               leg_dir / "main.py")
            m = ilu.module_from_spec(spec)
            sys.modules[spec.name] = m
            spec.loader.exec_module(m)
            detail.update({
                "build_stamp": getattr(m, "PILOT_BUILD_STAMP", None),
                "id_params": dict(getattr(m, "ID_PARAMS", {}) or {}),
                "exec_params": dict(getattr(m, "EXEC_PARAMS", {}) or {}),
                "trade_cfg": dict(getattr(m, "TRADE_CFG", {}) or {}),
                "amihud_filter": dict(getattr(m, "AMIHUD_FILTER", {}) or {}),
                "universe_size": len(getattr(m, "UNIVERSE", []) or []),
                "daily_order_cap": getattr(m, "PILOT_MAX_NEW_ORDERS_PER_DAY", None),
            })
        except Exception as e:
            detail["artifact_error"] = f"{type(e).__name__}: {e}"
        # ② 人工风控文件化身（ADR-16：state/RISK_BLOCK.flag + state/CAP.txt）
        state_dir = leg_dir / "state"
        detail["risk"] = {
            "risk_block": (state_dir / "RISK_BLOCK.flag").exists(),
            "cap_total": _read_cap(state_dir / "CAP.txt"),
            "note": "RISK_BLOCK=增量挂单人工闸（存在即拦）；CAP.txt=人工总仓位"
                    "上限（缺省 1.0）；pos_cap=单票仓位上限（TRADE_CFG）",
        }
        w(f"leg_detail_{leg.key}", detail)
    return 0


def _read_cap(path: Path) -> float:
    """CAP.txt → [0,1] 人工总仓位上限（缺省 1.0；坏值防御回 1.0）。"""
    try:
        v = float(path.read_text(encoding="utf-8").strip())
        return v if 0.0 <= v <= 1.0 else 1.0
    except (OSError, ValueError):
        return 1.0


_INDUSTRY_MAP: dict[str, str] | None = None


def _industry_map() -> dict[str, str]:
    """ts_code → 行业（stock_basic.parquet；模块级缓存一次，缺库返空表降级）。

    NaN 行业剔除（code-review J-14）：str(nan)='nan' 会产出字面 'nan' 扇区
    而非走「未分类」兜底。
    """
    global _INDUSTRY_MAP
    if _INDUSTRY_MAP is None:
        import pandas as pd
        try:
            basic = pd.read_parquet(ROOT / "data_lake" / "stock_basic.parquet",
                                    columns=["ts_code", "industry"])
            _INDUSTRY_MAP = {str(t): str(i) for t, i in
                             zip(basic["ts_code"], basic["industry"])
                             if pd.notna(i)}
        except (OSError, KeyError, ImportError):
            _INDUSTRY_MAP = {}
    return _INDUSTRY_MAP


def _enrich_industry(positions: list) -> None:
    """positions 行内挂 industry（SZSE.300433 → 300433.SZ join；缺映射置 None）。"""
    imap = _industry_map()
    for r in positions:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol") or "")
        ex, _, code = sym.partition(".")
        ts = f"{code}.{('SH' if ex == 'SHSE' else 'SZ' if ex == 'SZSE' else ex)}"
        r["industry"] = imap.get(ts)


def leg_dir(leg):
    """leg 对象 → 策略目录（免 import gm_ops_common 的便捷转发）。"""
    from ops import gm_ops_common as gc
    return gc.leg_strategy_dir(leg)


_SH_CAL: list[str] | None = None

def _sh_calendar() -> list[str]:
    """A 股交易日历（升序 ISO）：基准缓存的 000001.SH 日线（全量 2010 起），
    缺则降级湖 index_daily。模块级缓存一次。"""
    global _SH_CAL
    if _SH_CAL is not None:
        return _SH_CAL
    import json as _json
    days: set[str] = set()
    cache = ROOT / "logs" / "benchmarks_cache.json"
    if cache.exists():
        try:
            c = json.loads(cache.read_text(encoding="utf-8"))
            days.update(c.get("000001.SH", {}).keys())
            # 交易日历（含未来法定节假日精确口径）优先——上证日线只到昨日
            tc = c.get("TRADE_CAL") or []
            days.update(tc)
        except (OSError, ValueError):
            pass
    if not days:
        import pandas as pd
        idx = pd.read_parquet(ROOT / "data_lake" / "index_daily.parquet")
        days.update(str(d)[:10] for d in idx.index.get_level_values("date"))
    from ops.benchmarks import _norm
    # 去重必须在 _norm 之后：set 收的是原始值（'2026-08-20' 与 '2026-08-20T00:00:00'
    # 等同日异形在 set 层互不相等，norm 成同串后才可判重）——sorted 直接排产生
    # 2 倍重复日历（8420 vs 4372），_expire_date 的 bisect 索引密度翻倍 → 超期日
    # 偏早；trailing 回放的 holding_days 也会翻倍（09-03 实锤修复）
    _SH_CAL = sorted(set(_norm(d) for d in days))
    return _SH_CAL


def _expire_date(entry_date, max_holding: int) -> str | None:
    """进场后第 max_holding+1 个交易日（超期平仓触发日；策略口径 T-1 基准）。
    日历内=精确交易日；越过日历末端=按周末外推（周一~周五序列）。"""
    if not entry_date:
        return None
    cal = _sh_calendar()
    import bisect
    i = bisect.bisect_left(cal, str(entry_date))
    j = i + max_holding + 1
    if j < len(cal):
        return cal[j]
    from datetime import date as _date, timedelta as _td
    d = _date.fromisoformat(cal[-1]) if cal else _date.today()
    step = j - len(cal) + 1
    while step > 0:
        d += _td(days=1)
        if d.weekday() < 5:
            step -= 1
    return f"{d:%Y-%m-%d}"


def _trade_history(leg_dir_path: Path, today_rows: list | None = None) -> list:
    """跨日成交史：state 订单史（filled>0）→ execrpts 形状行。

    - 委托时间取 o['placed_at']（epoch 秒）→ ISO 本地；date 键=交易日
    - 价格优先 o['price']（限价帽；marketable limit 下成交价≤帽，state 未存
      均价时以帽价近似——展示口径，精确成交价以今日柜台行为准）
    - today_rows（今日柜台 execrpts）按 cl_ord_id 覆盖：价格/数量/时间以柜台
      真值优先（state 落盘可能滞后于柜台回报）
    """
    import json as _json
    from datetime import datetime, timezone, timedelta as _td
    from ops import gm_ops_common as gc

    CN = timezone(_td(hours=8))
    try:
        st = _json.loads(gc.state_pkl_path(leg_dir_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    out: dict[str, dict] = {}
    for oid, o in (st.get("orders") or {}).items():
        if not (o.get("filled") or 0) > 0:
            continue
        placed = o.get("placed_at")
        ts = (datetime.fromtimestamp(float(placed), CN).isoformat()
              if placed else f"{o.get('date', '')}T09:31:00+08:00")
        sym = o.get("symbol") or ""
        ex = "SHSE" if sym.endswith(".SH") else "SZSE"
        out[oid] = {
            "cl_ord_id": oid, "symbol": f"{ex}.{sym.split('.')[0]}",
            "side": 1,                              # state 订单史=开仓买方向（卖出走 EXPIRE/TICK 卖单也入 orders——side 按单型回填）
            "price": float(o.get("price") or 0),
            "volume": int(o.get("filled") or 0),
            "amount": round(float(o.get("price") or 0) * float(o.get("filled") or 0), 2),
            "created_at": ts,
            "date": o.get("date"),
            "purpose": o.get("purpose"),
        }
    # 卖出方向回填：purpose ∈ 卖出族（EXPIRE/TICK_EXIT/TP_*）→ side=2
    sell_purposes = {"EXPIRE", "TIME_EXIT", "TP", "STOP", "TRAIL", "FORCE", "EOD"}
    for oid, row in out.items():
        p = str(row.get("purpose") or "")
        if any(k in p for k in sell_purposes) or "SELL" in p:
            row["side"] = 2
    # 今日柜台实况覆盖（cl_ord_id 对齐；柜台价/量/时间是权威）
    for r in today_rows or []:
        cid = r.get("cl_ord_id")
        if cid in out:
            out[cid].update({k: r[k] for k in ("price", "volume", "amount",
                                               "created_at", "side") if r.get(k) is not None})
        elif r not in out.values():
            out[f"broker-{cid}"] = {**r, "date": str(r.get("created_at", ""))[:10]}
    return sorted(out.values(), key=lambda r: r.get("created_at") or "", reverse=True)


def _plan_card(w) -> int:
    """每日计划卡（09-02 用户需求）：今日实况（audit 驱动）+ 昨晚预演回看。

    前跑红线：明日预演（plan_date > today）绝不入公网快照——盘前公开
    marketable limit 的买入意向=送前跑；预演档案仅在执行日（plan_date ==
    today，即已 09:31 执行）公开，作为对拍记录。预演缺失（cron 未跑/未部署
    期）→ preview=null 优雅降级。
    """
    import csv as _csv
    from datetime import datetime as _dt
    from ops import gm_ops_common as gc
    from ops.emquant_eod_report import _name_map

    today = f"{_dt.now():%Y-%m-%d}"
    main_dir = gc.leg_strategy_dir(next(l for l in gc.active_legs() if l.key == "main"))

    facts = {"signals": [], "placed": {}, "filled": set(),
             "skip_held": 0, "blocked": 0}
    src = gc.audit_csv_path(today, main_dir)
    if src.exists():
        for row in _csv.reader(src.open(encoding="utf-8")):
            if len(row) < 2 or not row[0].startswith(today):
                continue
            ev = row[1]
            try:
                d = json.loads(row[2]) if len(row) > 2 and row[2] else {}
            except ValueError:
                d = {}
            if ev == "SIGNAL" and d.get("symbol"):
                facts["signals"].append(d)
            elif ev == "ORDER_PLACED" and d.get("symbol"):
                facts["placed"][str(d["symbol"])] = d
            elif ev in ("POS_ENRICHED", "FILL") and d.get("symbol"):
                facts["filled"].add(str(d["symbol"]))
            elif ev == "SIGNAL_SKIP_HELD":
                facts["skip_held"] += 1
            elif ev == "ORDER_BLOCKED":
                facts["blocked"] += 1

    names = _name_map([str(d.get("symbol")) for d in facts["signals"]])
    rows = []
    for d in facts["signals"]:
        sym = str(d.get("symbol", "?"))
        pl = facts["placed"].get(sym)
        rows.append({
            "sym": sym, "name": names.get(sym, "—"),
            "qty": int(pl.get("qty") or 0) if pl else None,
            "price": pl.get("price") if pl else None,
            "neckline": d.get("neckline"), "rr": d.get("rr"),
            "formed": str(d.get("formed_at") or ""),
            "status": "filled" if sym in facts["filled"]
            else "placed" if pl else "signal",
        })

    # 昨晚预演回看（已执行 → 公开 + 对拍）
    preview = None
    pp = ROOT / "logs" / f"plan_preview_{today}.json"
    if pp.exists():
        try:
            art = json.loads(pp.read_text(encoding="utf-8"))
            if art.get("plan_date") == today:
                plan = {str(r.get("sym")): r for r in (art.get("plan") or [])}
                actual = facts["placed"]
                both = set(plan) & set(actual)
                only_p = set(plan) - set(actual)
                only_a = set(actual) - set(plan)
                drift = [s for s in both
                         if int(actual[s].get("qty") or 0) != int(plan[s].get("qty") or 0)
                         or abs(float(actual[s].get("price") or 0)
                                - float(plan[s].get("entry") or 0)) > 0.011]
                total = len(set(plan) | set(actual))
                preview = {
                    "stamp": art.get("stamp"), "equity": art.get("equity"),
                    "recon": {"ok": len(both) - len(drift), "total": total,
                              "only_preview": sorted(only_p),
                              "only_actual": sorted(only_a),
                              "drift": sorted(drift)},
                }
        except (OSError, ValueError):
            pass

    # 明日预演（09-02 用户裁决：终态私域站+鉴权后置，预演完整上站）——取
    # plan_date 最大的预演档案；== today 的作为执行日回看（preview 块），
    # > today 的作为盘前参考整段公开。私域前提下的完整功能形态。
    next_preview = None
    pps = sorted(ROOT.glob("logs/plan_preview_*.json"))
    if pps:
        try:
            art = json.loads(pps[-1].read_text(encoding="utf-8"))
            if str(art.get("plan_date", "")) > today:
                from ops.emquant_eod_report import _name_map as _nm
                pn = _nm([str(r.get("sym")) for r in (art.get("plan") or [])]
                         + list(art.get("dropped_amihud") or [])
                         + [b.get("sym") for b in (art.get("blocked") or [])])
                next_preview = {
                    "plan_date": art.get("plan_date"),
                    "stamp": art.get("stamp"), "equity": art.get("equity"),
                    "rows": [{**r, "name": pn.get(str(r.get("sym")), "—")}
                             for r in (art.get("plan") or [])],
                    "dropped_amihud": [str(x) for x in (art.get("dropped_amihud") or [])],
                    "skip_held": art.get("skip_held") or [],
                    "blocked": art.get("blocked") or [],
                }
        except (OSError, ValueError):
            pass

    w("plan_card", {
        "today": today,
        "rows": rows,
        "summary": {"signals": len(facts["signals"]),
                    "filled": len(facts["filled"]),
                    "skip_held": facts["skip_held"],
                    "blocked": facts["blocked"]},
        "preview": preview,
        "next_preview": next_preview,
        "note": "预演≠计划：识别层与实跑同源逐字段一致，差异来自运行时闸态；"
                "权威以 09:31 实挂为准（站点终态私域，鉴权建设中）",
    })
    return 0


def _trailing_path(leg_dir: Path, pos: dict) -> list:
    """trailing 止损轨迹逐日回放（P5.3）：部署产物 compute_stop_price 真身口径。

    holding_days = (entry, t] 区间交易日数（与策略 trading_days_between 同式），
    对 entry 起每个交易日回放止损价；末点=当前止损位（与持仓 stop 列一致——
    300433 实证 33.9434 吻合）。neckline/atr 缺 → []（画线缺就不画，不猜）。
    """
    import importlib.util as ilu
    import bisect as _bisect

    tr = pos.get("trailing") or {}
    entry = str(pos.get("entry_date") or "")
    if tr.get("neckline") is None or tr.get("atr") is None or not entry:
        return []
    cal = _sh_calendar()
    import bisect as _bisect
    try:
        spec = ilu.spec_from_file_location(
            f"pilot_trail_{leg_dir.name[:8]}", leg_dir / "main.py")
        m = ilu.module_from_spec(spec)
        sys.modules[spec.name] = m
        spec.loader.exec_module(m)
        stop_fn = m.compute_stop_price

        # 锚点=首个 ≥ entry 的日历日（bisect_left 本身）——不用「前一交易日」
        # 回退（code-review J-3：周末/停牌 entry 回退会在入场前多画一个
        # base_stop 点，与策略 trading_days_between 的 bisect_right 口径分叉）
        i0 = _bisect.bisect_left(cal, entry)
        if i0 >= len(cal):
            return []                     # entry 越过日历末端（未来日）=无轨迹
        today = f"{datetime.now():%Y-%m-%d}"
        path = []
        for j in range(i0, len(cal)):
            t = cal[j]
            if t > today:
                break
            holding_days = j - i0        # (entry, t] 交易日数（entry 日=0）
            stop = stop_fn(
                neckline=float(tr["neckline"]), atr=float(tr["atr"]),
                holding_days=holding_days,
                stop_atr_mult=float(tr.get("stop_atr_mult", 1.0)),
                grace=int(tr.get("grace") or 0),
                step=float(tr.get("step") or 0.0),
                floor=tr.get("floor"))
            path.append([t, round(float(stop), 3)])
        return path
    except Exception as e:
        # 整段降级（code-review J-1：此前 try 只包 importlib 段——部署产物内
        # 运行时异常会穿透到 build_snapshot 中段带走后段全部快照；「加载失败
        # 降级」的承诺必须覆盖调用段）
        print(f"  ⚠ trailing 回放降级空表（{type(e).__name__}: {e}）")
        return []


def _ohlcv_files(w) -> int:
    """持仓+当日信号标的 → ohlcv_<sym>.json（160 根日 K + 颈线/止损/止盈画线）。

    marks 三源合璧：state.pkl 持仓（entry/stop/tp 定身位）+ audit SIGNAL（颈线/RR/
    formed_at）+ 湖日线（OHLCV，前复权）。缺任一源优雅降级（画线缺就不画）。
    """
    import pandas as pd
    from ops import gm_ops_common as gc
    from ops.emquant_eod_report import _name_map

    syms: dict[str, dict] = {}          # {sym: marks}
    for leg in gc.active_legs():
        leg_dir = gc.leg_strategy_dir(leg)
        try:
            st = json.loads(gc.state_pkl_path(leg_dir).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            st = {}
        for sym, pos in (st.get("positions") or {}).items():
            if int((pos or {}).get("remaining_qty") or 0) > 0:
                syms.setdefault(sym, {}).update({
                    "entry_date": pos.get("entry_date"),
                    "entry_price": pos.get("entry_price"),
                    "stop": pos.get("stop"),
                    "tp1_price": pos.get("tp1_price"),
                    "tp2_price": pos.get("tp2_price"),
                    "formed_at": pos.get("formed_at") or pos.get("entry_date"),
                    # trailing 止损轨迹（P5.3）：部署产物 compute_stop_price
                    # 逐日回放 [[date, stop], ...]——KlinePanel 画绿色虚线阶梯
                    "trailing_path": _trailing_path(leg_dir, pos),
                    # 颈线（09-01 用户问"有颈线么"）：trailing 六件套的 neckline 即
                    # 信号颈线（enrich 挂载的追踪锚=识别颈线同值，08-27 实证
                    # 300433 trailing.neckline 39.0 == SIGNAL 颈线）
                    "neckline": (pos.get("trailing") or {}).get("neckline"),
                    # 09-02 用户需求：下单日期+超时卖出日期。超期判定=策略
                    # trading_days_between(entry, T-1) > max_holding → 触发日=
                    # entry 后第 max_holding+1 个交易日（上证日历推算，日历外
                    # 按周末外推）。
                    "max_holding": int((pos.get("exec_params") or {})
                                       .get("max_holding") or 30),
                    "expire_date": _expire_date(
                        pos.get("entry_date"),
                        int((pos.get("exec_params") or {}).get("max_holding") or 30)),
                })
        # SIGNAL 回扫（当日新信号 + 持仓的历史信号——按日回扫至多 45 天）：
        # 理论委托 entry=颈线+2.5×ATR（超涨停带会被钳，成交远低于它）；state 的
        # entry_price 是成交价（marketable limit 贴盘口≈颈线，09-02 用户问
        # "颈线和 entry 为什么基本一样"的根源——两线语义必须分开画）。
        import csv as _csv
        from datetime import timedelta as _td
        held_syms = {sym for sym, mk in syms.items() if mk.get("entry_date")}
        for back in range(45):
            day = f"{datetime.now() - _td(days=back):%Y-%m-%d}"
            src = gc.audit_csv_path(day, leg_dir)
            if not src.exists():
                continue
            for row in _csv.reader(src.open(encoding="utf-8")):
                if len(row) < 3 or row[1] != "SIGNAL":
                    continue
                try:
                    d = json.loads(row[2])
                except ValueError:
                    continue
                sym = d.get("symbol")
                # 当日信号（任意）或仍持有标的的历史信号（回扫只为补精确值）
                if not sym or (back > 0 and sym not in held_syms):
                    continue
                if back > 0 and syms.get(sym, {}).get("signal_entry") is not None:
                    continue                       # 已有（最新日的）不覆盖
                m = syms.setdefault(sym, {})
                m.setdefault("neckline", d.get("neckline"))
                m.setdefault("signal_entry", d.get("entry_price"))
                m.setdefault("rr", d.get("rr"))
                m.setdefault("formed_at", d.get("formed_at"))

    if not syms:
        return 0
    lake = pd.read_parquet(ROOT / "data_lake" / "a_shares_daily.parquet")
    cutoff = pd.Timestamp(datetime.now()) - pd.Timedelta(days=420)
    names = _name_map(list(syms))
    n = 0
    for sym, marks in sorted(syms.items()):
        try:
            g = lake.xs(sym, level="symbol").sort_index()
            g = g.loc[cutoff:, ["open", "high", "low", "close", "volume"]].tail(160)
            rows = [[round(float(o), 3), round(float(h), 3), round(float(l), 3),
                     round(float(c), 3), int(v)]
                    for o, h, l, c, v in zip(g["open"], g["high"], g["low"],
                                             g["close"], g["volume"])]
            w(f"ohlcv_{sym}", {"symbol": sym, "name": names.get(sym, "—"),
                               "dates": [f"{d:%Y-%m-%d}" for d in g.index],
                               "rows": rows, "marks": marks,
                               "asof": f"{datetime.now():%Y-%m-%d %H:%M}"})
            n += 1
        except KeyError:
            print(f"  ⚠ ohlcv 缺湖数据跳过：{sym}")
    return n


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="公网只读快照生成（yzzhan.xin 数据源）")
    p.add_argument("--days-ab", type=int, default=10, help="ab 对照快照天数（AbHistoryCard 10 日）")
    p.add_argument("--days-audit", type=int, default=2, help="audit 下钻快照天数")
    args = p.parse_args(argv)
    print(f"=== public snapshot → {OUT_DIR} ===")
    summary = build_snapshot(days_ab=args.days_ab, days_audit=args.days_audit)
    print(f"[done] {summary['files']} 个文件 @ {summary['generated_at']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
