# -*- coding: utf-8 -*-
"""掘金腿晨检自动化（QuanterEmquantMorningCheck · QMT 退役方案 P0 / G-3）。

每日 09:40 跑六查（README 晨检清单的机器化），异常钉钉告警、正常静默（--ping-ok
可开每日 INFO 报平安）；报告全文落 logs/emquant_check_YYYYMM-DD.txt（复盘留痕）。

六查（顺序=因果链：终端→策略→配置→盘前→账实→在途）：
  ① 终端网关：7001 可连 + 7002 API 心跳（guard 同款探针，故障双源速报）；
  ② 策略进程在场（09:40 属交易窗口）；
  ③ 今日 INIT：audit 有今日 INIT 且 account == runtime.json 期望账户（错账户=
     最危险的配错形态，单列一查）；
  ④ 盘前已跑：state.pkl 的 last_pre_open_date == 今日（09:31 定时/自愈兜底）；
  ⑤ 账实对账：7002 持仓 symbol 集合 == state.pkl remaining_qty>0 集合（漂移即 WARN）；
  ⑥ 在途单面：7002 unfinished-orders 非空时列出（信息项，不告警——回补/自愈会消化）。

⑦ 今日计划段（2026-09-01「掘金侧计划→播报」桥 · 晨检半场）：GM 侧新信号在
  09:31 盘前扫描落 audit（formed_at=T-1 完成形态），09:40 时挂单/成交状态已齐
  ——本段把「今天买什么、按什么价、什么风控位、成交与否」一次播清，对齐旧本地
  腿 T+1 计划推送的信息位（EOD 15:45 半场=⑤明日持仓预案，两段合璧）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

from ops import gm_ops_common as gc
from ops.gm_terminal_guard import _in_market_window, probe_api, probe_port_7001, probe_strategy_process
from ops.gm_ops_common import notify as _notify


def _load_state(leg_dir: Path | None = None) -> dict:
    try:
        return json.loads(gc.state_pkl_path(leg_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _today_init(audit_path: Path, today: str) -> dict | None:
    import csv
    if not audit_path.exists():
        return None
    init = None
    with audit_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.reader(fh):
            if len(row) >= 3 and row[1] == "INIT" and row[0].startswith(today):
                try:
                    init = json.loads(row[2])
                except ValueError:
                    init = {}
    return init


def _api_position_symbols(token: str, account_id: str) -> list[str] | None:
    status, payload = gc.api_get(
        f"/v3/account-trade/positions/{account_id}", token)
    if status != 200 or not isinstance(payload, dict):
        return None
    syms = []
    for p in payload.get("data") or []:
        vol = int(p.get("volume") or 0)
        if vol > 0:
            # gm 符号 SZSE.300433 → ts 口径 300433.SZ（与 state.pkl 对齐）
            ex, code = str(p.get("symbol", ".")).split(".", 1)
            suffix = {"SHSE": "SH", "SZSE": "SZ"}.get(ex, ex)
            syms.append(f"{code}.{suffix}")
    return syms


def run_checks(now: datetime | None = None, leg_dir: Path | None = None) -> list[dict]:
    """单腿六查（双腿形态 2026-08-28：main() 按 active_legs 循环调用；leg_dir=None
    走 gc.GM_STRATEGY_DIR 缺省——单腿调用方/旧测试语义不变）。"""
    now = now or datetime.now()
    today = f"{now:%Y-%m-%d}"
    cfg = gc.runtime_config(leg_dir)
    token = str(cfg.get("token") or "")
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    port_ok = probe_port_7001()
    api_ok, api_status = probe_api(cfg)
    add("终端网关", port_ok and api_ok,
        f"7001={'通' if port_ok else '断'} 7002={'通' if api_ok else f'status={api_status}'}")

    proc_n = probe_strategy_process(leg_dir or gc.GM_STRATEGY_DIR)
    add("策略进程", proc_n == 1, f"进程数={proc_n}")

    init = _today_init(gc.audit_csv_path(today, leg_dir), today)
    expect_acc = str(cfg.get("account_id") or "")
    got_acc = str((init or {}).get("account") or "")
    # 常驻跨日语义（2026-08-28 实跑纠偏）：进程不重启就没有当日 INIT——健康形态。
    # 本检查只抓「当日重启过但绑错账户」；策略是否在役由盘前已跑+进程两查负责。
    init_ok = (init is None) or (got_acc == expect_acc)
    add("INIT账户核", init_ok,
        (f"常驻跨日（无当日 INIT，正常）" if init is None else
         f"account={got_acc} stamp={(init or {}).get('build_stamp', '—')}")
        + f"（期望账户 {expect_acc}）")

    state = _load_state(leg_dir)
    po = state.get("last_pre_open_date")
    add("盘前已跑", po == today, f"last_pre_open_date={po}（期望 {today}）")

    if api_ok:
        api_syms = _api_position_symbols(token, expect_acc)
        st_syms = sorted(s for s, p in (state.get("positions") or {}).items()
                         if int((p or {}).get("remaining_qty") or 0) > 0)
        if api_syms is None:
            add("账实对账", False, "7002 持仓查询失败")
        else:
            same = sorted(api_syms) == st_syms
            add("账实对账", same,
                f"API={sorted(api_syms)} state={st_syms}" + ("" if same else " ⚠️漂移"))
    else:
        add("账实对账", False, "API 不可用，跳过（上查已报）")

    st, unfinished = gc.api_get(
        f"/v3/account-trade/unfinished-orders/{expect_acc}", token)
    n_unf = len((unfinished or {}).get("data") or []) if st == 200 else -1
    add("在途单", True, f"在途={n_unf} 张（信息项）" if n_unf >= 0 else "查询失败（信息项）")

    return checks


def _plan_lines(today: str, leg_dir: Path | None = None) -> list[str]:
    """⑦ 今日计划段：audit 只读解析 SIGNAL→挂单/成交状态（09:31 扫描结果播报）。

    状态判定：POS_ENRICHED/FILL 有 symbol → ✅ 已成交；有 ORDER_PLACED 无成交
    → ⏳ 在途；仅 SIGNAL → 未挂（拦截/额度/集合竞价未回）。缺 audit 文件=
    尚未扫描/非交易日 → 「无新信号」占位（不炸，与 _audit_stats 同降级语义）。
    """
    import csv
    src = gc.audit_csv_path(today, leg_dir)
    signals: list[dict] = []
    placed: dict[str, dict] = {}
    filled: set[str] = set()
    skip_held = 0
    blocked = 0
    if src.exists():
        with src.open(encoding="utf-8", newline="") as fh:
            for row in csv.reader(fh):
                if len(row) < 2 or not row[0].startswith(today):
                    continue
                ev = row[1]
                try:
                    d = json.loads(row[2]) if len(row) > 2 and row[2] else {}
                except ValueError:
                    d = {}
                if ev == "SIGNAL" and d.get("symbol"):
                    signals.append(d)
                elif ev == "ORDER_PLACED" and d.get("symbol"):
                    placed[str(d["symbol"])] = d
                elif ev in ("POS_ENRICHED", "FILL") and d.get("symbol"):
                    filled.add(str(d["symbol"]))
                elif ev == "SIGNAL_SKIP_HELD":
                    skip_held += 1
                elif ev == "ORDER_BLOCKED":
                    blocked += 1

    def _n(v, nd=2):
        return f"{v:.{nd}f}" if isinstance(v, (int, float)) and v == v else "—"

    names: dict[str, str] = {}
    if signals:
        try:
            from ops.emquant_eod_report import _name_map
            names = _name_map([str(d["symbol"]) for d in signals])
        except Exception:
            names = {}

    out = ["**⑦ 今日计划**（09:31 扫描执行）"]
    if not signals:
        out.append("- 今日无新信号（轮动空档日）")
    for d in signals:
        sym = str(d.get("symbol", "?"))
        pl = placed.get(sym)
        qty_s = (f" ×{int(pl.get('qty') or 0)} @ {_n(pl.get('price'))}"
                 if pl else "")
        status = ("✅ 已成交" if sym in filled
                  else "⏳ 在途" if pl else "· 未挂（拦截/额度）")
        out.append(f"- **{names.get(sym, '—')}** {sym}{qty_s}"
                   f"｜颈线 {_n(d.get('neckline'))} · RR {_n(d.get('rr'), 1)}"
                   f"｜{status}")
    tail = []
    if skip_held:
        tail.append(f"已持有跳过 {skip_held}")
    if blocked:
        tail.append(f"拦截 {blocked}")
    if tail:
        out.append("- ⏭ " + " · ".join(tail))
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="掘金腿晨检（QMT 退役 P0/G-3）")
    p.add_argument("--quiet-ok", action="store_true", help="正常时静默（默认每日播报全绿——2026-08-28 钉钉对齐掘金）")
    p.add_argument("--register", action="store_true", help="注册 09:40 每日 schtasks")
    p.add_argument("--unregister", action="store_true")
    args = p.parse_args(argv)
    if args.register or args.unregister:
        import subprocess
        if args.register:
            # W0（0828 评审）：/TR 走 run_ops_task.bat 统一包装器——schtasks 裸 python
            # 不捕获 stdout/stderr，启动期崩溃零痕迹（详见 bat 头注）。
            bat = Path(__file__).parent / "run_ops_task.bat"
            rc = subprocess.run(["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", "09:40",
                                 "/TN", "QuanterEmquantMorningCheck",
                                 "/TR", f'"{bat}" emquant_morning_check.py'],
                                capture_output=True).returncode
            print("registered" if rc == 0 else f"failed rc={rc}")
            return rc
        subprocess.run(["schtasks", "/Delete", "/TN", "QuanterEmquantMorningCheck", "/F"],
                       capture_output=True)
        print("unregistered")
        return 0

    now = datetime.now()
    # W3（2026-08-28 评审 P1）：交易日闸——schtasks /SC DAILY 周末照触发，而周末
    # 「盘前已跑」必 FAIL（last_pre_open_date 停在周五）→ 每周末误报。周末直接
    # 心跳播报跳过。注：法定节假日（非周末的非交易日）仍会做全量检查——节假日
    # 数据面检查多能通过（audit/EOD 文件在），残余误报风险留观，长期解=接交易日历。
    if now.weekday() >= 5:
        _notify("INFO", f"掘金晨检 {now:%Y-%m-%d} 非交易日（周末），跳过深检——schtask 心跳正常")
        return 0
    # 双腿形态（2026-08-28 双轨 §4.3）：按 active_legs 逐腿六查；exp 未部署时
    # 只有主腿——单腿时代报告形态不变。exp 腿异常同样进 WARN（不静默），报告
    # 文案带 [实验腿] 标签区分。2026-09-01：每腿追加 ⑦今日计划段（markdown 化）。
    all_bad: list[dict] = []
    reports: list[str] = []
    for leg in gc.active_legs():
        checks = run_checks(now, gc.leg_strategy_dir(leg))
        bad = [c for c in checks if not c["ok"]]
        all_bad.extend({"leg": leg.label, **c} for c in bad)
        leg_dir = gc.leg_strategy_dir(leg)
        body = [f"**—— {leg.label} ——**"] + [
            f"{'✅' if c['ok'] else '❌'} {c['name']}: {c['detail']}" for c in checks]
        body += [""] + _plan_lines(f"{now:%Y-%m-%d}", leg_dir)
        reports.append("\n".join(body))
    report = "\n\n".join(reports)
    out = f"### 🌅 掘金晨检 · {now:%Y-%m-%d %H:%M}\n\n{report}"
    print(out)
    log = ROOT / "logs" / f"emquant_check_{now:%Y-%m-%d}.txt"
    log.parent.mkdir(exist_ok=True)
    log.write_text(out, encoding="utf-8")
    if all_bad:
        _notify("WARN", f"掘金晨检 {len(all_bad)} 项异常：\n" +
                "\n".join(f"❌ [{b['leg']}] {b['name']}: {b['detail']}" for b in all_bad))
    elif not args.quiet_ok:
        _notify("INFO", "掘金晨检全绿 ✅\n\n" + report)
    return 1 if all_bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
