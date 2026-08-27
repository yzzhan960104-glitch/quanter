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
from ops.miniqmt_guard import _notify


def _load_state() -> dict:
    try:
        return json.loads(gc.state_pkl_path().read_text(encoding="utf-8"))
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


def _api_position_symbols(token: str) -> list[str] | None:
    status, payload = gc.api_get(
        f"/v3/account-trade/positions/{gc.runtime_config().get('account_id')}", token)
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


def run_checks(now: datetime | None = None) -> list[dict]:
    now = now or datetime.now()
    today = f"{now:%Y-%m-%d}"
    cfg = gc.runtime_config()
    token = str(cfg.get("token") or "")
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    port_ok = probe_port_7001()
    api_ok, api_status = probe_api(cfg)
    add("终端网关", port_ok and api_ok,
        f"7001={'通' if port_ok else '断'} 7002={'通' if api_ok else f'status={api_status}'}")

    proc_n = probe_strategy_process(gc.GM_STRATEGY_DIR)
    add("策略进程", proc_n == 1, f"进程数={proc_n}")

    init = _today_init(gc.audit_csv_path(today), today)
    expect_acc = str(cfg.get("account_id") or "")
    got_acc = str((init or {}).get("account") or "")
    add("今日INIT", init is not None and got_acc == expect_acc,
        f"INIT={'有' if init else '无'} account={got_acc or '—'}（期望 {expect_acc}）"
        f" stamp={(init or {}).get('build_stamp', '—')}")

    state = _load_state()
    po = state.get("last_pre_open_date")
    add("盘前已跑", po == today, f"last_pre_open_date={po}（期望 {today}）")

    if api_ok:
        api_syms = _api_position_symbols(token)
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
        f"/v3/account-trade/unfinished-orders/{cfg.get('account_id')}", token)
    n_unf = len((unfinished or {}).get("data") or []) if st == 200 else -1
    add("在途单", True, f"在途={n_unf} 张（信息项）" if n_unf >= 0 else "查询失败（信息项）")

    return checks


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="掘金腿晨检（QMT 退役 P0/G-3）")
    p.add_argument("--ping-ok", action="store_true", help="正常时也发 INFO 报平安")
    p.add_argument("--register", action="store_true", help="注册 09:40 每日 schtasks")
    p.add_argument("--unregister", action="store_true")
    args = p.parse_args(argv)
    if args.register or args.unregister:
        import subprocess
        if args.register:
            py = ROOT / ".venv310" / "Scripts" / "python.exe"
            rc = subprocess.run(["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", "09:40",
                                 "/TN", "QuanterEmquantMorningCheck",
                                 "/TR", f'"{py}" "{Path(__file__)}"'],
                                capture_output=True).returncode
            print("registered" if rc == 0 else f"failed rc={rc}")
            return rc
        subprocess.run(["schtasks", "/Delete", "/TN", "QuanterEmquantMorningCheck", "/F"],
                       capture_output=True)
        print("unregistered")
        return 0

    now = datetime.now()
    checks = run_checks(now)
    bad = [c for c in checks if not c["ok"]]
    report = "\n".join(
        f"{'✅' if c['ok'] else '❌'} {c['name']}: {c['detail']}" for c in checks)
    out = f"掘金晨检 {now:%Y-%m-%d %H:%M}\n{report}"
    print(out)
    log = ROOT / "logs" / f"emquant_check_{now:%Y-%m-%d}.txt"
    log.parent.mkdir(exist_ok=True)
    log.write_text(out, encoding="utf-8")
    if bad:
        _notify("WARN", f"掘金晨检 {len(bad)}/{len(checks)} 项异常：\n" +
                "\n".join(f"❌ {c['name']}: {c['detail']}" for c in bad))
    elif args.ping_ok:
        _notify("INFO", f"掘金晨检全绿（{len(checks)} 项）")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
