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
        leg, day = m.group(1), m.group(2)
        # 旧单腿命名（无前缀）是 main 腿前身：ERA 前只入 pre_era 存证桶；
        # ERA 后若再出现（迟到补报）归 main——绝不落 "pre_era" 腿键产幽灵腿
        #（code-review J-17：幽灵腿会污染前端「双腿 assets 齐」过滤致整日被丢）
        if not leg:
            leg = "pre_era" if day < ERA_START else "main"
        text = f.read_text(encoding="utf-8", errors="replace")
        hit = _NAV_RE.search(text)
        if not hit:
            continue
        nav = _num(hit.group(1))
        if leg == "pre_era":
            pre_era[day] = nav
            continue
        avail_m = _AVAIL_RE.search(text)
        mv_m = _MV_RE.search(text)
        days.setdefault(day, {})[leg] = {
            "nav": nav,
            "available": _num(avail_m.group(1)) if avail_m else None,
            "market_value": _num(mv_m.group(1)) if mv_m else None,
        }
    pre_era = {d: v for d, v in pre_era.items() if v is not None}
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
    today = f"{datetime.now():%Y-%m-%d}"
    for day, legs in parsed.items():
        for leg, vals in legs.items():
            cur = merged.setdefault(day, {}).setdefault(leg, {})
            # 历史日：实时点/已有值不覆盖（幂等保护）；today：EOD 终值覆盖盘中
            # 实时点（code-review J-5「盘中发布中毒」——中午手动 publish 落盘中
            # nav 后，若晚间 7002 恰断，不覆盖=当日点永久钉死在午间值无自愈）
            if cur.get("nav") is None or day == today:
                cur["nav"] = vals["nav"]
            for k in ("available", "market_value"):
                if vals[k] is not None and (cur.get(k) is None or day == today):
                    cur[k] = vals[k]

    # 实时点（当日）：7002 cash nav/available/market_value，双腿。
    # 双守卫（09-04「昨日收益=0」bug 实锤修复）：
    #   ① 仅交易时段（09:10-15:40）写——凌晨/深夜跑 update 时柜台返回的是
    #      上一收盘价，写进 today 键=前日值复制（09-04 行被 00:59 发布污染成
    #      09-03 值，真实 main -2.82% 被藏掉）；
    #   ② 不覆盖 parsed 已回填的当日 EOD 值——收盘后 EOD 日志（15:45）是
    #      终态权威，实时点（如 16:00 手动跑）不得回写盘中/等值旧价。
    now_hm = datetime.now().hour * 60 + datetime.now().minute
    in_session = 9 * 60 + 10 <= now_hm <= 15 * 60 + 40
    eod_today_legs = {leg for day, legs in parsed.items()
                      if day == today for leg in legs}
    if today >= ERA_START and in_session:
        try:
            token = str(gc.runtime_config().get("token") or "")
            for leg in gc.active_legs():
                cfg = gc.runtime_config(gc.leg_strategy_dir(leg))
                st, payload = gc.api_get(
                    f"/v3/account-trade/cash/{cfg.get('account_id')}",
                    str(cfg.get("token") or token), timeout=4.0)
                row = ((payload or {}).get("data") or [None])[0]
                if (st == 200 and row and row.get("nav") is not None
                        and leg.key not in eod_today_legs):
                    cur = merged.setdefault(today, {}).setdefault(leg.key, {})
                    cur["nav"] = float(row["nav"])
                    if row.get("available") is not None:
                        cur["available"] = float(row["available"])
                    if row.get("market_value") is not None:
                        cur["market_value"] = float(row["market_value"])
        except Exception:
            pass                                  # 实时点失败不阻断（回填值兜底）

    # 输出形状：legs（净值族既有消费者零改动）+ assets（P5.2 堆叠图，可选）。
    # assets 逐键判 None 后再 round（code-review HV-1：「任一非 None 即入桶」的
    # 过滤配上无条件 round=旧格式单段日志（有可用无市值）直接 TypeError，且
    # 炸点在 build_snapshot 中段——后段全部快照不再写出）
    days = []
    for d in sorted(merged):
        if d < ERA_START:
            continue
        legs = {k: round(v["nav"], 2) for k, v in sorted(merged[d].items())
                if v.get("nav") is not None}
        assets = {}
        for k, v in sorted(merged[d].items()):
            row = {kk: round(vv, 2) for kk, vv in
                   (("available", v.get("available")),
                    ("market_value", v.get("market_value")))
                   if vv is not None}
            if row:
                assets[k] = row
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
