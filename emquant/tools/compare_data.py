# -*- coding: utf-8 -*-
"""对拍比较器：data_lake（tushare 源）vs gm 腿定点复权日线（东财源）。

Why 存在：W0' 对拍的「比」这一腿。gm_data_pull.py（.venv_emquant）落盘的
parity_gm_*.csv 与 data_lake/a_shares_daily.parquet（.venv310 才有 pyarrow 可读）
逐标的逐列比数值，回答一个问题：**换数据源会不会改变颈线信号**。两源任何一处
分叉（复权因子精度、除权事件时点、价格修复）都会以 |Δ|/close 形式现形；一致
率是 W2 双轨对照的分母可信度依据，不一致标的进排除池豁免分母（设计 §7）。

预期管理（写进设计的前提，不因结果难看而放宽容差）：gm=东财源与 tushare 的
前复权因子由各自服务端独立维护，「大面积不一致」是**有效结论**而非工具故障——
报告如实记录一致率/最大相对偏差/典型样例三个数即可，绝不为好看放宽 1e-6。

运行环境（对拍另一腿 gm_data_pull 在 .venv_emquant，本脚本禁跨环境混跑）：
    PYTHONUTF8=1 E:/quanter/.venv310/Scripts/python.exe emquant/tools/compare_data.py <子命令>

子命令（晨间补跑顺序即用法顺序）：
    anchors   生成锚表：每标的取 data_lake 内末交易日为定点复权锚 →
              emquant/state/parity_anchors_YYYYMMDD.json。
              Why 逐标的锚而非全局一日：长停牌/退市边缘标的 lake 末日参差，锚
              到无数据的日子 gm 腿拉不到对齐窗口；锚各自末日则两腿窗口重心一致。
    selftest  比对器自检（不依赖 gm/token）：从 lake 抽样伪造「完全相等副本」
              （期望一致率 100%）+ 注入 1e-3 级扰动的对照标的（期望被判不一致）
              ——双向验证比对器无假阴/假阳，夜间降级时的工具可信度凭证。
    compare   正式对拍：lake vs parity_gm_* → 一致率三数 + 排除池
              emquant/state/parity_exclude_YYYYMMDD.json + markdown 摘要
              （--md-out 可落盘成段，供贴入对拍报告）。
"""

import argparse
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

_TOOLS_DIR = Path(__file__).resolve().parent
_EMQUANT_DIR = _TOOLS_DIR.parent
STATE_DIR = _EMQUANT_DIR / "state"
CONFIG_DIR = _EMQUANT_DIR / "config"
LAKE_PARQUET = _EMQUANT_DIR.parent / "data_lake" / "a_shares_daily.parquet"

# 对拍容差（设计钉死，禁止放宽）：|gm - lake| / |lake| 逐列逐日 ≤ 1e-6。
# Why 以 lake 为分母基准：本地腿（tushare qfq）是生产口径的延续，「gm 相对生产
# 偏了多少」才是问题本身；分母换 gm 或 max() 只改变第六位小数的边界形态，不动
# 结论量级——钉死 lake 分母并写进口径注记，避免两次对拍因分母选择不同而不可比。
_REL_TOL = 1e-6

# 交叠窗下限：两腿交集交易日 < 60 视为「窗口不足以对拍」（新上市/长期停牌边缘），
# 进排除池但单列原因——它们不是「数值不一致」，混入会污染「源分叉」的判读
_MIN_OVERLAP = 60

_OHLC = ["open", "high", "low", "close"]


def _die(code: int, msg: str) -> None:
    print(f"[compare_data] 终止：{msg}", flush=True)
    sys.exit(code)


def _universe() -> list:
    return json.loads((CONFIG_DIR / "universe.json").read_text(encoding="utf-8"))["symbols"]


def _load_lake(symbols: list) -> pd.DataFrame:
    """读 lake 中 universe 子集（pyarrow filters 下推，全湖千万行不进内存）。"""
    if not LAKE_PARQUET.exists():
        _die(10, f"data_lake 不存在：{LAKE_PARQUET}")
    df = pd.read_parquet(LAKE_PARQUET, filters=[("symbol", "in", symbols)])
    if not isinstance(df.index, pd.MultiIndex) or set(df.index.names) != {"date", "symbol"}:
        # 读回形状防御：index 丢失即两腿 join 口径崩坏，fail-loud 好过错位对拍
        _die(11, f"lake 读回非预期 MultiIndex(date,symbol)：实得 {type(df.index).__name__} {df.index.names}")
    return df[["open", "high", "low", "close", "volume"]].sort_index()


def _load_gm_leg(explicit: str):
    """读 gm 腿产物（csv/parquet 均可——pull 侧按引擎可用性落盘）→ (df, 文件名)。"""
    if explicit:
        p = Path(explicit)
        if not p.exists():
            _die(12, f"gm 腿产物不存在：{p}")
    else:
        cands = sorted(STATE_DIR.glob("parity_gm_*.csv")) + sorted(STATE_DIR.glob("parity_gm_*.parquet"))
        if not cands:
            _die(12, "缺 gm 腿产物（emquant/state/parity_gm_*.csv|parquet）——先在 .venv_emquant 跑 "
                     "emquant/tools/gm_data_pull.py")
        p = cands[-1]  # 文件名含 YYYYMMDD，字典序即时间序（同日多格式时取字典序后者，人工覆盖可控）
    reader = pd.read_parquet if p.suffix == ".parquet" else pd.read_csv
    df = reader(p)
    if df.index.names != ["date", "symbol"]:
        df = df.set_index(["date", "symbol"])
    return df[_OHLC + ["volume"]].sort_index(), p.name


# ── anchors 子命令 ────────────────────────────────────────────────────────────

def cmd_anchors(args) -> None:
    """每标的 lake 末日 → 锚表（gm 腿定点复权的 adjust_end_time 逐标的取值）。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    syms = _universe()
    lake = _load_lake(syms)
    # groupby level 取 max：末日是「该标的 lake 内最后一个交易日」，不是全局日历末日
    anchors = lake.groupby(level="symbol").apply(
        lambda g: g.index.get_level_values("date").max().strftime("%Y-%m-%d"))
    anchors = anchors.to_dict()
    missing = [s for s in syms if s not in anchors]
    out = STATE_DIR / f"parity_anchors_{datetime.now().strftime('%Y%m%d')}.json"
    out.write_text(json.dumps(anchors, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[compare_data] 锚表完成：{len(anchors)} 标的 → {out}", flush=True)
    if missing:
        print(f"[compare_data] 注意：universe 中 {len(missing)} 只在 lake 无数据（gm 腿将记入失败清单）："
              f"{missing[:10]}{'...' if len(missing) > 10 else ''}", flush=True)
    print("[compare_data] 下一步（.venv_emquant）：PYTHONUTF8=1 E:/quanter/.venv_emquant/Scripts/python.exe "
          "emquant/tools/gm_data_pull.py", flush=True)


# ── 比对核心（selftest 与 compare 共用）───────────────────────────────────────

def compare_core(lake: pd.DataFrame, gm: pd.DataFrame, tol: float = _REL_TOL) -> dict:
    """逐标的 inner join 后四列相对偏差 → 一致率三数 + 排除池明细。

    判定口径（对拍报告的「口径注记」，改动须同步报告模板）：
        - 分母 = lake 值（生产基准腿）；
        - 标的级一致 ⇔ 交叠窗内【所有日 × 四列】rel ≤ tol（一日一列超即整标的
          不一致——W2 分母豁免按标的粒度，不按日粒度）；
        - 交叠日 < _MIN_OVERLAP 单列 insufficient_overlap（不是源分叉证据）；
        - 原因猜测：四列 gm/lake 比率的中位数偏离 1 超过 10×tol 且比率近似常数
          （std/median < 1e-3）→ 「系统性比例偏移，疑似复权因子时点/精度差」；
          否则 → 「散布型差异，疑似单日价格修复/数据源修复不同步」。
    """
    lake_syms = set(lake.index.get_level_values("symbol"))
    gm_syms = set(gm.index.get_level_values("symbol"))
    participants = sorted(lake_syms & gm_syms)

    n_pass, excludes, worst = 0, [], {"symbol": None, "rel": 0.0, "col": "", "date": ""}
    for sym in participants:
        a = lake.xs(sym, level="symbol")[_OHLC]
        b = gm.xs(sym, level="symbol")[_OHLC]
        joined = a.join(b, how="inner", lsuffix="_lake", rsuffix="_gm")
        if len(joined) < _MIN_OVERLAP:
            excludes.append({"symbol": sym, "reason": "insufficient_overlap",
                             "overlap_days": int(len(joined)),
                             "guess": "窗口不足（新上市/长期停牌边缘）——非源分叉证据"})
            continue
        rel = (np.abs(joined[[f"{c}_gm" for c in _OHLC]].to_numpy()
                      - joined[[f"{c}_lake" for c in _OHLC]].to_numpy())
               / np.abs(joined[[f"{c}_lake" for c in _OHLC]].to_numpy()))
        max_rel = float(np.nanmax(rel))
        if max_rel > worst["rel"]:
            i, j = divmod(int(rel.argmax()), rel.shape[1])
            worst = {"symbol": sym, "rel": max_rel, "col": _OHLC[j],
                     "date": str(joined.index[i])[:10]}
        if max_rel <= tol:
            n_pass += 1
            continue
        # 原因猜测：比率形态分型（系统性比例 vs 散布修复）
        ratio = joined[[f"{c}_gm" for c in _OHLC]].to_numpy() / joined[[f"{c}_lake" for c in _OHLC]].to_numpy()
        r_med, r_std = float(np.nanmedian(ratio)), float(np.nanstd(ratio))
        systematic = abs(r_med - 1.0) > 10 * tol and (r_std / abs(r_med)) < 1e-3
        excludes.append({
            "symbol": sym,
            "reason": "mismatch",
            "max_rel": round(max_rel, 9),
            "overlap_days": int(len(joined)),
            "ratio_median": round(r_med, 9),
            "guess": ("系统性比例偏移（四列比率≈常数 %.6f），疑似复权因子时点/精度差"
                      % r_med) if systematic else
                     ("散布型差异（比率波动 std/med=%.2e），疑似单日价格修复/源修复不同步"
                      % (r_std / abs(r_med))),
        })

    n_mismatch = sum(1 for e in excludes if e["reason"] == "mismatch")
    n_par = len(participants)
    denom = n_par - sum(1 for e in excludes if e["reason"] == "insufficient_overlap")
    return {
        "participants": n_par,
        "pass": n_pass,
        "mismatch": n_mismatch,
        "insufficient_overlap": n_par - n_pass - n_mismatch,
        # 一致率分母 = 参与且窗口充足（排除池整体豁免 W2 分母，口径与设计 §7 对齐）
        "consistency_rate": round(n_pass / denom, 6) if denom else float("nan"),
        "max_rel": round(worst["rel"], 9) if worst["symbol"] else 0.0,
        "worst": worst,
        "excludes": excludes,
        "lake_only": sorted(lake_syms - gm_syms),
        "gm_only": sorted(gm_syms - lake_syms),
    }


def _markdown_summary(res: dict, gm_leg_name: str) -> str:
    """结果 markdown 段（stdout 打印 + --md-out 落盘，供贴对拍报告）。"""
    ex_mm = [e for e in res["excludes"] if e["reason"] == "mismatch"]
    lines = [
        "## 实测对拍结果（%s）" % datetime.now().strftime("%Y-%m-%d %H:%M"),
        "",
        "- gm 腿产物：`%s`" % gm_leg_name,
        "- 参与标的 %d（lake∩gm）；一致 %d，不一致 %d，窗口不足 %d；lake 独有 %d，gm 独有 %d"
        % (res["participants"], res["pass"], res["mismatch"], res["insufficient_overlap"],
           len(res["lake_only"]), len(res["gm_only"])),
        "- **一致率 %.4f%%**（分母=窗口充足标的；排除池豁免 W2 分母，设计 §7）"
        % (res["consistency_rate"] * 100),
        "- **最大相对偏差 %.3e**（%s @ %s %s，容差 1e-6）"
        % (res["max_rel"], res["worst"]["symbol"], res["worst"]["date"], res["worst"]["col"]),
        "",
        "### 不一致典型样例（前 8）",
        "",
        "| symbol | max_rel | 比率中位 | 原因猜测 |",
        "|---|---|---|---|",
    ]
    for e in ex_mm[:8]:
        lines.append("| %s | %.3e | %s | %s |" % (e["symbol"], e["max_rel"],
                                                  e.get("ratio_median", "-"), e["guess"]))
    if not ex_mm:
        lines.append("（无——全部参与标的四列均过 1e-6）")
    return "\n".join(lines)


# ── selftest 子命令 ───────────────────────────────────────────────────────────

def cmd_selftest(args) -> None:
    """比对器双向自检：相等副本全过 + 扰动标的必现——无 gm/token 依赖的可信度凭证。"""
    syms = _universe()
    lake = _load_lake(syms)
    # 抽 5 只有充分历史的标的：尾部 180 根（模拟 gm 腿产物形状，锚定语义隐含于
    # 「尾部」——与 pull 的 tail(n_roots) 同构）
    picked = sorted(symbols_with_depth(lake, syms, 180))[:5]
    if len(picked) < 5:
        _die(20, f"lake 可抽样标的不足 5 只（{len(picked)}）——selftest 无法进行")
    frames = []
    for sym in picked[:-1]:  # 前 4 只：逐位相等副本（期望判一致）
        frames.append(_tail_frame(lake, sym, 180))
    # 末 1 只：close 注入 1e-3 级比例扰动（期望判不一致且 guess 指向系统性偏移）
    tampered = _tail_frame(lake, picked[-1], 180)
    tampered["close"] = tampered["close"] * 1.0005
    frames.append(tampered)
    fake_gm = pd.concat(frames).sort_index()

    res = compare_core(lake, fake_gm)
    expect_pass = set(picked[:-1])
    got_pass_ok = expect_pass.issubset(
        {s for s in picked if s not in {e["symbol"] for e in res["excludes"]}})
    tampered_caught = picked[-1] in {e["symbol"] for e in res["excludes"]}
    print(f"[compare_data][selftest] 相等副本 {len(picked) - 1} 只全判一致：{'PASS' if got_pass_ok else 'FAIL'}", flush=True)
    print(f"[compare_data][selftest] 扰动标的 {picked[-1]}（close×1.0005）被判不一致："
          f"{'PASS' if tampered_caught else 'FAIL'}", flush=True)
    print(f"[compare_data][selftest] max_rel={res['max_rel']:.3e}（应 ~5e-4 量级）", flush=True)
    if not (got_pass_ok and tampered_caught):
        _die(21, "selftest 未过——比对器存在假阴/假阳，修好再对拍")


def symbols_with_depth(lake: pd.DataFrame, syms: list, depth: int) -> list:
    """lake 内历史 ≥ depth 根的标的集（selftest 抽样池）。"""
    counts = lake.groupby(level="symbol").size()
    return [s for s in syms if counts.get(s, 0) >= depth]


def _tail_frame(lake: pd.DataFrame, sym: str, n: int) -> pd.DataFrame:
    """取单标的尾部 n 根（模拟 gm 腿产物的形状：定点锚的尾部窗）。

    返回保持 MultiIndex(date,symbol)——与 _load_gm_leg 的读回形状同构，
    compare_core 的 participants 交集逻辑才不用为测试特判。
    """
    one = lake.xs(sym, level="symbol")[_OHLC + ["volume"]].tail(n).copy()
    one.insert(0, "symbol", sym)
    return one.reset_index().set_index(["date", "symbol"])


# ── compare 子命令 ────────────────────────────────────────────────────────────

def cmd_compare(args) -> None:
    syms = _universe()
    lake = _load_lake(syms)
    gm, gm_leg_name = _load_gm_leg(args.gm_leg)
    # gm 腿 symbol 应为 ts 口径（pull 落盘时已折回）；出现 SHSE./SZSE. 前缀即口径
    # 事故，fail-loud 而非静默换装（换装会掩盖「符号纪律被破坏」这个上游异变）
    bad = [s for s in gm.index.get_level_values("symbol").unique() if "." not in str(s)]
    if bad:
        _die(13, f"gm 腿出现非 ts 口径 symbol（疑似 gm 前缀泄漏）：{bad[:5]}")
    res = compare_core(lake, gm)
    today = datetime.now().strftime("%Y%m%d")
    out = STATE_DIR / f"parity_exclude_{today}.json"
    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tolerance_rel": _REL_TOL,
        "min_overlap_days": _MIN_OVERLAP,
        "summary": {k: v for k, v in res.items() if k != "excludes"},
        "excludes": res["excludes"],
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md = _markdown_summary(res, gm_leg_name)
    print(md, flush=True)
    print(f"\n[compare_data] 排除池：{out}（{len(res['excludes'])} 条）", flush=True)
    if args.md_out:
        Path(args.md_out).write_text(md + "\n", encoding="utf-8")
        print(f"[compare_data] markdown 摘要：{args.md_out}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="对拍比较器（详见模块 docstring）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("anchors", help="生成每标的 lake 末日锚表（供 gm_data_pull 定点复权）")
    sub.add_parser("selftest", help="比对器双向自检（无 gm/token 依赖）")
    c = sub.add_parser("compare", help="lake vs gm 腿正式对拍")
    c.add_argument("--gm-leg", default="", help="gm 腿产物路径（缺省取 state/ 下最新 parity_gm_*）")
    c.add_argument("--md-out", default="", help="把 markdown 摘要另存为文件（供贴报告）")
    args = ap.parse_args()
    {"anchors": cmd_anchors, "selftest": cmd_selftest, "compare": cmd_compare}[args.cmd](args)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
