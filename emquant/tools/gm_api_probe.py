# -*- coding: utf-8 -*-
"""gm SDK API 探针（东财掘金颈线单文件试点 Task 1 产物）。

Why 存在：掘金官方文档为在线 wiki 且随 SDK 版本滚动，函数签名的参数名/
默认值/必填性以本机 site-packages 实物为准——本脚本把单文件试点要消费的
关键 API 签名、运行模式常量、订单枚举逐项打印成静态清单，供 Task 2
「gm SDK 源码级 API 核对文档」直接取材，避免文档抄错参数名/漏必填参数
这类"文档漂移"事故。

用法（专用 venv，绝不进生产 .venv310）：
    PYTHONUTF8=1 E:/quanter/.venv_emquant/Scripts/python.exe emquant/tools/gm_api_probe.py
"""

import inspect
import sys
from importlib.metadata import version as _pkg_version

# ── 环境头：必须先于 gm.api 导入打印 ────────────────────────────────────────
# Why：gm.api 装载 C 扩展时会扰动 stdio 缓冲（实测 -c 模式下可吞掉未 flush
# 的输出），故横幅在导入前落盘、全程 print 显式 flush=True，确保输出可见且有序。
print("== gm SDK API 探针 ==", flush=True)
print(f"python  : {sys.version.split()[0]}", flush=True)
print(f"gm      : {_pkg_version('gm')}", flush=True)

import pandas as _pd
import numpy as _np

print(f"pandas  : {_pd.__version__}", flush=True)
print(f"numpy   : {_np.__version__}", flush=True)

import gm.api as gm_api

# ── 1) 关键函数签名 ─────────────────────────────────────────────────────────
# 单文件试点主链路消费的 8 个入口；逐一 getattr 防御——SDK 版本演进中偶有
# 改名/挪位，缺失时打 MISSING 而非抛 AttributeError 崩掉整个探针，保证
# 一次运行产出完整清单（漏一个入口比崩掉更危险，会带病进入 Task 2）。
_FUNCTIONS = [
    "run",           # 事件循环总入口（schedule 注册的回调由它驱动）
    "schedule",      # 定时任务注册（颈线试点 §6 五阶段编排的载体）
    "order_volume",  # 按股数下单（试点唯一下单原语，amount 由策略层算好）
    "order_cancel",  # 撤单（trailing 止损的撤-改链路）
    "get_orders",    # 订单状态查询（生命周期判定：挂单/已成交/已撤/废单）
    "history",       # 历史行情拉取（§2 数据层：颈线合成与均线的原料）
    "subscribe",     # 行情订阅（盘中事件驱动）
    "current",       # 实时行情快照（盘前巡检/价格锚定）
]

for _name in _FUNCTIONS:
    _obj = getattr(gm_api, _name, None)
    if _obj is None:
        print(f"\n[{_name}] MISSING", flush=True)
        continue
    try:
        _sig = inspect.signature(_obj)
    except (TypeError, ValueError):
        # C 扩展直出的内建函数可能无法解析 signature，退回 repr 保清单完整
        _sig = f"<signature 不可解析: {_obj!r}>"
    print(f"\n[{_name}] {_sig}", flush=True)

# ── 2) 运行模式/复权常量 ────────────────────────────────────────────────────
# MODE_* 三态决定 gm 走实盘/仿真/回测通道，试点必须显式钉 MODE_SIMULATION
# 防 run() 默认值把仿真单打到实盘；ADJUST_* 是 history 复权口径——前复权
# (ADJUST_PREV) 必须与生产侧 tushare 前复权口径一致，否则 Task 10 数据
# 对拍会因口径差假阴性。
_CONSTANTS = [
    "MODE_LIVE",
    "MODE_SIMULATION",
    "MODE_BACKTEST",
    "ADJUST_PREV",
    "ADJUST_NONE",
]

print("\n== 常量 ==", flush=True)
for _name in _CONSTANTS:
    print(f"{_name} = {getattr(gm_api, _name, '<MISSING>')!r}", flush=True)

# ── 3) 订单枚举 ─────────────────────────────────────────────────────────────
# OrderSide(买/卖)、OrderType(市价/限价)、PositionEffect(开/平) 是下单
# 三要素的方向字典——成员值必须与柜台约定一致，Task 7 移植下单工具时逐值核对。
_ENUMS = ["OrderSide", "OrderType", "PositionEffect"]

for _name in _ENUMS:
    _obj = getattr(gm_api, _name, None)
    if _obj is None:
        print(f"\n[{_name}] MISSING", flush=True)
        continue
    print(f"\n[{_name}]", flush=True)
    try:
        # Enum 类可遍历成员；若 gm 用普通类/常量组伪装枚举则走 TypeError 兜底
        for _member in _obj:
            print(f"  {_member.name} = {_member.value!r}", flush=True)
    except TypeError:
        print(f"  <非可遍历枚举: {_obj!r}>", flush=True)

# ── 4) gm.api 实物路径 ──────────────────────────────────────────────────────
# Task 2 源码核对的入口锚点：所有签名/常量/枚举的原始定义都从这个包里读。
print(f"\ngm.api.__file__ = {gm_api.__file__}", flush=True)
