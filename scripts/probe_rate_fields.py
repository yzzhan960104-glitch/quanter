# -*- coding: utf-8 -*-
"""利率 / 货币供应 / 国债 接口探测（代理 tnskhdata · 固定 token[0]）。

目的：实测四层动能评分 · A股动能层【宏观流动性子模块】所需接口的字段/参数/权限，
为补采 Shibor 历史段 + LPR + M2 + 社融 + 国债收益率做准备。

对齐 probe_tushare_fields.py：固定 token[0]（多 token 权限不一，轮询致间歇失败）。
代理 tnskhdata 非 tushare 全量接口都有（实测 concept/szse_daily 报 No such method），
利率相关同样要逐个实测——getattr None / 异常 / 空均记录。

用法：
    PYTHONIOENCODING=utf-8 python -u scripts/probe_rate_fields.py
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data._tushare_compat import get_pro, source_name  # noqa: E402

pro = get_pro()
print(f"[probe] source={source_name()}（代理优先；回退 tushare 直连）\n")

# 探测清单：(接口名, [(参数名变体1, dict), (参数名变体2, dict)], 备注)
# 参数名变体：cn_m/cn_sf 的 start_m/end_m vs start_date/end_date 是 brief 假设，两套都试。
# 历史段定 2018-01：① 验证能否补 2016-2023 中段；② 覆盖颈线法熊市软肋年份（2018）。
PROBES = [
    ("shibor",     [("date", {"start_date": "20180101", "end_date": "20180115"})], "Shibor 利率（补2018历史段；on/1w/2w/1m/3m/6m/9m/1y）"),
    ("shibor_lpr", [("date", {"start_date": "20180101", "end_date": "20180131"})], "LPR 贷款基础利率（1y/5y）"),
    ("libor",      [("date", {"start_date": "20180101", "end_date": "20180115"})], "Libor（隔夜/1m/3m/6m/12m）"),
    ("hibor",      [("date", {"start_date": "20180101", "end_date": "20180115"})], "Hibor 香港银行间利率"),
    # 货币供应量 M0/M1/M2：sync_macro_credit 注释明写字段/参数为 brief 假设，需真 token 实测
    ("cn_m",       [("month", {"start_m": "201801", "end_m": "201803"}),
                    ("date",  {"start_date": "201801", "end_date": "201803"})], "货币供应量 M0/M1/M2 同比（参数名待实测）"),
    # 社融增量：接口名/参数均为推测，逐个试
    ("cn_sf",      [("month", {"start_m": "201801", "end_m": "201803"})], "社融增量（接口名待实测）"),
    # 中债国债收益率曲线：10Y/2Y 利差（衰退/扩张预期核心）
    ("yc_cb",      [("date", {"start_date": "20180101", "end_date": "20180115"})], "中债国债收益率曲线（期限利差）"),
    # 美国国债收益率（导航"美国利率"，全球风险偏好/外资流动）：接口名待测，试候选
    ("usb_yield",  [("date", {"start_date": "20180101", "end_date": "20180115"})], "美国国债收益率（候选接口名·待实测）"),
]


def _try(api, params):
    fn = getattr(pro, api, None)
    if fn is None:
        return "NO_METHOD", None
    df = fn(**params)
    if df is None or len(df) == 0:
        return "EMPTY", None
    return "OK", df


for api, variants, note in PROBES:
    print(f">>> {api}  — {note}")
    if getattr(pro, api, None) is None:
        print(f"    ✗ 代理无此方法（getattr None）\n")
        continue
    for _, params in variants:
        try:
            status, df = _try(api, params)
            if status == "OK":
                print(f"    ✓ {len(df)}行 | 参数={params} | 字段={list(df.columns)}")
                # 样例（中文银行名/数值各取首2行，截断长串）
                for r in df.head(2).to_dict("records"):
                    print(f"        {r}")
                print()
                break               # 命中即停（不再试参数变体）
            else:
                print(f"    △ {status} | 参数={params}（试下一变体）")
        except Exception as e:
            print(f"    ✗ 异常 | 参数={params} | {str(e)[:140]}")
    else:
        print(f"    （所有参数变体均失败）\n")
