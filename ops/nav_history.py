# -*- coding: utf-8 -*-
"""净值历史管道（yzzhan.xin 首页净值曲线族的数据源，2026-09-01 可视化重构 P1；
09-03 P5.2 扩展：days[].assets 资产构成（available/market_value 历史落盘））。

三源合一（后写覆盖前读，同日幂等）：
  ① 存量合并：public/data/nav_history.json 已有 days（历史发布日累积，日志清理后仍存活）
  ② 回填解析：logs/emquant_eod_{main,exp}_YYYY-MM-DD.txt「③ 资金面：nav **N,NNN**
     ｜可用 N｜市值 N」+ 旧单腿命名 logs/emquant_eod_YYYY-MM-DD.txt（main 腿前身）
  ③ 实时点：7002 cash nav/available/market_value（快照时点，收盘后≈当日终值）

时代口径红线（重构方案 §族1）：序列自 2026-08-31（两腿各入金 20 万）起算；
08-28 及以前的 10 万单腿试点时代**不混画**（不同资金基数，混入=伪造翻倍日）——
旧单腿日志解析结果仅用于 era_note 存证，不入 days。

资产构成（P5.2）：days[].assets = {leg: {available, market_value}}（可选键，旧文件
无此键=该日缺数据，前端堆叠图断点不画——向后兼容）。闭合口径：nav =
available + market_value（7002 实测 198,014 = 67,899 + 130,115 逐分吻合）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT = ROOT / "presentation" / "web" / "public" / "data" / "nav_history.json"
LOGS = ROOT / "logs"
ERA_START = "2026-08-31"
BASE = 200_000.0

# 「③ 资金面：nav **201,843**｜可用 67,899｜市值 130,115」与旧「③ 资金：nav 99,752」
# 两代格式（可用/市值 段 P5.2 新解析；旧格式缺段 → assets 该腿该日缺省不画）
_NAV_RE = re.compile(r"nav\s*\*{0,2}([\d,]+(?:\.\d+)?)\*{0,2}")
_AVAIL_RE = re.compile(r"可用\s*([\d,]+(?:\.\d+)?)")
_MV_RE = re.compile(r"市值\s*([\d,]+(?:\.\d+)?)")


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def parse_eod_logs() -> tuple[dict[str, dict[str, dict]], dict[str, float]]:
    """EOD 日志回填 → (days, pre_era)。

    days={date: {leg: {"nav": x, "available": y?, "market_value": z?}}}（ERA 起，
    available/market_value 缺段的日子为 None——不猜数据）；pre_era={date:nav}
    （10 万单腿试点，仅存证不混画）。
    """
    days: dict[str, dict[str, dict]] = {}
    pre_era: dict[str, float] = {}
    for f in sorted(LOGS.glob("emquant_eod_*.txt")):
        m = re.match(r"emquant_eod_(?:(main|exp)_)?(\d{4}-\d{2}-\d{2})\.txt$", f.name)
        if not m:
            continue
        leg, day = m.group(1) or "pre_era", m.group(2)
        text = f.read_text(encoding="utf-8", errors="replace")
        hit = _NAV_RE.search(text)
        if not hit:
            continue
        nav = _num(hit.group(1))
        if day < ERA_START:
            pre_era[day] = nav
            continue
        avail_m = _AVAIL_RE.search(text)
        mv_m = _MV_RE.search(text)
        days.setdefault(day, {})[leg] = {
            "nav": nav,
            "available": _num(avail_m.group(1)) if avail_m else None,
            "market_value": _num(mv_m.group(1)) if mv_m else None,
        }
    return days, pre_era


def update() -> dict:
    """合并三源写出 nav_history.json。返回写出的文档（快照摘要/测试消费）。"""
    import json
    from datetime import datetime

    from ops import gm_ops_common as gc

    doc: dict = {"base": BASE, "era_start": ERA_START,
                 "note": "净值自两腿各入金 20 万起算（08-31）；更早的 10 万单腿试点"
                         "时代不混画（资金基数不同）", "days": []}
    if OUT.exists():
        try:
            old = json.loads(OUT.read_text(encoding="utf-8"))
            doc["days"] = old.get("days") or []
            doc["pre_era_note"] = old.get("pre_era_note")
        except (OSError, ValueError):
            pass
    # 旧文件 days 兼容并集：{date: {leg: {nav…}}}（旧形状 legs:{leg:nav} 提升内层）
    merged: dict[str, dict[str, dict]] = {}
    for d in doc["days"]:
        for leg, nav in (d.get("legs") or {}).items():
            merged.setdefault(d["date"], {})[leg] = {"nav": float(nav)}
        for leg, a in (d.get("assets") or {}).items():
            merged.setdefault(d["date"], {}).setdefault(leg, {}).update(a)

    parsed, pre_era = parse_eod_logs()
    for day, legs in parsed.items():
        for leg, vals in legs.items():
            # 回填值不覆盖已有 nav（实时点更准）；assets 缺段补回填
            cur = merged.setdefault(day, {}).setdefault(leg, {})
            if cur.get("nav") is None:
                cur["nav"] = vals["nav"]
            for k in ("available", "market_value"):
                if cur.get(k) is None and vals[k] is not None:
                    cur[k] = vals[k]

    # 实时点（当日）：7002 cash nav/available/market_value，双腿
    today = f"{datetime.now():%Y-%m-%d}"
    if today >= ERA_START:
        try:
            token = str(gc.runtime_config().get("token") or "")
            for leg in gc.active_legs():
                cfg = gc.runtime_config(gc.leg_strategy_dir(leg))
                st, payload = gc.api_get(
                    f"/v3/account-trade/cash/{cfg.get('account_id')}",
                    str(cfg.get("token") or token), timeout=4.0)
                row = ((payload or {}).get("data") or [None])[0]
                if st == 200 and row and row.get("nav") is not None:
                    cur = merged.setdefault(today, {}).setdefault(leg.key, {})
                    cur["nav"] = float(row["nav"])
                    if row.get("available") is not None:
                        cur["available"] = float(row["available"])
                    if row.get("market_value") is not None:
                        cur["market_value"] = float(row["market_value"])
        except Exception:
            pass                                  # 实时点失败不阻断（回填值兜底）

    # 输出形状：legs（净值族既有消费者零改动）+ assets（P5.2 堆叠图，可选）
    days = []
    for d in sorted(merged):
        if d < ERA_START:
            continue
        legs = {k: round(v["nav"], 2) for k, v in sorted(merged[d].items())
                if v.get("nav") is not None}
        assets = {k: {"available": round(v["available"], 2),
                      "market_value": round(v["market_value"], 2)}
                  for k, v in sorted(merged[d].items())
                  if v.get("available") is not None or v.get("market_value") is not None}
        day_doc: dict = {"date": d, "legs": legs}
        if assets:
            day_doc["assets"] = assets
        days.append(day_doc)
    doc["days"] = days
    if pre_era:
        doc["pre_era_note"] = (f"史前期（10 万单腿试点，不入曲线）："
                               + "，".join(f"{d} nav {v:,.0f}"
                                           for d, v in sorted(pre_era.items())))
    doc["assets_note"] = ("资产构成=P5.2 起 EOD 日志+实时点双源落盘；"
                          "cash+市值=nav 逐日闭合（available+market_value）")
    doc["updated_at"] = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return doc


if __name__ == "__main__":
    d = update()
    print(f"[nav_history] {len(d['days'])} 天 @ {d['updated_at']}")
    for day in d["days"]:
        print(" ", day)
    if d.get("pre_era_note"):
        print(" ", d["pre_era_note"])
