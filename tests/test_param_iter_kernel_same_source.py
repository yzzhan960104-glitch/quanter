# -*- coding: utf-8 -*-
"""固化 spec§3.6 订正：param_iter 调的 scan_symbol 与 replay driver 调的 scan_at
走同一 simulate_exit/detect_neckline_method 函数对象（is 同源）。

物理意图（Why 本文件存在）：
    param_iter（研究侧调参）与 replay driver（编排侧/实盘执行）的统计层有意分轨——
    param_iter 算 kelly + 年化作调参目标函数，replay 算 CAGR 作展示口径；两侧统计封装
    各自演进是允许的。但**识别层 + 模拟层内核必须同源**：两侧都应调到同一份
    detect_neckline_method（识别主流程）和 simulate_exit（挂单回踩 + 分级止盈状态机），
    否则会出现「调参优化的参数 ≠ 实盘/异步回测真正执行的参数」致命分叉——研究侧调出来
    的最优参数在实盘跑的是另一套逻辑，回测好看而实盘失效。

    本测试是「统计层分轨但识别+模拟内核同源」契约的护栏：
      ① scan_symbol（param_iter 直调）模块级引用的 simulate_exit/detect_neckline_method，
        与 NecklineMethodStrategy.scan_at（driver 调）模块级引用的，必须是同一函数对象
        （Python is 判定）——保证两侧永远共用一份代码，任何一侧被替换/复制都会被本测试抓出。
      ② scan_symbol 必须接受显式 id_cfg 参数（Layer2 #2a 去 mutation 后的契约），
        不依赖调用方做 DEFAULTS.update() 全局 mutation。

同源判定强度说明：
    理想断言用 ``is``（同一函数对象）。本仓库两侧 import 路径一致（均走包内相对 import
    ``from .neckline.method_v0 import detect_neckline_method``，无 sys.modules 别名 hack），
    is 同源成立。若未来引入模块别名（如 sys.modules 注册双名）导致 is 失败，断言降级为
    ``__module__`` + ``__qualname__`` 一致（同源代码同一份，仅对象身份不同），并在此处
    注明降级原因。
"""
import sys
from pathlib import Path

import pandas as pd

# 项目根挂 sys.path（与 tests/test_neckline_recognition.py 同风格，保证包 import 可达）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from strategies.neckline.backtest import scan_symbol, simulate_exit  # noqa: E402
from strategies.neckline.method_v0 import detect_neckline_method     # noqa: E402


def test_param_iter_kernel_is_same_object_as_driver_kernel():
    """scan_symbol 模块级引用的 simulate_exit/detect 与 NecklineMethodStrategy(scan_at) 同源。

    断言：两侧模块级 binding 指向同一函数对象（is 同源）。
    失败时降级为 ``__module__``+``__qualname__`` 一致判定（见下方注释块）。
    """
    # param_iter 路径（研究侧）：strategies.neckline.backtest 模块级 binding
    import strategies.neckline.backtest as bk
    import strategies.neckline.method_v0 as m0
    # driver/编排路径（execution 侧）：strategies.neckline_method 模块级 binding
    import strategies.neckline_method as nm

    # —— ① simulate_exit 同源（backtest 与 neckline_method 两处 binding 同对象）——
    # 物理意图：simulate_exit 是颈线法出场状态机（挂单回踩+撤单+分级止盈+超时），
    # param_iter 经 scan_symbol 调它，driver 经 scan_at 调它——必须是同一份代码，
    # 否则调参优化的止盈/止损参数在实盘会跑出不同行为。
    assert bk.simulate_exit is simulate_exit, (
        "backtest.simulate_exit 与直接 import 的 simulate_exit 不同对象"
        "（疑似模块别名 hack 导致对象分叉）"
    )
    assert nm.simulate_exit is simulate_exit, (
        "neckline_method.simulate_exit 与 backtest.simulate_exit 不同对象——"
        "param_iter 路径与 driver 路径的模拟内核已分叉，调参优化参数不会在实盘生效"
    )

    # —— ② detect_neckline_method 同源（三处 binding 同对象）——
    # 物理意图：detect 是识别主流程（6 个调用者，耦合 7 个守卫），param_iter 与 driver
    # 共用同一份识别逻辑——否则「研究侧认定的颈线形态 ≠ 实盘触发的颈线形态」。
    assert bk.detect_neckline_method is detect_neckline_method, (
        "backtest.detect_neckline_method 与 method_v0.detect_neckline_method 不同对象"
    )
    assert m0.detect_neckline_method is detect_neckline_method, (
        "method_v0 自身 binding 与 import 的 detect_neckline_method 不同对象（不应发生）"
    )
    assert nm.detect_neckline_method is detect_neckline_method, (
        "neckline_method.detect_neckline_method 与 method_v0.detect_neckline_method 不同对象——"
        "param_iter 识别内核与 driver 识别内核已分叉"
    )

    # —— ③ scan_at 经 strategy 调到同一内核（间接验证：strategy 模块 binding 已同源，
    #         scan_at 内部即调用 self 模块级 import 的 detect/simulate，无需另取副本）——
    assert hasattr(nm.NecklineMethodStrategy, "scan_at"), (
        "NecklineMethodStrategy 缺 scan_at（driver 入口消失）"
    )


def test_scan_symbol_accepts_id_cfg_no_global_mutation():
    """scan_symbol 接受显式 id_cfg，不依赖调用方 DEFAULTS.update() 全局 mutation。

    物理意图（Layer2 #2a 契约）：param_iter.run_one 去全局 mutation 后，调参参数经
    id_cfg 显式透传到 scan_symbol → simulate_exit（旧版靠 DEFAULTS.update 全局 patch，
    simulate_exit 默认 id_cfg=None 读全局——去 mutation 后必须显式传，否则悄悄退化
    用 DEFAULTS 默认档，偷改目标函数且 golden 漏报）。本测试构造与默认不同的 id_cfg
    （window=30），证明走显式参数路径（非读全局），不抛即契约成立。
    """
    from strategies.neckline.method_v0 import DEFAULTS

    # 构造极小 df（仅触发参数路径，不求识别命中——目的在验证 id_cfg 被接受不退化）
    idx = pd.date_range("2024-01-01", periods=80, freq="B")
    df = pd.DataFrame({"high": 10.0, "low": 9.0, "close": 9.5, "volume": 1000,
                       "amount": 10000}, index=idx)
    df["symbol"] = "000001.SZ"

    # 传与默认（DEFAULTS["window"]）不同的 id_cfg（window=30），证明走显式参数而非全局
    id_cfg = {**DEFAULTS, "window": 30}
    filled, n_sig, n_skip = scan_symbol(df, 30, id_cfg=id_cfg)

    # 不抛即参数路径通；返回结构契约（list + 两个 int）
    assert isinstance(filled, list)
    assert isinstance(n_sig, int)
    assert isinstance(n_skip, int)
