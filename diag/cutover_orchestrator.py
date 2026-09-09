# -*- coding: utf-8 -*-
"""A 方案编排器(2026-09-07):收盘后自动执行退役+部署,隔夜清仓自动续跑。"""
import subprocess, sys, time, datetime as dt
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent

def now():
    return dt.datetime.now()

def wait_until(h, m):
    target = now().replace(hour=h, minute=m, second=0, microsecond=0)
    if now() >= target:
        return False
    print(f"等待 {target:%H:%M} ...", flush=True)
    time.sleep(max(0, (target - now()).total_seconds()))
    return True

def probe_7002(timeout=8):
    import json, os, urllib.request
    cfg = json.load(open(os.path.expanduser(
        "~/.emgm3/projects/d9324346-9d1b-11f1-ae25-7c10c93fcb7d/config/runtime.json"), encoding="utf-8"))
    try:
        r = urllib.request.Request("http://127.0.0.1:7002/v3/strategies",
                                   headers={"Authorization": f"Bearer {cfg['token']}"})
        urllib.request.urlopen(r, timeout=timeout)
        return True
    except Exception:
        return False

# ① 收盘后 15:10 首试
wait_until(15, 10)
for attempt in range(30):                       # 15:10-20:00 每十分钟探测
    if probe_7002():
        print(f"[{now():%H:%M}] 7002 恢复,执行退役脚本", flush=True)
        break
    print(f"[{now():%H:%M}] 7002 仍挂(第{attempt+1}次)", flush=True)
    if now().hour >= 20:
        print("!! 20:00 仍未恢复——需要人工重启终端(登录人闸),退出", flush=True)
        sys.exit(3)
    time.sleep(600)
else:
    sys.exit(3)

r = subprocess.run([str(ROOT / ".venv310/Scripts/python.exe"),
                    str(ROOT / "diag/retire_exp_provision_balanced.py")],
                   cwd=str(ROOT), timeout=3600)
print(f"退役脚本退出码 {r.returncode}", flush=True)
if r.returncode != 0:
    # 持仓未清(隔夜排队)→ 明早 09:45 续跑
    print("隔夜清仓排队中,明早 09:45 续跑", flush=True)
    wait_until(9, 45)
    wait = True
    for _ in range(6):                          # 09:45-09:55 等成交回报
        if probe_7002():
            r2 = subprocess.run([str(ROOT / ".venv310/Scripts/python.exe"),
                                 str(ROOT / "diag/retire_exp_provision_balanced.py")],
                                cwd=str(ROOT), timeout=3600)
            print(f"续跑退出码 {r2.returncode}", flush=True)
            if r2.returncode == 0:
                sys.exit(0)
        time.sleep(120)
    sys.exit(2)
print("A 方案完成", flush=True)
