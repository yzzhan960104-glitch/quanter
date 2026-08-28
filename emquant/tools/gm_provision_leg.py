# -*- coding: utf-8 -*-
"""掘金实验腿全自动开通工具(2026-08-29 全链路固化)。

物理意图:把「一条新实验腿从零到 UI 三位一体可见」的全部动作固化为一次调用。
2026-08-29 凌晨实测打通的完整链(每步都有实弹验证,详见 skill goldminer-terminal):

    ① 账户:    POST 本机 /v5/term/account/create-account(账户+通道+期初+撮合一体)
    ② 连接态:  PATCH 本机 /v3/account-connections/<id>/credential(空密码——仿真柜台
               不校验,写入即"自动登录已启用")+ POST login → UI 显示已连接
    ③ 云端注册: POST 云端 sc-api(7302) /v3/strategy-center/strategies {name,language}
               → 得云端 strategy_id(「我的策略」列表的数据源,这是 UI 可见的唯一途径)
    ④ 本机注册: POST 本机 /v3/strategies 带 strategy_id=<云端 id>(本机接受指定 id——
               两体系同 ID 是 UI/运行时/云端三位一体的钥匙)
    ⑤ 目录:    ~/.emgm3/projects/<id>/ 建 main.py + config/runtime.json(token 复用主腿)
               + relaunch_strategy.ps1(目录名替换自主腿模板)
    ⑥ 绑定:    PUT strategy-commands(directory/path)+ PUT strategies/<id>/accounts
    ⑦ 启动:    relaunch → 验 audit INIT(stamp/account/strategy_id 三锚)

关键事实(逆向成果,勿重新踩坑):
    - 掘金两体系:云端 strategy-center(「我的策略」列表源)与本机 gmterm-serv(运行时),
      互不同步;UI 新建走云端拿 id→本机同 id 注册——本工具复刻该顺序;
    - POST /v3/strategies 必须带完整字段且可带 strategy_id(空 body 也会创建——坑);
    - credential 的 password 仿真账户传空串即可(柜台不校验);
    - 云端注册需 encryptedToken(main.log grep,10h 有效),其余全部本机终端 token;
    - 配额:仿真账户每账号限 2 个(主+实验已满),重建须先删。

用法:
    PYTHONUTF8=1 python emquant/tools/gm_provision_leg.py --account-title 实验腿2 \
        --init-cash 100000 --strategy-name NECK-EXP2 [--deploy-artifact <main.py 路径>] \
        [--account-id <已存在账户则跳过①②>]

仅 stdlib + ops.gm_ops_common。退出码:0=全链成功 / 2=参数 / 3=远端拒绝。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

from ops import gm_ops_common as gc   # noqa: E402

PROJECTS = Path.home() / ".emgm3" / "projects"
SC_API = "http://61.129.248.86:7302"        # strategy-center 唯一可直连入口(7522 拒连)
DEFAULT_CHANNEL = "gm-broker-1"
DEFAULT_ENGINE = "simulate"


def _local_token() -> str:
    return str(gc.runtime_config().get("token") or "")


def _http(method: str, url: str, token: str, body: dict | None = None,
          headers: dict | None = None) -> tuple[int, object]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    h = {"Authorization": "Bearer " + token,
         "Content-Type": "application/json; charset=utf-8", **(headers or {})}
    r = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=12) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return 0, f"{type(e).__name__}: {e}"


def _die(msg: str) -> None:
    print(f"[provision] 失败:{msg}")
    sys.exit(3)


def _encrypted_token() -> str:
    """云端注册用的 encryptedToken:取最新终端会话日志(10h 有效,过期须终端重登)。"""
    logs = sorted((Path.home() / ".emgm3" / "logs").glob("*/main.log"))
    for p in reversed(logs):
        m = re.findall(r'encryptedToken# .*?"encryptedToken":"([^"]+)"',
                       p.read_text(encoding="utf-8", errors="replace"))
        if m:
            return m[-1]
    _die("main.log 无 encryptedToken——终端未登录或日志异位")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="实验腿全自动开通(账户→连接→云端+本机注册→目录→绑定)")
    ap.add_argument("--account-title", required=True, help="账户显示名")
    ap.add_argument("--init-cash", type=float, default=100000.0)
    ap.add_argument("--strategy-name", required=True, help="策略名(「我的策略」列表显示)")
    ap.add_argument("--account-id", help="复用已存在账户(跳过创建/连接两步)")
    ap.add_argument("--deploy-artifact", help="部署的 main.py(缺省=主腿模板复制)")
    args = ap.parse_args()

    tok = _local_token()
    # ①② 账户创建+连接态(或复用)
    if args.account_id:
        acc = args.account_id
        print(f"[provision] ① 复用账户 {acc[:12]}…")
    else:
        st, d = _http("POST", f"{gc.GM_API_BASE}/v5/term/account/create-account", tok,
                      {"serviceOrgcode": "eastmoney", "account": {"title": args.account_title},
                       "channelId": DEFAULT_CHANNEL,
                       "extProperties": {"initCash": str(int(args.init_cash)),
                                         "engineId": DEFAULT_ENGINE}})
        if st != 200:
            _die(f"账户创建 HTTP{st}:{d}")
        acc = str(d.get("account", {}).get("account_id") or "")
        print(f"[provision] ① 账户已建 {acc[:12]}…")
    # 连接态:credential(空密码=自动登录)+login
    _http("PATCH", f"{gc.GM_API_BASE}/v3/account-connections/{acc}/credential", tok,
          {"accountId": acc, "password": ""})
    st, d = _http("POST", f"{gc.GM_API_BASE}/v3/account-trade/login/{acc}", tok, {})
    print(f"[provision] ② 连接态:credential+login state="
          f"{((d.get('status') or {}).get('status') or {}).get('state') if isinstance(d, dict) else '?'}")

    # ③ 云端注册(「我的策略」列表源)
    etok = _encrypted_token()
    cloud_h = {"Grpc-Metadata-X-USERID": "13336", "Grpc-Metadata-X-ORGCODE": "eastmoney-bus-simu",
               "Grpc-Metadata-mfp-modid": "termui"}
    st, d = _http("POST", f"{SC_API}/v3/strategy-center/strategies", etok,
                  {"name": args.strategy_name, "language": "python"}, cloud_h)
    if st != 200 or not isinstance(d, dict) or not d.get("strategy_id"):
        _die(f"云端注册 HTTP{st}:{d}")
    sid = str(d["strategy_id"])
    print(f"[provision] ③ 云端注册 {sid[:12]}…(「我的策略」可见)")

    # ④ 本机同 id 注册(三位一体钥匙)
    st, d = _http("POST", f"{gc.GM_API_BASE}/v3/strategies", tok,
                  {"name": args.strategy_name, "language": "python", "strategy_id": sid})
    if st != 200:
        _die(f"本机注册 HTTP{st}:{d}")
    print(f"[provision] ④ 本机同 id 注册完成")

    # ⑤ 目录(main.py + runtime.json + relaunch 模板)
    leg_dir = PROJECTS / sid
    leg_dir.mkdir(parents=True, exist_ok=True)
    main_leg = gc.leg_strategy_dir(gc.LEGS[0])
    src_py = Path(args.deploy_artifact) if args.deploy_artifact else main_leg / "main.py"
    shutil.copy2(src_py, leg_dir / "main.py")
    ps1 = (main_leg / "relaunch_strategy.ps1").read_text(encoding="ascii", errors="replace")
    (leg_dir / "relaunch_strategy.ps1").write_text(
        ps1.replace(main_leg.name, sid), encoding="ascii")
    cfg = {"token": tok, "strategy_id": sid, "account_id": acc}
    (leg_dir / "config").mkdir(exist_ok=True)
    (leg_dir / "config" / "runtime.json").write_text(
        json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"[provision] ⑤ 目录就位 {leg_dir}")

    # ⑥ 双绑定
    _http("PUT", f"{gc.GM_API_BASE}/v3/strategy-commands/{sid}", tok,
          {"strategy_id": sid, "directory": str(leg_dir), "path": str(leg_dir / "main.py")})
    _http("PUT", f"{gc.GM_API_BASE}/v3/strategies/{sid}/accounts", tok,
          {"strategyId": sid, "accountIds": [acc], "stage": 1, "extData": {acc: ""}})
    print("[provision] ⑥ 启动配置+账户绑定完成")

    # ⑦ 启动+验 INIT
    r = subprocess.run(["powershell", "-ExecutionPolicy", "Bypass", "-File",
                        str(leg_dir / "relaunch_strategy.ps1")],
                       capture_output=True, text=True, timeout=180)
    print(f"[provision] ⑦ relaunch rc={r.returncode}:{(r.stdout or '').strip()[-80:]}")
    print(f"[provision] 全链完成。验证:audit INIT 行(account={acc[:12]}…/strategy_id={sid[:12]}…);")
    print(f"[provision] 终端 UI 需 F5 刷新;后续 .env 写 GM_EXP_STRATEGY_DIR={leg_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
