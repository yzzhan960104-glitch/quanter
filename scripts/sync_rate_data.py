# -*- coding: utf-8 -*-
"""利率 / 货币供应数据补采（代理 tnskhdata · token[0] 固定）。

为四层动能评分 · A股动能层【宏观流动性子模块】补数据：
  - shibor:     Shibor 利率 on/1w/.../1y（日频）—— 补全 2016-2026（现有仅 2023-07 起，缺熊市段）
  - cn_m:       货币供应量 M0/M1/M2 + 同比/环比（月频）—— 新采，m2_yoy = 央行水龙头
  - shibor_lpr: LPR 贷款基础利率 1y/5y（日频）—— 新采，实体融资成本

代理缺 cn_sf(社融)/yc_cb(国债)/usb_yield(美债)：探测均 No such method（probe_rate_fields.py）。
替代项：M1-M2 剪刀差（cn_m.m1_yoy−m2_yoy，实体活性 vs 金融空转）替代社融动能；
       Shibor 期限利差（1y−on，银行间长短端）替代国债期限利差。不阻塞评分。

字段/参数已 probe_rate_fields.py 实测确认。单次 2000 条上限 → 日频接口按年分段拉。
"""
import os
import sys
import time

import pandas as pd
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data._tushare_compat import get_pro  # noqa: E402

pro = get_pro()
LAKE = "data_lake"
SD_YEAR, ED_YEAR = 2016, 2026   # 全历史；今天 2026-07-19，年末分段自动只取到有的


def fetch_by_year(api, sd_year, ed_year, **extra):
    """日频接口按年分段拉（规避单次 2000 条上限 + 限频），concat 按 date 去重返 DF。

    每段间 sleep 0.3s 防限频；单年异常不中断（记录后继续）。
    """
    frames = []
    for y in range(sd_year, ed_year + 1):
        try:
            df = getattr(pro, api)(start_date=f"{y}0101", end_date=f"{y}1231", **extra)
            if df is not None and len(df):
                frames.append(df)
                print(f"  {api} {y}: +{len(df)} 行")
            time.sleep(0.3)
        except Exception as e:
            print(f"  {api} {y}: 异常 {str(e)[:80]}")
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["date"]).sort_values("date")
    return out


def main():
    os.makedirs(LAKE, exist_ok=True)

    # 1. Shibor 全历史（覆盖现有：现有仅 2023-07 起，重拉补全 2016 起，date 作 index 对齐格式）
    print(">>> shibor 按年分段拉 2016-2026 ...")
    sh = fetch_by_year("shibor", SD_YEAR, ED_YEAR)
    if len(sh):
        sh = sh.set_index("date")
        sh.to_parquet(f"{LAKE}/shibor.parquet")
        print(f"  → shibor.parquet {len(sh)} 行 [{sh.index.min()}~{sh.index.max()}]\n")

    # 2. LPR 全历史（日频，月度才变值，但接口按日返）
    print(">>> shibor_lpr 按年分段拉 2016-2026 ...")
    lpr = fetch_by_year("shibor_lpr", SD_YEAR, ED_YEAR)
    if len(lpr):
        lpr = lpr.set_index("date")
        lpr.to_parquet(f"{LAKE}/lpr.parquet")
        print(f"  → lpr.parquet {len(lpr)} 行 [{lpr.index.min()}~{lpr.index.max()}]\n")

    # 3. 货币供应量（月频，130 条一次拉全，无需分段）
    print(">>> cn_m 一次拉全 2016-01 ~ 2026-07 ...")
    try:
        cm = pro.cn_m(start_m="201601", end_m="202607")
        cm = cm.sort_values("month").reset_index(drop=True)
        cm.to_parquet(f"{LAKE}/cn_m.parquet", index=False)
        print(f"  → cn_m.parquet {len(cm)} 行 [{cm['month'].min()}~{cm['month'].max()}] 字段={list(cm.columns)}\n")
    except Exception as e:
        print(f"  cn_m 异常: {str(e)[:120]}\n")


if __name__ == "__main__":
    main()
