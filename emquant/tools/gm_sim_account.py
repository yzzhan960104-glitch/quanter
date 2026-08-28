# -*- coding: utf-8 -*-
"""掘金仿真账户管理工具(完全自动化路径 · 2026-08-28)。

物理意图:
    「添加仿真账户」的 UI 表单后端 = 本机 gmterm-serv(7002)的
    POST /v5/term/account/create-account(gmterm-serv 统一代理 walle-v5 云端 gRPC)——
    一次调用完成 账户+柜台通道+期初资金+撮合引擎 四件事,零 GUI。本工具是其 CLI 封装,
    掘金侧多腿(主/实验)部署的账户供给段。

关键事实(2026-08-28 实测,详见 skill goldminer-terminal 云端通道段):
    - 走本机 7002 只需终端 token(runtime.json),无需云端 encryptedToken;
    - body 为 camelCase(UI 原生形态直发,勿 decamelize);
    - channelId 用 gm-broker-1(掘金仿真交易-普通,与主账户 67334fef 同通道,
      双腿同撮合器是对照公平的前提);
    - engineId: simulate=仿真撮合(价格优先+时间优先,交易时段)/ emulate=模拟撮合(7×24);
    - 创建后需 POST /v3/account-trade/login/<id>(本机)激活交易通道,否则柜台报
      account not exist(state=6);
    - 中文字符串必须 UTF-8 发送(curl 从 Windows shell 传参会 GBK 乱码——首次创建
      事故实锤,当场删除重建);
    - 云端 61.129.248.86:7102(broker-rpcgw)也有 /v3/accounts 族,但那只建"账户壳"
      (broker_api.AddAccountReq 仅收 account.title),不带通道/资金——勿混用。

用法(Git Bash、仓库根目录):
    PYTHONUTF8=1 E:/quanter/.venv310/Scripts/python.exe emquant/tools/gm_sim_account.py list
    ... gm_sim_account.py create --title 实验腿 --init-cash 100000
    ... gm_sim_account.py create --title X --init-cash 100000 --engine emulate --no-login
    ... gm_sim_account.py delete --id <account_id>
    ... gm_sim_account.py login --id <account_id>

退出码:0=成功 / 2=参数或环境错误 / 3=服务端拒绝(HTTP!=200)。仅 stdlib(工具家族约定)。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

ROOT = Path(__file__).resolve().parents[2]
GM_API_BASE = os.environ.get("GM_API_BASE", "http://127.0.0.1:7002")
# 终端 token 来源:主腿 runtime.json(env GM_STRATEGY_DIR 可覆写,同 ops/gm_ops_common)
_RUNTIME = Path(os.environ.get(
    "GM_STRATEGY_DIR",
    r"C:\Users\yzzhan\.emgm3\projects\d9324346-9d1b-11f1-ae25-7c10c93fcb7d",
)) / "config" / "runtime.json"

# 通道/撮合的缺省与主账户(67334fef)同款——双腿同撮合器是 A/B 对照公平的前提。
DEFAULT_CHANNEL_ID = "gm-broker-1"
DEFAULT_ENGINE_ID = "simulate"


def _token() -> str:
    if not _RUNTIME.exists():
        print(f"[sim-account] 终止:缺 {_RUNTIME}(终端 token 来源,GM_STRATEGY_DIR 可覆写)")
        sys.exit(2)
    tok = str(json.loads(_RUNTIME.read_text(encoding="utf-8")).get("token") or "").strip()
    if not tok:
        print("[sim-account] 终止:runtime.json 缺 token")
        sys.exit(2)
    return tok


def _req(method: str, path: str, body: dict | None = None) -> tuple[int, object]:
    """带鉴权请求(本机 7002)→ (status, parsed)。body 恒 UTF-8(中文 title 防乱码)。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    r = urllib.request.Request(
        GM_API_BASE + path, data=data, method=method,
        headers={"Authorization": "Bearer " + _token(),
                 "Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(r, timeout=12) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]


def cmd_list(args=None) -> int:
    st, d = _req("GET", "/v5/term/account/accounts")
    if st != 200:
        print(f"[sim-account] list 失败 HTTP {st}: {d}")
        return 3
    for row in (d or {}).get("data", []):
        a, c = row.get("account", {}), row.get("channel", {})
        ext = row.get("ext_properties") or row.get("extProperties") or {}
        engine = ext.get("engineId") or ext.get("engine_id") or "—"
        cash = ext.get("initCash") or ext.get("init_cash") or "—"
        print(f"{a.get('account_id')} | {a.get('title')} | {c.get('channel_id')} | "
              f"engine={engine} | init_cash={cash} | {a.get('created_at', '')[:19]}")
    return 0


def cmd_create(args) -> int:
    body = {
        "serviceOrgcode": "eastmoney",
        "account": {"title": args.title},
        "channelId": args.channel,
        "extProperties": {"initCash": str(int(args.init_cash)), "engineId": args.engine},
    }
    st, d = _req("POST", "/v5/term/account/create-account", body)
    if st != 200:
        print(f"[sim-account] create 失败 HTTP {st}: {d}")
        return 3
    acc = (d or {}).get("account", {})
    print(f"[sim-account] 已创建: account_id={acc.get('account_id')} title={acc.get('title')}")
    if not args.no_login:
        _login(acc.get("account_id"))
    return 0


def _login(account_id: str) -> None:
    if not account_id:
        return
    st, d = _req("POST", f"/v3/account-trade/login/{account_id}", {})
    state = ((d or {}).get("status", {}) or {}).get("status", {}) if isinstance(d, dict) else {}
    print(f"[sim-account] login: HTTP {st} state={state.get('state')} "
          f"{state.get('error', {}).get('info', '') if isinstance(state.get('error'), dict) else ''}")


def cmd_delete(args) -> int:
    st, d = _req("POST", "/v5/term/account/delete-account", {"account_id": args.id})
    if st == 404 or (isinstance(d, str) and "Not Found" in str(d)):
        # 退化路径:term 路由不可用时走云端 broker DELETE(仅删壳;encryptedToken 需另取)
        print("[sim-account] term delete 路由不可用——用云端 DELETE 需 encryptedToken,"
              "或终端 UI 手动删除")
        return 3
    if st != 200:
        print(f"[sim-account] delete 失败 HTTP {st}: {d}")
        return 3
    print(f"[sim-account] 已删除: {args.id}")
    return 0


def cmd_login(args) -> int:
    _login(args.id)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="掘金仿真账户 CLI(本机 7002,零 GUI)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="列出全部 term 账户(含通道/资金/撮合)")
    p_c = sub.add_parser("create", help="创建仿真账户(账户+通道+资金+撮合一体)")
    p_c.add_argument("--title", required=True, help="账户显示名")
    p_c.add_argument("--init-cash", type=float, default=100000.0, help="期初资金(默认 10 万)")
    p_c.add_argument("--engine", default=DEFAULT_ENGINE_ID,
                     choices=["simulate", "emulate"], help="撮合引擎(默认 simulate)")
    p_c.add_argument("--channel", default=DEFAULT_CHANNEL_ID,
                     help=f"柜台通道(默认 {DEFAULT_CHANNEL_ID},与主账户同款)")
    p_c.add_argument("--no-login", action="store_true", help="创建后不激活交易通道")
    p_d = sub.add_parser("delete", help="删除账户")
    p_d.add_argument("--id", required=True)
    p_l = sub.add_parser("login", help="激活账户交易通道(重启终端后可能需要)")
    p_l.add_argument("--id", required=True)
    args = ap.parse_args()
    return {"list": cmd_list, "create": cmd_create,
            "delete": cmd_delete, "login": cmd_login}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
