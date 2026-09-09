# -*- coding: utf-8 -*-
"""A 方案一键执行:退役实验腿 → 腾户 → 建均衡栈户 → 部署(2026-09-07 用户裁决)。

前置:7002 网关健康(调用方先探测)。步骤(每步幂等可重跑):
  1 停实验腿策略(POST strategy-commands/<sid>/stop)
  2 清仓实验腿(POST account-trade/close-all-positions/<acc>);持仓=0 才继续
  3 删实验腿账户(POST /v5/term/account/delete-account)
  4 建新户「均衡栈试点」(gm-broker-1, initCash 200000, simulate)
  5 login 激活 + credential 自动连接
  6 组装 balanced 产物(build_pilot --leg balanced --account-id <new>)
  7 gm_provision_leg 一键注册部署(云端+本机+目录+绑定+状态表)
  8 relaunch + INIT 三锚验证
用法:python diag/retire_exp_provision_balanced.py [--skip-to N]
"""
from __future__ import annotations
import json, os, subprocess, sys, time, urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

EXP_SID = "5557c9cb-a308-11f1-915e-52560acd7b8c"
EXP_ACC = "c4ba3b2e-a2da-11f1-9262-52560acd7da0"
MAIN_DIR = os.path.expanduser("~/.emgm3/projects/d9324346-9d1b-11f1-ae25-7c10c93fcb7d")
TOK = json.load(open(f"{MAIN_DIR}/config/runtime.json", encoding="utf-8"))["token"]
H = {"Authorization": f"Bearer {TOK}", "Content-Type": "application/json"}

def call(method, path, body=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"http://127.0.0.1:7002{path}", data=data, headers=H, method=method)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode() or "{}")

def step(n, desc):
    print(f"\n===== [{n}] {desc} =====", flush=True)

# 1 停策略
step(1, "停实验腿策略")
try:
    s, _ = call("POST", f"/v3/strategy-commands/{EXP_SID}/stop",
                {"strategyId": EXP_SID, "reason": "退役(A 方案:均衡栈替换)",
                 "reasonDetail": "四方面对比无胜出维度,用户 09-07 裁决"})
    print("stop:", s)
except urllib.error.HTTPError as e:
    print("stop HTTP", e.code, e.read().decode()[:120])

# 2 清仓
step(2, "清仓实验腿(1 只持仓)")
s, d = call("POST", f"/v3/account-trade/close-all-positions/{EXP_ACC}")
print("close-all:", s, str(d)[:120])
for i in range(40):
    time.sleep(20 if i else 10)
    _, cash = call("GET", f"/v3/account-trade/positions/{EXP_ACC}")
    npos = len((cash or {}).get("data") or [])
    print(f"  持仓 {npos} ({time.strftime('%H:%M:%S')})", flush=True)
    if npos == 0:
        break
else:
    print("!! 持仓未清零,中止(账户不删)"); sys.exit(1)

# 3 删户
step(3, "删除实验腿账户")
s, d = call("POST", "/v5/term/account/delete-account", {"accountId": EXP_ACC})
print("delete:", s, str(d)[:150])

# 4 建户
step(4, "创建均衡栈试点账户")
s, d = call("POST", "/v5/term/account/create-account",
            {"serviceOrgcode": "eastmoney", "account": {"title": "均衡栈试点"},
             "channelId": "gm-broker-1",
             "extProperties": {"initCash": "200000", "engineId": "simulate"}})
print("create:", s, json.dumps(d, ensure_ascii=False)[:300])
new_acc = None
for it in (d.get("data") or {}).get("accounts") or ([d.get("data")] if isinstance(d.get("data"), dict) else []):
    a = it.get("account") or it
    if isinstance(a, dict) and a.get("title") == "均衡栈试点":
        new_acc = a.get("account_id")
if not new_acc:
    # 兜底:列表找
    s2, d2 = call("GET", "/v5/term/account/accounts")
    for it in (d2.get("data") or []):
        a = it.get("account") or {}
        if a.get("title") == "均衡栈试点":
            new_acc = a.get("account_id")
assert new_acc, "新账户 id 未取得"
print("NEW_ACCOUNT:", new_acc)

# 5 激活
step(5, "login 激活 + credential")
try:
    print("login:", call("POST", f"/v3/account-trade/login/{new_acc}")[0])
except urllib.error.HTTPError as e:
    print("login HTTP", e.code)
try:
    print("cred:", call("PATCH", f"/v3/account-connections/{new_acc}/credential",
                        {"accountId": new_acc, "password": ""})[0])
except urllib.error.HTTPError as e:
    print("cred HTTP", e.code)

# 6 组装
step(6, "组装 balanced 产物")
r = subprocess.run([str(ROOT / ".venv310/Scripts/python.exe"),
                    str(ROOT / "emquant/build_pilot.py"), "--leg", "balanced",
                    "--account-id", new_acc], capture_output=True, text=True, cwd=str(ROOT))
print(r.stdout.strip() or r.stderr.strip()[-300:])

# 7 一键注册部署
step(7, "gm_provision_leg 部署(--account-id 跳过建户)")
r = subprocess.run([str(ROOT / ".venv310/Scripts/python.exe"),
                    str(ROOT / "emquant/tools/gm_provision_leg.py"),
                    "--account-id", new_acc,
                    "--strategy-name", "NECK-BALANCED",
                    "--deploy-artifact", str(ROOT / "emquant/emquant_neckline_pilot_balanced.py")],
                   capture_output=True, text=True, cwd=str(ROOT), timeout=600)
print(r.stdout[-2000:] or r.stderr[-500:])

# 8 INIT 验证由调用方人工核(audit INIT 三锚)
print("\nDONE — 请核 ~/.emgm3/projects/<新sid>/audit INIT 三锚(stamp/account/strategy_id)")
