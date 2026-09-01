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
  - 绝不入快照：掘金 token、次日计划预演（前跑风险红线）、内网路径、研究面
    数据（discovery 试验=研究 IP——静态站点路由已收敛 cockpit/experiments/data）

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
        w(f"gm_asset_{leg.key}", (payload or {}).get("data") or {} if st == 200 else {})
        st, payload = gc.api_get(f"/v3/account-trade/positions/{la}", lt, timeout=4.0)
        w(f"gm_positions_{leg.key}", (payload or {}).get("data") or [] if st == 200 else [])
        st, payload = gc.api_get(f"/v3/account-trade/orders/{la}", lt, timeout=4.0)
        w(f"gm_orders_{leg.key}", (payload or {}).get("data") or [] if st == 200 else [])
        st, payload = gc.api_get(f"/v3/account-trade/execrpts/{la}", lt, timeout=4.0)
        w(f"gm_trades_{leg.key}", (payload or {}).get("data") or [] if st == 200 else [])

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

    # ── OHLCV 快照（P2 静态半场：持仓+当日信号标的的 K 线回放数据）──
    _ohlcv_files(w)                     # w 闭包自增 n_files
    return {"files": n_files, "generated_at": f"{t0:%Y-%m-%d %H:%M:%S}"}


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
                })
        # 当日 SIGNAL（未成仓的新信号也给 K 线：颈线/entry/RR）
        src = gc.audit_csv_path(f"{datetime.now():%Y-%m-%d}", leg_dir)
        if src.exists():
            for row in __import__("csv").reader(src.open(encoding="utf-8")):
                if len(row) >= 3 and row[1] == "SIGNAL" and row[0].startswith(
                        f"{datetime.now():%Y-%m-%d}"):
                    try:
                        d = json.loads(row[2])
                    except ValueError:
                        continue
                    if d.get("symbol"):
                        m = syms.setdefault(d["symbol"], {})
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
