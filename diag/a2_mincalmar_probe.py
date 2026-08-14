# -*- coding: utf-8 -*-
"""A2 实证：25c602 冠军参数在扩展口径（inner 2021-2024）下的各年 calmar 分解。

物理意图：验证「新目标确有判别力」——旧口径 inner(2025) calmar 44.87 的冠军，
在四年考场下 min 应显著塌陷（2022 熊市年预期 ≈0 或负）。同时录单组耗时
（P1 后 35.5s/组是 2025+ 窗口口径，扩窗 ×3 的实测数供 estimate_budget 校正）。
"""
import sys, os, time, json, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from discovery.snapshot import freeze
from discovery.split import extended_split
from discovery.objective import evaluate

conn = sqlite3.connect("experiment/experiments.db")
conn.row_factory = sqlite3.Row
params = json.loads(conn.execute(
    "SELECT params FROM experiment_version WHERE status='ACTIVE'").fetchone()["params"])

t0 = time.time()
universe, meta = freeze("2021-01-01")
t_freeze = time.time() - t0
t1 = time.time()
res = evaluate(params, universe, extended_split())
t_eval = time.time() - t1
print(f"universe={meta.universe_count} freeze={t_freeze:.1f}s eval={t_eval:.1f}s")
inner = res["inner"]
print("inner 整段:", {k: (round(v, 3) if isinstance(v, float) else v)
                      for k, v in inner.items() if k != "yearly_calmar"})
print("各年 calmar:", inner["yearly_calmar"])
print("min_yearly_calmar:", round(inner["min_yearly_calmar"], 3))
