# -*- coding: utf-8 -*-
"""§2-§7 执行编排（pilot_body）——单文件的业务躯干（Task 5-8 逐节增补）。

物理定位：
    本文件是【源】而非生成物：build_pilot.py 把它整段拼到组装单文件的 §2-§7 区
    （§0 参数区/§1 识别内核在前）。试点执行编排按任务波次落进来后重跑组装器。

拼接纪律（Why——单文件位次约束，违者 SyntaxError 或 C2/C4 红线事故）：
    - 禁 `from __future__ import ...`：future import 只允许出现在组装文件顶部
      （head 已放），本段拼在模块中部，再出现即 SyntaxError；
    - 禁相对 import（`from .xxx import`）：单文件无包结构，相对 import 必炸；
    - 禁顶层 `import gm`（C4）：gm 只允许函数体内惰性 import——保证无 gm 环境
      可完整 import 单文件跑识别内核等价性测试（本任务阶段天然无 gm，纪律先行）；
    - 只许标准库（C3 同源）：不 import 仓库任何模块——单文件部署到掘金终端后无
      仓库上下文，任何仓库依赖都当场 ImportError。本段 §3/§4 全部 stdlib 实现；
      §2 数据层在 stdlib 之外允许 pandas（§1 内核同依赖，掘金终端 Python 自带），
      gm 只经 `_api()` 惰性 seam 触达（见 §2 头注）。

入口抑制（Task 4 遗留的 __main__ 演示块问题，Task 8 落地）——见下方 hoist 标记块。
"""
# [pilot-hoist:begin] ← 本行到 end 标记之间的块由 build_pilot.py【剪切】到产物 head 区
#（§0 之前）——源里写在 pilot_body 顶部、产物里必须落在 §1 内核逐字块之前，两件事都对：
#
# What：先捕获「是否以脚本形态运行」，是则立刻重绑 __name__，令其后执行的 §1 内核
#       逐字块尾部 `if __name__ == "__main__": main()`（method_v0.py:651 演示入口，
#       读 CSV 全量扫描）判 False 跳过；pilot_body 尾部 `if _IS_MAIN: run_pilot()` 用
#       【捕获值】而非 __name__ 本身，保证入口不被自身的抑制误伤。
# Why 必须先于 §1 执行：Python 顺序执行，§1 的守卫在产物 ~804 行、先于 §2-§7 拼接位
#       求值——抑制若留在 body 原位就是马后炮（演示块已执行并 NameError：method_v0.main
#       引用了其从未 import 的 os）。故 build_pilot._hoist_entrance_guard 把本块剪出到
#       head 区（§0 之前，控制器 progress 注记明示的合法落位），body 原位【剪切非复制】
#       ——复制会让第二次 _IS_MAIN 赋值在已抑制的 __name__ 上重算出 False，尾部入口
#       永久哑火（tests/emquant/test_events_orchestration.py::
#       test_entrance_suppression_hoisted_before_kernel_guard 钉死唯一性与位次）。
# Why C2 合法：本块一个字节不动内核（signal/method_v0 逐字保留），只在产物 head 拼
#       入 pilot_body 源内容；import 形态（pytest 的 spec_from_file_location、掘金终端
#       run() 的 import_module——Task 2 文档 Q9）下 __name__ 恒非 "__main__"，_IS_MAIN
#       = False，整块 no-op——零影响零副作用。脚本形态（runbook 冒烟
#       `python emquant/emquant_neckline_pilot.py`）下走 run_pilot()，其内读
#       config/runtime.json（缺 token 即 raise），全程不触内核演示路径，也无需
#       为演示块补 import os（抑制后该引用永不发生）。
# Why 抑制分支里还要注册 sys.modules 别名：§1 的 Signal 是 dataclass + PEP563 字符串
#       注解，@dataclass 装饰器在类创建期就要 `sys.modules[cls.__module__].__dict__`
#       回查命名空间——重绑 __name__ 后类定义拿到的 __module__ 是
#       "pilot_kernel_suppressed"，sys.modules 里没有这个键 → AttributeError
#       （Task 4 报告 §5 同类坑的脚本形态变体）。别名指向正在运行的 __main__ 模块
#       对象本身（同一命名空间，注解解析结果与不抑制时逐字节一致）；setdefault
#       幂等，且整段只在脚本分支执行——import 形态零触碰。
_IS_MAIN = (__name__ == "__main__")
if _IS_MAIN:
    import sys
    sys.modules.setdefault("pilot_kernel_suppressed", sys.modules["__main__"])
    __name__ = "pilot_kernel_suppressed"
# [pilot-hoist:end]
import bisect
import csv
import json
import os
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd


# ============================ §2 数据层（符号映射 + 定点前复权取数 + 交易日历）============================
# gm seam（C4 红线的数据层落点）：模块级 `_GM` 缓存 + `_api()` 惰性 import——顶层
# `import gm` 会让无 gm 环境（仓库 .venv310 / 识别内核等价测试）连产物都 import 不了。
# 本段所有 gm 调用一律经 `_api()`（或调用方注入 api 实参）；测试侧 monkeypatch 产物
# 模块级 `_GM` 注入 tests/emquant/fake_gm.py 的 FakeGm（签名/12 列 bar/常量按 Task 2
# 核对文档 docs/research/2026-08-21-gm-sdk-api-verified.md 钉死）。
_GM = None


def _api():
    """惰性取 gm.api 模块（首调 import 并缓存进模块级 _GM；None 视为未初始化可重试）。

    Why 缓存写模块属性而非闭包变量：测试注入点必须是【模块属性】——闭包变量
    monkeypatch 不到，`monkeypatch.setattr(产物, "_GM", FakeGm())` 即完成离线替身。
    Why 失败转 RuntimeError 中文诊断而非裸 ImportError 上抛：掘金环境问题（终端
    venv 用错/未装 gm）在诊断文本里给出处置方向，晨检排障省一轮「看堆栈猜原因」；
    RuntimeError 责任在调用方编排层决定停或降级，数据层自身不吞。
    """
    global _GM
    if _GM is None:
        try:
            from gm import api as _gm_api
        except Exception as e:  # ImportError 及 gm 包内任何加载期炸裂（缺 C 扩展/dll）统一收口
            raise RuntimeError(
                "gm SDK 不可用：§2 数据层取数/交易日历依赖掘金终端专用环境（.venv_emquant 内置 gm）。"
                "在仓库环境跑识别内核等价测试不触达本路径属正常；执行编排（run_pilot）必须有 gm。"
            ) from e
        _GM = _gm_api
    return _GM


# ts↔gm 交易所码表：universe 导出侧是创板科创 only 池——【只保留】创业板/科创板
# （300/301/688/689 前缀），北交所与主板均不在池（08-21 勘误：原注「过滤北交所
# （300/301/688/689 前缀）」把保留池误写成剔除集）；这里只见沪深两市；表外后缀
# 一律 fail-loud——见到 .BJ 说明上游过滤被绕过，映射错一只 = 取数/下单打到错误
# 市场，必须在启动期炸给人工（宁停不错）。
_TS_SUFFIX_TO_GM = {".SH": "SHSE", ".SZ": "SZSE"}
_GM_EXCHANGE_TO_TS = {v: k for k, v in _TS_SUFFIX_TO_GM.items()}


def to_gm_symbol(ts: str) -> str:
    """ts 格式 → gm 格式（600000.SH → SHSE.600000；000001.SZ → SZSE.000001）。

    Why 独立映射函数而非裸字符串替换：universe/audit/state 全程 ts 口径（与本地腿
    及 data_lake MultiIndex 同源），只在 gm 调用边界换装——映射集中一处，往返恒等
    可测；gm 侧符号（SHSE.600000）绝不回流 state/audit（对拍两腿各持一套符号纪律）。
    """
    code, dot, suffix = ts.partition(".")
    exchange = _TS_SUFFIX_TO_GM.get(dot + suffix)
    if not code or exchange is None:
        raise ValueError(
            f"无法映射 gm 符号（仅支持沪深 ts 格式如 300750.SZ/688111.SH；universe 是 "
            f"创板科创 only 池——只保留 300/301/688/689，北交所与主板均不在池，见到即 "
            f"上游异变须人工介入）：{ts!r}")
    return f"{exchange}.{code}"


def from_gm_symbol(gm_symbol: str) -> str:
    """gm 格式 → ts 格式（SHSE.600000 → 600000.SH）——逆向映射同样 fail-loud。

    消费场景：gm 侧回执（order/position 的 symbol 键是 gm 格式）落 state/audit 前
    须折回 ts 口径；期货所（CFFEX 等）与北交所不在试点可交易面，映射即炸。
    """
    exchange, dot, code = gm_symbol.partition(".")
    suffix = _GM_EXCHANGE_TO_TS.get(exchange)
    if not dot or suffix is None or not code:
        raise ValueError(
            f"无法映射 ts 符号（仅支持 SHSE./SZSE. 前缀的 A 股 gm 符号）：{gm_symbol!r}")
    return f"{code}{suffix}"


def _audit_warn(type_: str, **fields) -> None:
    """audit WARN 的防炸包装：audit_log 自身失败（磁盘满/目录被锁）时降级 print 不上抛。

    Why：数据层失败路径的契约是「可观测地返 None」——若审计写失败再抛一层异常，
    「降级跳过该标的」会被恶化成「编排整轮崩溃」，双故障叠加时策略停摆而非收缩。
    print 落掘金终端进程 stdout，晨检的 audit CSV 与终端控制台双通道兜底。
    """
    try:
        audit_log("WARN", type=type_, **fields)
    except Exception as e:  # 审计通道自身故障：降级 print，绝不反炸数据层调用方
        print(f"[audit 降级 print] WARN type={type_} {fields}（audit_log 失败：{e!r}）")


# 取数回看自然日窗：200 交易日根数 × ~1.4（周末密度）≈ 280 自然日，再加春节/国庆
# 连休与长期停牌冗余取整 500——同窗内已成交根数 ≥200 的把握充足；即便不足（超长
# 停牌/次新），截断规则是「有多少返多少」，识别内核自带 len<window 守卫返 None，
# 不会拿短数据硬算。Why 不精确到「日历倒数第 N 个交易日再起拉」：那要依赖日历先
# 就位（build_calendar 又依赖取数）——互为前置的环；自然日宽窗一次拉够是日线场景
# 的最简无环解。
_FETCH_LOOKBACK_DAYS = 500


def fetch_df_upto(api, ts_symbol: str, end_date: str):
    """拉单标的截至 end_date 的定点前复权日线 → 识别内核口径 df（OHLCV+零点 DatetimeIndex）。

    与本地腿对齐的口径（对拍命门，tests/emquant/test_data_layer.py 逐参钉死）：
        - 闭区间含 end_date：本地腿 data_ctx.load_df_upto 是 .loc[:date]，T 日盘后
          扫描当日 bar 必须在列；
        - 定点前复权【两端同钉 end_date】：adjust=ADJUST_PREV 且 adjust_end_time=
          end_date。Why 钉 adjust_end_time：缺省 ''（SDK 默认）会让前复权基准漂到
          「服务端眼里的最新」，跨日重取同一 end_date 的历史段数值会因新除权事件
          改变——对拍腿要求的「同日重取逐字节可重复」就此瓦解；钉到 end_date 后
          复权曲面只由 [上市, end_date] 区间的除权事件决定，幂等可重放；
        - skip_suspended=True：停牌日无 bar（本地腿 tushare 行集同构），日线根数=
          实际成交日数，TR/ATR 不被停牌零成交量日稀释；
        - index=DatetimeIndex(eob).normalize()：gm 日线 eob 是 bar 结束时刻（北京
          时间 naive datetime；SDK 文档未钉死日线 eob 的时分秒——可能 15:00:00 也
          可能零点，normalize 把两种可能都折到零点，Task 10 live 对拍再实测收口）；
          本地腿 trade_date 索引
          是零点 naive。折零点后 formed_at/index 与本地腿逐字段可比（对拍口径），
          时区层面 gm 与 tushare 同为北京时间的 naive datetime，无 tz 换算需求；
        - 尾部 2×window+40 根截断：识别窗 window（§0=80）+ ATR（Wilder RMA ~14 期
          收敛）与 local_extrema 掩码的预热冗余 + 停牌跳空后的根数冗余。回测腿吃
          全湖 5 年历史，本函数 200 根是「预热充分前提下的最小带宽」——比回测短
          但识别窗内数值已无预热差异（120 根预热 >> 14 期）。

    33000 服务端上限注记（Task 2 文档 Q6/D17）：单次 history 上限 33000 条（gm
    METADATA v3.0.162，SDK Python 层无此常量）。日线 500 自然日 ≈ 340 根 << 33000，
    单次调用即足【无需分页】；若未来切 60s 频率（≈240 bar/日 → 500 天 12 万条超限），
    须按时间段切片分页（每片 ≤65 交易日）——届时在此扩，不在本版预造。

    失败契约：gm 异常/空结果/列缺失/【末根缺 end_date】→ None + audit WARN（type=
    fetch_fail/fetch_empty/fetch_end_missing，带 symbol/err 详情）——编排层 None-check
    跳过该标的，绝不拿残缺 df 喂识别（识别器没有「数据可能短一截」的守卫义务）。
    api 实参 None → 经 _api() 惰性取（测试 monkeypatch _GM 注入替身的离线路径）。
    """
    a = _api() if api is None else api
    n_roots = 2 * int(ID_PARAMS["window"]) + 40          # §0 快照 window=80 → 200 根
    try:
        start = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=_FETCH_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        raw = a.history(symbol=to_gm_symbol(ts_symbol), frequency="1d",
                        start_time=start, end_time=end_date,
                        fields="eob,open,high,low,close,volume",
                        skip_suspended=True, fill_missing=None,
                        adjust=a.ADJUST_PREV, adjust_end_time=end_date, df=True)
        if raw is None or len(raw) == 0:
            _audit_warn("fetch_empty", symbol=ts_symbol, end_date=end_date, rows=0)
            return None
        out = raw[["open", "high", "low", "close", "volume"]].copy()   # 列缺失 → KeyError 进下方统一收口
        out.index = pd.DatetimeIndex(pd.to_datetime(raw["eob"])).normalize()
        out = out.sort_index()                                        # 升序（ATR/滑窗算子前提）
        out = out.tail(n_roots)                                       # 尾部截断：保最近、弃最老
        # 末根不变量（终审 I-2）：定点取数的契约是【闭区间含 end_date】——末根日期
        # ≠ end_date 说明 history 的 end_time 端性行为异变（服务端把 end 当开区间/时
        # 刻边界截掉当日 bar）。此时识别内核会在「少了最新一根」的序列上算信号：
        # formed_at/颈线全部偏一日且不报错——条件性静默零信号或错位信号，比取数失败
        # 更难察觉。宁可 WARN+None 让编排层跳过（故障可观测），不喂残缺 df。
        if out.index[-1].strftime("%Y-%m-%d") != end_date:
            _audit_warn("fetch_end_missing", symbol=ts_symbol, end_date=end_date,
                        last_bar=out.index[-1].strftime("%Y-%m-%d"), rows=len(out))
            return None
        return out
    except Exception as e:
        # gm 抛错（GmError/断连）与形状异变（列缺失/schema 漂移）统一 WARN+None：
        # 两者同属「取不到合规件数据」，audit 带异常摘要供晨检定位是通道问题还是口径问题
        _audit_warn("fetch_fail", symbol=ts_symbol, end_date=end_date,
                    err=f"{type(e).__name__}: {e}")
        return None


# 交易日历推导源：沪深300 现货指数。Why 指数而非个股：指数每个交易日必有 bar（无
# 停牌/退市概念），eob 序列就是「窗口内真实发生过交易的日集」；Why 不用日历 API
# （get_trading_dates，Q10）：日历与行情是两条服务端口径，多一层转换就多一处两腿
# 分叉面——历从指数 bar 自推，与 fetch_df_upto 的 bar 同源自洽（对拍时历与 K 线
# 不会互相矛盾）。
_CALENDAR_INDEX = "SHSE.000300"


def build_calendar(api, end_date: str, lookback_days: int = 500) -> list:
    """拉 SHSE.000300 日线 eob 序列 → 升序去重 YYYY-MM-DD 交易日历。

    lookback_days 是【自然日】回看窗宽（默认 500：trading_days_between 的消费场景
    是 max_holding≤20/max_wait≤8 量级的日数差，500 自然日 ≈ 340 交易日的历史深度
    富余两个数量级；调用方要更长窗显式传参）。指数无除权事件，adjust 不参与（不
    传即 SDK 默认 None=不复权——对 eob 日期序列零影响）。

    失败契约：gm 异常/空结果 → []（空历）+ audit WARN（cal_fetch_fail）。空历的
    下游语义见 trading_days_between 头注——编排层（Task 8）必须对空历显式降级
    （停扫或保守处置），本层不兜底造历（造一个错的历比没有历更危险）。
    """
    a = _api() if api is None else api
    try:
        start = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=int(lookback_days))).strftime("%Y-%m-%d")
        raw = a.history(symbol=_CALENDAR_INDEX, frequency="1d", start_time=start,
                        end_time=end_date, fields="eob", df=True)
        if raw is None or len(raw) == 0:
            _audit_warn("cal_fetch_empty", end_date=end_date, lookback_days=int(lookback_days))
            return []
        # eob→YYYY-MM-DD 字符串化去重排序：index 每日一根（日线），set 防御性去重
        # 只为把「重复行=服务端异变」折成无害结果（历的成员语义天然是集合）
        return sorted({pd.Timestamp(t).strftime("%Y-%m-%d") for t in raw["eob"]})
    except Exception as e:
        _audit_warn("cal_fetch_fail", end_date=end_date, lookback_days=int(lookback_days),
                    err=f"{type(e).__name__}: {e}")
        return []


def trading_days_between(cal, start, end) -> int:
    """数 (start, end] 区间内的交易日——不含 start、含 end（trading/compute/stop.py:22
    trading_days_between 的单文件可移植重述，口径逐字对齐）。

    Why (start, end]：start 锚 formed_at/entry_date（信号形成/进场当日），end 是
    「今日已到」的判断日——持有计数从进场次一交易日数起、含今日，与本地腿
    max_holding/max_wait/cooldown 的日数差完全同式（跨腿对齐的命门，差一位就
    双腿一进一出的错位平仓）。

    边界（与 stop.py:22-70 逐字同语义）：
        - start/end 缺失或解析失败（含非 str 类型）→ 0（保守视为窗口内，向后兼容）；
        - ed <= sd → 0（同日=零持有、倒序=脏数据）。

    与 stop.py 的【刻意差异】——空历返 0 而非退化自然日：
        stop.py 在 trade_cal 取不到时退化自然日差（保守上界：自然日 ≥ 交易日，
        宁可多判超期早退出）。单文件没有仓库 calendar 模块可依赖（C3），历由调用
        方经 build_calendar 就位并【保证非空】；空历=取数已失败，此时本函数返 0
        意味着「视同未超期」（fail-open，方向与 stop.py 相反）——因此空历绝不能
        静默发生，编排层（Task 8）对 build_calendar 空结果必须显式降级（停扫/人工
        处置），本函数保持纯函数可测、不做隐性兜底（造一个错历比没有历更危险）。

    「end 不在历」补计 +1（终审 I-1）：
        build_calendar 的指数 bar 只到最近【已收盘】日——盘中（pre_open 09:15 /
        on_tick 全日）调用的 end=今日恒不在历上（今日指数 bar 要 15:00 后才存在），
        若只数历成员，max_wait/cooldown/holding_days 会比本地系统性少计一日（本地
        真身 pre_open.py:574 / engine.py:1315 在全量 trade_cal 上当日计入）。修法：
        end > cal[-1] 时把 end 视为交易日补计 1。前提论证：gm 终端 schedule 仅在
        交易日触发事件（非交易日无 pre_open/on_tick 调用），故 end 超出历尾时 end
        必是「已到但未收盘」的交易日；若周末被意外调用，+1 对 max_wait/max_holding
        是更早退出的保守方向，对 cooldown 与本地「end 计入」口径同向漂一日——两
        消费面合计，补计是口径对齐项而非风险项。end 在历内（盘后/回放场景）零影响。
    """
    try:
        sd = datetime.strptime(start, "%Y-%m-%d")   # None/非串/错格式 → TypeError/ValueError
        ed = datetime.strptime(end, "%Y-%m-%d")
    except (ValueError, TypeError):
        return 0                                    # 日期缺失/格式错 → 0（保守窗口内）
    if ed <= sd:
        return 0
    if not cal:
        return 0                                    # 空历=未知（语义见头注，编排层责防）
    # 升序字符串历上的双二分：(start, end] = bisect_right(end) − bisect_right(start)
    # ——YYYY-MM-DD 定长格式字典序即时间序；与 stop.py 的自然日逐日枚举 memberships
    # 数学等价（O(log n)，260 只标的 × 巡检频次下差一个量级的常数）
    n = bisect.bisect_right(cal, end) - bisect.bisect_right(cal, start)
    if end > cal[-1]:
        n += 1                                       # 今日不在历（盘中恒态）→ 补计（头注 I-1）
    return n


# ============================ §3 状态层（state.pkl 原子读写 + 人工风控双值文件）============================
# 物理布局（设计 §3：掘金腿没有 DB/钉钉/台账，state.pkl + 两个人工可编辑文件就是全部持久面）：
#   <BASE_DIR>/state/state.pkl         策略唯一持久状态（schema v1，见 _initial_state）
#   <BASE_DIR>/state/RISK_BLOCK.flag   人工风控开关（存在即拦增量；ADR-16 block_new_orders 的文件化身）
#   <BASE_DIR>/state/CAP.txt           人工仓位上限（ADR-16 max_total_position 的文件化身，缺省 1.0）
#   <BASE_DIR>/audit/audit_YYYYMMDD.csv 对拍审计（本地腿同机直接读，§7 逐行补全事件族）
# Why flag/cap 放 state/ 内：三件都是「运行时真值」，落 gitignore 的 state/ 目录，
# 仓库工作区零污染；人工触达路径（touch/编辑）由 README runbook 钉死。
BASE_DIR = Path(__file__).resolve().parent   # 产物自定位：仓库内跑落 emquant/{state,audit}/，
STATE_DIR = BASE_DIR / "state"               # 部署到掘金终端策略目录时自动落策略文件旁（零配置）
AUDIT_DIR = BASE_DIR / "audit"
CONFIG_DIR = BASE_DIR / "config"             # runtime.json（token/strategy_id/account）——Task 8 读
STATE_FILE = STATE_DIR / "state.pkl"         # 文件名沿用设计文档；内容是 JSON（见 save_state 的 Why）
RISK_BLOCK_FLAG = STATE_DIR / "RISK_BLOCK.flag"
CAP_FILE = STATE_DIR / "CAP.txt"


def _initial_state() -> dict:
    """state.pkl schema v1 的空白态（缺文件/首启时的落点）。

    schema v1（设计 §3，冻结——load_state 见到别的 version 一律拒载）：
        version    int      schema 版本号（未来迁移的哨兵，防静默猜结构）
        scan_done  set[str] 当日已扫描标记（"YYYY-MM-DD"，幂等防重扫——识别本身是纯
                          函数重扫零风险，重扫只产生重复 audit 噪声行）
        placed     dict[str, list[str]] {date: [cl_ord_id, ...]} 当日已挂（幂等防重挂，
                          对齐本地 has_order(OPEN)+UNIQUE 的防重意图，cl_ord_id 是订单主键）
        orders     dict[str, dict] {cl_ord_id: {symbol,date,price,qty,purpose,
                          exec_params,status,...}}（Task 7 生命周期判定消费）
        positions  dict[str, dict] {symbol: {entry_date,entry_price,qty,remaining_qty,
                          stop,tp1_price,tp1_done,tp2_price,trailing:{...},exec_params}}
        last_signal dict[str, str] {symbol: formed_at}（Task 8 追加——cooldown 跨日去重
                          的锚点 map，复刻 engine.py:1036-1050 _eod「最近 cooldown 日已
                          plan 的标的集」；v1 兼容扩展：旧文件缺键 load 时填 {}）
    """
    return {"version": 1, "scan_done": set(), "placed": {}, "orders": {},
            "positions": {}, "last_signal": {}}


def load_state(path=None) -> dict:
    """读 state.pkl → 内存 dict；缺文件返空白态（首启语义）。

    反序列化规则：scan_done 在盘上是 JSON list（set 非 JSON 原生），载入即回转 set；
    placed/orders/positions 为 JSON 原生结构原样返回。

    Why 损坏即抛（fail-loud 而非静默重置）：save_state 的 tmp+rename 原子写已消灭
    「写到一半崩溃」这一唯一常态损坏源——文件仍坏只可能是磁盘损坏或人工误编辑。
    此时静默回空白态会把 orders/positions 一并抹掉 = 持仓裸奔（无止损管理）；
    抛错让策略停在启动期，人工核对柜台实况后处置（对齐本地引擎「宁停不裸奔」
    基调，同 pre_open DB 幂等读失败即 _CriticalHalt 的取舍）。version 不等 1
    或缺 v1 必备键同抛：schema 冻结 v1，未来升版必须显式迁移，绝不静默猜。

    可选 path 参数：默认 STATE_FILE；测试/编排传显式路径实现隔离（不污染真值区）。
    """
    p = Path(path) if path is not None else STATE_FILE
    if not p.exists():
        return _initial_state()
    raw = json.loads(p.read_text(encoding="utf-8"))            # 损坏 → JSONDecodeError 上抛（fail-loud）
    if not isinstance(raw, dict) or raw.get("version") != 1:
        seen = raw.get("version") if isinstance(raw, dict) else type(raw).__name__
        raise RuntimeError(f"state.pkl schema 异变（version={seen!r} != 1，冻结 v1 须显式迁移）：{p}")
    missing = [k for k in ("scan_done", "placed", "orders", "positions") if k not in raw]
    if missing:
        raise RuntimeError(f"state.pkl 缺 schema v1 必备键 {missing}（文件不完整，须人工核对）：{p}")
    raw["scan_done"] = set(raw["scan_done"])                   # 盘上 list → 内存 set（成员测 O(1)，防重语义天然）
    # last_signal（Task 8 追加键）的 v1 兼容扩展：旧文件（Task 5-7 期产物）缺键填默认
    # {} 而非拒载——键值自描述（symbol→formed_at 日期串）、无歧义二义性、旧代码不读
    # 它，填默认零风险；与 version!=1 的 fail-loud 不同类（那是结构不可知，这是结构
    # 可知只是历史缺席）。schema 版本不升：升版会把「加一个可选键」放大成全量迁移。
    raw.setdefault("last_signal", {})
    return raw


def save_state(state: dict, path=None) -> None:
    """state 落盘（tmp+rename 原子写，设计 §3 写入纪律）。

    Why 原子写：进程在写入中途被杀/断电，若直接覆写目标文件，崩溃点落在半途 =
    state 损坏 = 持仓/挂单记忆全丢。先写同目录临时文件、fsync、再 os.replace
    原子改名（同目录保证同一文件系统，rename 不可分割），消灭整类恢复场景。
    replace 失败（断电/被占用）时清理临时文件后原样上抛——不留垃圾 tmp（试点
    目录人工晨检要看，残留 .tmp 会当事故排查半天），也绝不吞错假成功。

    序列化（JSON 而非 pickle，尽管文件名叫 state.pkl）：pickle 对象绑定 Python
    版本/类路径，掘金终端与仓库的 Python 版本不保证一致；JSON 文本人工可检视、
    可跨版本手工修复，且 scan_done 的 set→list 转换显式可控。落盘前浅拷贝转换
    （不改动调用方 state 对象——编排层手里还持有它继续跑当轮）；scan_done 排序
    落盘保证同状态两次落盘字节一致（人工 diff/未来对拍友好）。
    """
    target = Path(path) if path is not None else STATE_FILE
    payload = dict(state)                                      # 浅拷贝：只改写 scan_done 一个键
    payload["scan_done"] = sorted(state.get("scan_done") or ())
    payload.setdefault("version", 1)                           # 兜底：防手工构造的 dict 落出无版本文件
    target.parent.mkdir(parents=True, exist_ok=True)           # 首启目录不存在就地创建（终端冷启动）
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".state.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False))   # 值不可序列化 → TypeError 上抛（写侧免费校验）
            f.flush()
            os.fsync(f.fileno())                               # 先刷盘再改名：封掉「rename 成功而数据仍在页缓存」的断电窗口
        os.replace(tmp_name, target)                           # 原子改名（Windows MoveFileEx REPLACE_EXISTING 同语义）
    except BaseException:
        try:
            os.unlink(tmp_name)                                # 失败清理残 tmp（晨检目录干净）
        except OSError:
            pass                                               # 清理失败无害：tmp 不会被任何读路径触达
        raise


def audit_log(event: str, audit_dir=None, **fields) -> None:
    """对拍审计逐行追加（audit_YYYYMMDD.csv）——§7 审计事件的物理写入原语在此先落位。

    行格式恒定三列：ts,event,detail（detail 为排序 JSON 字段包）。Why 塞 detail
    而非逐字段开列：审计事件族（CAP WARN/信号/挂单/撤单/成交/巡检动作……Task 8
    逐个补）键值不定，三列结构让 Excel 人工复核与文本 grep 两头都好使，新事件
    零迁移。Why CSV 而非 JSONL：设计 §2 降级清单明示「audit CSV 每日人工复核
    （替代钉钉 CRITICAL 链）」——晨检工具是 Excel/文本编辑器，CSV 是最大公约数。
    追加模式（审计行只增不改，完整性优先于整洁；崩溃留半行人工可辨，不做原子写）。
    """
    d = Path(audit_dir) if audit_dir is not None else AUDIT_DIR   # 调用时查模块常量：monkeypatch 即生效
    d.mkdir(parents=True, exist_ok=True)
    # 按日分文件走 _today_str() seam（终审 M-3）：全模块「今日」单一口径——跨午夜
    # 补跑（15:35 盘后跨零点重触发等极端场景）与测试注入（monkeypatch _today_str 钉
    # 日期）都落在同一份按日文件里；直取 date.today() 会在注入态把审计行写进真实
    # 今天的文件，事件日期与文件日期口径分叉。晨检只看当天、复核期自然滚动不变。
    p = d / f"audit_{_today_str().replace('-', '')}.csv"
    with p.open("a", encoding="utf-8", newline="") as f:       # newline=""：csv 模块接管换行（Windows CRLF 可控）
        csv.writer(f).writerow([
            datetime.now().isoformat(timespec="seconds"), event,
            json.dumps(fields, ensure_ascii=False, sort_keys=True, default=str)])


def is_blocked(path=None) -> bool:
    """人工风控开关（ADR-16 block_new_orders 的文件化身）：RISK_BLOCK.flag 存在即 True。

    Why 只看存在性不看内容：开关的触达方式就是人工 touch/rm（回退 SOP 第一步），
    任何内容语义（0/1/off）都给「着急拦单」多加一步出错机会。与本地
    pre_open.py:478 同口径：block 只拦【增量】（挂新买单整体跳过），存量管理
    （撤昨日单/超期平仓/trailing）照常执行——编排层（Task 8 pre_open 阶段④）
    在「扫描后、挂单前」调用本函数，为 True 跳过挂单段而非整个 pre_open。
    """
    p = Path(path) if path is not None else RISK_BLOCK_FLAG
    return p.exists()


def read_cap(path=None) -> float:
    """人工仓位上限（ADR-16 max_total_position 的文件化身）：读 CAP.txt → [0.0, 1.0]。

    两分支刻意不对称（与本地 state_store.py:2016 resolve_risk_control 同口径）：
      - 缺文件 → 1.0（不限制）+ audit WARN：CAP.txt 是可选收紧项，试点默认态就是
        「不设总仓位上限」（另有单日 ≤2 + 单票 ≤5% 两道试点硬闸兜底）——缺文件是
        正常态不是事故，但 WARN 留痕让晨检能发现「以为设了 0.5 其实文件没了」的
        静默失效；
      - 内容非法（非数字/越界 [0,1]/NaN/不可读）→ 0.0（全拦）+ audit WARN：
        人工想收紧却写错（典型：把 50% 写成 50）时，最坏组合是系统静默按 50.0
        放行——fail-closed，宁可误拦人工改文件。与本地「值损坏视同 0 全跳」
        （I-4）逐字同义。
    NaN 防渗透：float('nan') 参与链式比较 0<=v<=1 恒 False，反向写法
    `not (0<=v<=1)` 恰把 nan 归入非法支（正向写法 `v<0 or v>1` 拦不住 nan）。
    """
    p = Path(path) if path is not None else CAP_FILE
    if not p.exists():
        audit_log("WARN", type="cap_missing", msg=f"CAP.txt 缺失，按缺省 1.0（不限制总仓位）运行：{p}")
        return 1.0
    try:
        v = float(p.read_text(encoding="utf-8").strip())       # strip：容忍人工编辑的首尾空白/换行
    except (OSError, ValueError):
        audit_log("WARN", type="cap_invalid", msg=f"CAP.txt 不可读/非数字，视同 0（全拦）fail-closed：{p}")
        return 0.0
    if not (0.0 <= v <= 1.0):                                  # 链式比较把 NaN 也判 False → 非法支
        audit_log("WARN", type="cap_invalid", msg=f"CAP.txt 值 {v!r} 越界 [0,1] 或 NaN，视同 0（全拦）：{p}")
        return 0.0
    return v


# ============================ §4 风控闸（ADR-16 双值 + 试点三硬闸合一预检）============================
def check_caps(sod_state, equity, positions_mv, open_buy_amount, price, qty, today, cap=None):
    """挂单前三闸合一预检（纯函数：不落盘、不查柜台——一切实况由调用方喂入）。

    闸序（任一不过即返 (False, 中文原因)，全过返 (True, "")；原因字符串直接进
    audit，人工晨检靠它定位是哪道闸拦的）：
      ① fail-closed 前置：equity/positions_mv/open_buy_amount 任一为 None，或
         equity<=0，或本单金额<=0 → 拒。上游查询失败（gm get_cash/get_positions
         拉取异常**静默返空**——Task 2 核对结论）必须显式传 None 进来，绝不拿
         0 冒充真值；「不知道占多少时宁可多拦不可盲放」对齐 pre_open.py:508-539
         的三查任一失败全跳。NaN 防渗透：判空判负全用反向写法 not (x>0)，nan
         参与比较恒 False → 自然落拒（正向写法拦不住 nan）。
      ② CAP 总仓位额度：本单金额 ≤ equity×CAP − 持仓市值 − 未终态买单占额。与
         pre_open.py:540 同式（`总权益×比例 − market_value − open_buy`）；其中
         open_buy_amount 口径与 state_store.py:1470 get_open_buy_amount 同义——
         未终态 buy 委托按 (qty−filled)×price 合计（成交部分已体现在持仓市值不
         重复扣；卖单是退出方向不占增量额度）。「逐单扣减」由调用方承担：每挂出
         一单把本单金额累进 open_buy_amount 再喂下一单（对齐 pre_open 循环侧）。
         注记（终审 M-7）：CAP=1.0（缺省不限制）时本闸【仍执行】额度检查——本地
         腿 max_pos=1.0 时直接跳过检查（phases/pre_open.py:510 `if max_pos < 1.0`
         才启用）；pilot 恒查 = 恒紧于本地（quota=equity−mv−ob 仍会拦「超出总权益
         减占额」的单），单票 5% 定尺下该差额几乎不可达，方向保守（多拦不错放）
         可接受，不为对齐而引入「CAP≥1 跳过」的分支面。
      ③ 单日新挂 ≤ PILOT_MAX_NEW_ORDERS_PER_DAY（试点硬闸 FR3，§0 写死 2）：
         当日 placed 已达上限 → 拒（试点期规模闸，验收后可放开）。
      ④ 单票金额 ≤ PILOT_MAX_POSITION_PCT×equity（试点硬闸 FR3，§0 写死 5%）：
         一单一票，单票新增敞口即本单金额；同票次日补挂的聚合敞口由 ② 总闸兜底。
         挂单量公式 qty=⌊equity×pos_cap/entry/100⌋×100 本就按 5% 定尺，本闸是
         对「定尺漂移/人工误用」的二次核验。
    恰等边界一律放行：② 与 ④ 的比较均用严格 >（对齐 pre_open.py:602
    `_order_amt > _pos_quota` 才拦——恰好吃满额度是合法满仓，不是违规）。
    cap 参数：默认 None → 现场读 CAP.txt（read_cap 内嵌 WARN 审计留痕）；测试与
    编排层可显式注入数值，免文件依赖。
    """
    # ① fail-closed 前置：先归一再判（None 守卫在前，防 float(None) 直接 TypeError；
    #    数值脏成非数字串则 float() 抛错上抛——编排层 bug 该炸就炸，不静默吞）
    eq = None if equity is None else float(equity)
    mv = None if positions_mv is None else float(positions_mv)
    ob = None if open_buy_amount is None else float(open_buy_amount)
    amount = float(price) * float(qty)
    if eq is None or mv is None or ob is None or not (eq > 0):
        return False, (f"fail-closed：权益/持仓/挂额查询不完整（equity={equity!r} "
                       f"positions_mv={positions_mv!r} open_buy={open_buy_amount!r}），当日不挂")
    if not (amount > 0):
        return False, f"委托参数残缺（price={price!r} qty={qty!r}，金额非正），拒挂"
    # ② CAP 总仓位额度（pre_open.py:540 同式；open_buy 口径 state_store.py:1470 同义）
    c = read_cap() if cap is None else float(cap)
    quota = eq * c - mv - ob
    if amount > quota:
        return False, (f"总仓位额度不足：本单 {amount:.2f} > 余量 {quota:.2f}"
                       f"（equity×CAP{c:g}−持仓{mv:.2f}−已挂{ob:.2f}）")
    # ③ 单日新挂上限（试点硬闸 FR3）
    placed_today = sod_state.get("placed", {}).get(today, [])
    if len(placed_today) >= PILOT_MAX_NEW_ORDERS_PER_DAY:
        return False, (f"单日新挂已达试点上限 {PILOT_MAX_NEW_ORDERS_PER_DAY}"
                       f"（placed[{today}] 共 {len(placed_today)} 单）")
    # ④ 单票金额上限（试点硬闸 FR3）
    sym_cap = PILOT_MAX_POSITION_PCT * eq
    if amount > sym_cap:
        return False, f"单票金额 {amount:.2f} 超试点上限 {PILOT_MAX_POSITION_PCT:.0%}×equity={sym_cap:.2f}"
    return True, ""


# ============================ §5 订单工具 + trailing 移植 + 生命周期判定 ============================
# 物理定位（设计 §5）：本段是「执行参数 → 柜台动作」的翻译层——place/cancel/sell 是
# gm Q4 契约的薄封装（形态钉死：order_volume 返 List[Dict] 取 [0]、order_cancel 收
# dict），decide_pending/decide_position 是 C9 口径红线的纯判定（本地腿 decide_exit/
# pre_open 同式），absorb_reality 是幂等三查的「以柜台实况修 state」纯逻辑。编排层
# （Task 8 §6）只做时序调度与 audit 留痕，判定与下单的数学全部收口在此。


# ---- 5.0 trailing 止损数学（strategies/neckline/execution.py:47-72 逐句移植）----
def compute_stop_price(
    neckline: float,
    atr: float,
    holding_days: int,
    stop_atr_mult: float,
    grace: int,
    step: float,
    floor: float | None,
) -> float:
    """给定持有天数算当日止损价（颈线基准，trailing 离散）。

    【逐句移植自 strategies/neckline/execution.py:47-72（strangler 红线：函数体零改动，
    7 行纯数学逐字对齐——签名/分支/边界/表达式序任何一处漂移都会被 tests/emquant/
    test_order_lifecycle.py::test_compute_stop_price_golden 的仓库真身对拍当场拦下；
    浮点表达式序一致 ⇒ 位位一致，== 即逐位相等）。】

    物理意图（与 simulate_exit:160-173 完全同源）：
    - grace 天内：用 base_stop（颈线 - stop_atr_mult×ATR，固定，给趋势确认空间）；
    - grace 天后：每日收紧 step×ATR（eff_mult 递减），到 floor 卡底（收紧上限）；
    - grace=0/step=0：退化为固定止损（=base_stop，兼容旧行为）。

    离散化（二期）：盘后对每只持仓调本函数重算【次日】固定止损价；盘中监控用此固定价，
    不移动（符合 spec「盘中不调整」）。回测里是逐根 K 线调；实盘改为每日一次。

    实弹注记：试点快照 EXEC_PARAMS 的 trailing 三件 = grace 0 / step 0.0 / floor 0.0
    → 恒走 base_stop 支（退化为固定止损=本地现状）——这正是双轨一致性要的：掘金腿
    与本地腿在实弹参数下用同一颗退化数学，trailing 活跃分支仅为未来参数演进预置。
    """
    base_stop = neckline - stop_atr_mult * atr
    if grace and step and holding_days > grace:
        eff_mult = stop_atr_mult - (holding_days - grace) * step
        if floor is not None:
            eff_mult = max(eff_mult, floor)
        return neckline - eff_mult * atr
    return base_stop


# ---- 5.1 柜台订单工具（place_limit_buy / cancel / sell_limit，Q4 口径）----
def place_limit_buy(api, ts, price, qty, account):
    """限价买入 → cl_ord_id（下单异常上抛 gm 原生 GmError；空回执返 None）。

    gm 契约（Task 2 文档 Q4，trade.py:115-127）：order_volume 限价最小参数集 =
    side=OrderSide_Buy(1) / order_type=OrderType_Limit(1) / position_effect=
    PositionEffect_Open(1)（A 股现货无开平仓语义但字段必填，官方示例 Open 买）+
    显式 price（默认 0 被柜台拒 IllegalPrice=8）+ 显式 account（C1 可审计：单据
    落到白名单账户，绝不依赖终端侧默认解析）。返回 List[Dict] 取 [0]["cl_ord_id"]
    （D6 通识错名警示——不是单 dict 不是裸 id）。

    qty≤0 防御：负/零量是编程错（上游定尺/风控闸漏了），宁在本地炸也不发给柜台
    造废单占拒单频次；price≤0 不在此拦——对齐柜台拒单路径（IllegalPrice 有审计
    票据，比本地静默改价诚实）。
    """
    if int(qty) <= 0:
        raise ValueError(f"place_limit_buy 拒绝非正量 qty={qty!r}（上游定尺异常，须人工查）")
    a = _api() if api is None else api
    res = a.order_volume(symbol=to_gm_symbol(ts), volume=int(qty),
                         side=a.OrderSide_Buy, order_type=a.OrderType_Limit,
                         position_effect=a.PositionEffect_Open,
                         price=float(price), account=account)
    if not res:
        return None                               # 空回执=柜台没给订单体（异常形态，调用方降级）
    first = res[0]
    cid = first.get("cl_ord_id") if isinstance(first, dict) else None
    return cid or None


def sell_limit(api, ts, price, qty, account):
    """限价卖出 → cl_ord_id（side=Sell(2)+position_effect=Close(2)，Q4 官方示例口径）。

    与 place_limit_buy 同构的薄封装；卖出量防御同理由（qty≤0 本地炸——卖错数量是
    致命方向，宁可炸给编排层降级也不发废单）。
    """
    if int(qty) <= 0:
        raise ValueError(f"sell_limit 拒绝非正量 qty={qty!r}（卖出量异常，须人工查）")
    a = _api() if api is None else api
    res = a.order_volume(symbol=to_gm_symbol(ts), volume=int(qty),
                         side=a.OrderSide_Sell, order_type=a.OrderType_Limit,
                         position_effect=a.PositionEffect_Close,
                         price=float(price), account=account)
    if not res:
        return None
    first = res[0]
    cid = first.get("cl_ord_id") if isinstance(first, dict) else None
    return cid or None


def cancel(api, cl_ord_id, account=""):
    """撤单（order_cancel 收 {cl_ord_id, account_id} 两键 dict——Q4/D7，不是裸字符串）。

    Why 签名比 brief 多 account 形参：gm 契约的撤单字典必须有 account_id 键；缺省
    "" 透传后端由单账户解析（与 order_volume 的 account 语义同源），Task 8 编排层
    显式传 PILOT_ACCOUNT_ID 保 C1 可审计。撤单语义（先撤后挂）归编排层：本函数只
    发起不确认——撤单是否到终态以下一轮 absorb_reality 的柜台实况为准（对齐本地
    M2「撤单发起后确认终态」的验收口径，但试点用对账轮询替代实时确认回调）。
    """
    a = _api() if api is None else api
    a.order_cancel({"cl_ord_id": cl_ord_id, "account_id": account})


# ---- 5.2 跌停价（自算档位 + get_history_symbol API 值优先）----
def _limit_rate(symbol: str) -> float:
    """按 ts 代码前缀取涨跌幅档位（创板科创 20% / 主板 10%——data_ctx._load_universe
    同口径的 300/301/688/689 判别）。

    ST 5% 档无法从代码判别（ST 标记在证券名不在代码里）——本函数职责边界到此，
    ST 场景的正解是 fetch_limit_down 走 API 值（lower_limit 由数据服务按真实板位/
    ST 状态给出，天然覆盖）。
    """
    code = symbol.partition(".")[0]
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    return 0.10


def limit_down_price(prev_close, symbol) -> float:
    """自算跌停价 = round(prev_close × (1 − 档位), 2)（二位取整到分）。

    universe 是创板科创 only → 实弹路径恒 round(prev_close×0.80, 2)；主板档保留
    口径（快照池已滤但公式留全，防未来 universe 演进时口径漂移）。round 取 Python
    内建（银行家舍入）——对 x×0.8/x×0.9 的二进制浮点表示，与交易所四舍五入在
    「恰好半分」边界极罕见处可能差 1 分，此类边界以 fetch_limit_down 的 API 值
    为准（API 值优先正是为此兜底）。
    """
    return round(float(prev_close) * (1.0 - _limit_rate(symbol)), 2)


def fetch_limit_down(api, ts_symbol, end_date=None, prev_date=None):
    """取当日跌停价：get_history_symbol 的 lower_limit 优先（Q7 权威），失败/缺字段
    逐级回退——同行 pre_close 自算 → prev_date（T-1）收盘价自算——API 值优先。

    Why API 优先：柜台/数据服务知道真实板位与 ST 状态（自算只认代码前缀），且
    除权日 pre_close 口径由服务端钉死；Why 保留自算回退：查询失败（断连/字段缺）
    不能让跌停定价整体失效——自算档位在创板科创 universe 内与 API 值几乎恒等。

    三级回退链（终审 I-3）：
        ① API 值：lower_limit 有效正数即采信；
        ② 同行 pre_close 自算（与 lower_limit 同源同日，口径自洽）；
        ③ prev_date 收盘价自算：get_history_symbol 整体失败（异常/空行/双字段缺，
           典型形态=盘前调用时【当日】证券信息行尚未生成）时，用 T-1 日线末根
           close 走 limit_down_price（预置的 20% 自算）。prev_date 由调用方传入
           （历归编排层所有：pre_open ② 传 t_minus_1）；不传则本层无从定 T-1
           （自然日减一在周末/节假日错位），维持 None 放弃。Why 取 T-1 而非
           「end_date 前一自然日」：跌停价=昨收×(1−档位)，除权日外 T-1 收盘就是
           真值昨收；fetch_df_upto 的末根不变量（I-2）同时保证末根恰是 prev_date
           当日。回退触发即 WARN 留痕（limit_down_fallback_t1）——API 通道降级
           须进晨检面。

    失败契约：三级全败 → None + audit WARN（type=limit_down_fetch_fail /
    limit_down_unavailable）——调用方（Task 8）对 None 显式降级（跳过依赖跌停价
    的动作），绝不造一个错价顶上（错的跌停价 = 卖单挂错价位或风控误判）。
    """
    a = _api() if api is None else api
    day = end_date or f"{date.today():%Y-%m-%d}"  # 未传即当日（调用方编排层显式传日保可测）
    row = None
    try:
        rows = a.get_history_symbol(symbol=to_gm_symbol(ts_symbol),
                                    start_date=day, end_date=day, df=False)
        row = rows[-1] if rows else None          # 单日窗取末行（服务端返回序不假设）
        if row is None:
            _audit_warn("limit_down_fetch_fail", symbol=ts_symbol, end_date=day,
                        err="空返回（无该日证券信息行）")
    except Exception as e:
        _audit_warn("limit_down_fetch_fail", symbol=ts_symbol, end_date=day,
                    err=f"{type(e).__name__}: {e}")
        row = None                                # 异常不提前 return：③ 的 T-1 回退仍可救
    if row is not None:
        # API 值优先：lower_limit 是有效正数（防 None/0/NaN——NaN 参与比较恒 False 被
        # 反向写法拦下）即采信
        ll = row.get("lower_limit")
        try:
            ll = float(ll)
        except (TypeError, ValueError):
            ll = None
        if ll is not None and ll == ll and ll > 0:
            return ll
        # 回退②：昨收用同行 pre_close（与 lower_limit 同源同日，口径自洽）
        pc = row.get("pre_close")
        try:
            pc = float(pc)
        except (TypeError, ValueError):
            pc = None
        if pc is not None and pc == pc and pc > 0:
            return limit_down_price(pc, ts_symbol)
        _audit_warn("limit_down_unavailable", symbol=ts_symbol, end_date=day,
                    err=f"lower_limit 与 pre_close 均缺（row 键：{sorted(row)}）")
    # 回退③（I-3）：API 路径整体失败 → T-1 收盘价自算（prev_date 由编排层喂入）
    if prev_date is not None:
        df = fetch_df_upto(a, ts_symbol, prev_date)   # None/WARN 自治（含 I-2 末根不变量）
        if df is not None:
            px = float(df["close"].iloc[-1])
            est = limit_down_price(px, ts_symbol)
            _audit_warn("limit_down_fallback_t1", symbol=ts_symbol, end_date=day,
                        prev_date=prev_date, prev_close=px, price=est)
            return est
    return None


# ---- 5.3 生命周期判定（decide_pending / decide_position，C9 口径红线）----
def decide_pending(tick_price, order, today, cal):
    """挂单等待期撤单判定（纯函数）→ "cancel_on" / "max_wait" / "chase" / None（不撤）。

    C9 口径（评审必查，两判据对齐本地腿）：
      - cancel_on 触价：tick_price ≥ order["cancel_on"]（含等——decide_exit pending
        分支 simulate_exit:130 `high >= cancel_on` 同式；None=不配阈值放飞所有回踩）；
      - max_wait 过期：trading_days_between(cal, formed_at, today) **> max_wait**
        （严格大于；formed_at 起算——backtest 挂单窗 range(signal_idx+1, min(signal_idx+
        max_wait,...)+1) 的「窗口内含第 max_wait 个交易日」边界语义，恰好 == 不撤。
        锚点是真身的 signal_idx（backtest.py:177-179，信号日起算）——不是 buy_idx：
        buy_idx 是窗口内【成交日】，窗口边界不以成交日起算）；
      - **chase（R6-10 C 线② · 2026-08-25）**：超期且 exec_params.chase_entry=True →
        返 "chase" 交 on_tick 追入（现价守卫 tp2 + 撤旧单后追新单——回测
        simulate_exit chase 语义的实盘兑现；守卫与撤旧逻辑在 on_tick 编排层，
        本纯函数只判「该追」）。
    两因并发归因 cancel_on（价格事件盘中即时，max_wait 是窗口边界——对齐 decide_exit
    pending 分支的判序：窗口内逐根先判 cancel_on，窗口边界只是循环外限）。

    order 契约（§3 schema v1 的 orders 值）：必含 cancel_on（可 None）、formed_at
    （信号形成日，max_wait 锚点——与 date（挂单日）语义不同： formed_at 才是策略
    语义上的等待起点）、exec_params.max_wait（信号定终身快照）。formed_at 缺失/
    非法 → trading_days_between 容错返 0 → 视为未超期（宁等一日不误撤）。
    """
    cancel_on = order.get("cancel_on")
    if cancel_on is not None and float(tick_price) >= float(cancel_on):
        return "cancel_on"
    max_wait = (order.get("exec_params") or {}).get("max_wait")
    if max_wait is not None and trading_days_between(cal, order.get("formed_at"), today) > int(max_wait):
        if (order.get("exec_params") or {}).get("chase_entry"):
            return "chase"   # R6-10：超期追入判定（守卫/撤旧/挂新在 on_tick 编排层）
        return "max_wait"
    return None


def decide_position(tick_price, pos, today, cal):
    """持仓离场判定（纯函数）→ ("sell", qty, reason) / None（持有）；reason ∈
    {stop_loss, tp2, tp2_share, tp1, tp2_dust, tp2_eod_sweep}。

    C9 口径（优先序对齐本地 decide_exit，strategies/neckline/execution.py:249-294）：
      ⓪ force_exit（盘后 sweep 标记，见 after_close 的 tp1_eod_sweep_marked）→
        全量跟 tick 市价出（reason=tp2_eod_sweep——回测「lot1 随 tp2 同价平仓」的
        次日首 tick 近似，隔夜跳空风险入对照报告 known_divergence 台账）；
      ① stop 触价（tick ≤ 当日止损价，含等——priority 1 :249-259 硬风控先于止盈，
        防日内闪崩穿底后反弹的假象）→ 卖 remaining 全量；
      ② tp2 触价（tick ≥ tp2_price，含等——priority 2 :269-276）：
        - 正常 regime（tp1 ≤ tp2，历史全档）：清仓全量（=decide_exit priority 2）；
        - **反转 regime（tp1_price > tp2_price，R6-6 冠军形态 tp1=2H > tp2=1.5H）**：
          只卖 lot2 份额 floor(remaining×(1−tp1_portion)/100)×100（reason=tp2_share，
          置 tp2_done 一档一次）；不足一手 → ("sell", 0, "tp2_dust")——份额沉 lot1
          不落单（known_divergence=tp2_dust_sinks）。对齐 R6-4 幽灵修复后的回测
          语义：首触 tp2 当日 lot1 若摸到 tp1 则按 tp1 成交（由 ③ 的 tick 序自然
          承接——同一冲高日 1.5H 先触 2H 后触），未摸到则随 tp2 同价平（tick 腿
          无法当日收盘卖 → 盘后 force_exit 次日出，④⓪ 链）。
      ③ tp1 触价（tick ≥ tp1_price 且 not tp1_done——priority 3 :287-294，tp1_done
        即本地 lot1_open=False 对齐 simulate_exit:191 的一档一次）：
        - 正常 regime：卖 portion 档一次：qty = floor(remaining×tp1_portion/100)×100
          （trading/phases/exit.py:190 同式向下整手）；不足一手（floor=0）→ 本档卖
          全部剩余（known_divergence=tp1_dust_clears）。
        - 反转 regime：卖全部剩余（lot1 即剩余——tp1 在此是 lot1 的强势日目标位）。
      ④ 均未触发 → None。

    当日止损价来源（两级）：pos["trailing"] 的 neckline/atr 在场（非 None）即走
    compute_stop_price 活口径（holding_days = trading_days_between(cal, entry_date,
    today)，与回测 i−buy_idx 同式：进场日=0——此处 buy_idx 是成交日，语义正确）；
    余参缺省退化——stop_atr_mult→1.0 / grace→0 / step→0.0 / floor→None（实弹
    六键齐的定终身快照之外，残键按此默认补齐而非弃走活口径）；neckline/atr 缺 →
    回退 pos["stop"]（盘后预算的当日固定价——execution docstring 离散化口径的
    兜底）。实弹快照（grace 0/step 0.0）下两路径恒等（=base_stop）。

    pos 契约（§3 schema v1.2 的 positions 值）：remaining_qty / tp1_price / tp1_done /
    tp2_price / tp2_done（反转 regime 的 lot2 一档一次锚）/ force_exit（盘后 sweep
    次日出场标记）/ trailing{...} / exec_params.tp1_portion / entry_date。
    remaining_qty ≤0 → None（无仓可卖，防裸调炸 KeyError）。
    """
    remaining = int(pos.get("remaining_qty") or 0)
    if remaining <= 0:
        return None
    px = float(tick_price)
    # ⓪ 盘后 sweep 残仓：强制出场（全量，跟 tick 价）
    if pos.get("force_exit"):
        return ("sell", remaining, "tp2_eod_sweep")
    # ① 当日止损价：trailing 快照齐 → compute_stop_price（5.0 移植真身）
    tr = pos.get("trailing") or {}
    if tr.get("neckline") is not None and tr.get("atr") is not None:
        holding_days = trading_days_between(cal, pos.get("entry_date"), today)
        stop = compute_stop_price(
            neckline=float(tr["neckline"]), atr=float(tr["atr"]),
            holding_days=holding_days,
            stop_atr_mult=float(tr.get("stop_atr_mult", 1.0)),
            grace=int(tr.get("grace") or 0),
            step=float(tr.get("step") or 0.0),
            floor=tr.get("floor"))
    else:
        stop = pos.get("stop")                    # 盘后预算固定价兜底（离散化口径）
    # ② priority 1：止损（硬风控，全平剩余）
    if stop is not None and px <= float(stop):
        return ("sell", remaining, "stop_loss")
    tp2 = pos.get("tp2_price")
    tp1 = pos.get("tp1_price")
    inverted = (tp1 is not None and tp2 is not None
                and float(tp1) > float(tp2))       # R6-6 反转形态（tp1 挂 tp2 之上）
    # ③ priority 2：tp2
    if tp2 is not None and px >= float(tp2):
        if not inverted:
            return ("sell", remaining, "tp2")      # 正常 regime：全平（历史口径）
        if not pos.get("tp2_done"):
            portion = float((pos.get("exec_params") or {}).get("tp1_portion") or 0.0)
            # epsilon 防 (1−portion) 浮点下溢截断（1.0−0.9=0.0999…→int(0.999…)=0
            # 把整手份额截没——1000 股×10% 应得 100 股而非 dust；测试实锤）
            qty = int(remaining * (1.0 - portion) / 100 + 1e-9) * 100   # lot2 份额（向下整手）
            if qty <= 0:
                return ("sell", 0, "tp2_dust")     # 不足一手：份额沉 lot1（置位不落单）
            return ("sell", qty, "tp2_share")
        # tp2_done 已置（lot2 已出）→ 落到 ③ 判 lot1
    # ④ priority 3：tp1
    if tp1 is not None and not pos.get("tp1_done") and px >= float(tp1):
        if inverted:
            return ("sell", remaining, "tp1")      # 反转 regime：lot1 即剩余，全卖
        portion = float((pos.get("exec_params") or {}).get("tp1_portion") or 0.0)
        qty = int(remaining * portion / 100) * 100  # exit.py:190 同式（向下整手）
        if qty <= 0:
            qty = remaining                           # 不足 100 股 → 本档卖全部剩余
        return ("sell", qty, "tp1")
    return None


# ---- 5.4 柜台对账（absorb_reality 幂等三查）----
# gm 订单状态 int → 本地 OrderState 词汇（trading/types/order_state.py:42-49 八态；
# Task 2 文档 Q4 enum.py:23-37 权威值）。映射取舍（Why——错杀比错留危险）：
#   - 未知/罕见码（4 DoneForDay、9 Suspended、11/13/14 等）一律保守映射【非终态】：
#     把活单标成终态 = state 不再管理它 = 真单裸奔；把死单标成活态只是多管一轮，
#     下一轮对账自愈。方向性不对称决定保守侧。
#   - 7 Stopped → FAILED（终态：停止=不再工作，归异常桶供人工复核）；
#     12 Expired → CANCELLED（终态未成交，与已撤同处置）。
_GM_STATUS_TO_LOCAL = {
    0: "PENDING",            # Unknown 未知（保守非终态）
    1: "SUBMITTED",          # New 已报
    2: "PARTIAL_FILLED",     # PartiallyFilled 部成
    3: "FILLED",             # Filled 已成
    4: "SUBMITTED",          # DoneForDay（保守非终态：当日终结次日可续）
    5: "CANCELLED",          # Canceled 已撤
    6: "SUBMITTED",          # PendingCancel 待撤（撤未生效，仍挂柜台——非终态）
    7: "FAILED",             # Stopped 停止（终态，异常桶）
    8: "REJECTED",           # Rejected 已拒绝
    9: "SUBMITTED",          # Suspended 挂起（保守非终态）
    10: "PENDING",           # PendingNew 待报
    11: "SUBMITTED",         # Calculated（保守非终态）
    12: "CANCELLED",         # Expired 已过期（终态未成交）
    13: "SUBMITTED",         # AcceptedForBidding（保守非终态）
    14: "SUBMITTED",         # PendingReplace（保守非终态）
}

# 终态集（state_store.py:69 死态集 REJECTED/FAILED/CANCELLED/PARTIAL_CANCELLED + FILLED
# 成功终态）：FILLED 不算死态（不可重挂）也不算活态（不可撤）——对「state 有柜台无」
# 判定而言它与死态同侧：next 交易日 get_orders 不再返昨日单，若误标 CANCELLED 会
# 把已成交单的语义抹掉（成交→撤？持仓对账将失锚），故 FILLED 必须豁免改写。
_TERMINAL_ORDER_STATES = frozenset(
    {"FILLED", "CANCELLED", "PARTIAL_CANCELLED", "REJECTED", "FAILED"})


def _gm_status_to_local(status):
    """gm int 状态 → 本地 OrderState 字符串（未知值保守落 SUBMITTED 非终态）。"""
    try:
        return _GM_STATUS_TO_LOCAL.get(int(status), "SUBMITTED")
    except (TypeError, ValueError):
        return "SUBMITTED"                        # None/脏值：保守非终态（错留可自愈）


def _date_str_of(value):
    """gm created_at（datetime）→ YYYY-MM-DD；非 datetime/缺失 → None（不强塑）。"""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    return None


def absorb_reality(state, api_orders, api_positions):
    """柜台↔state 对账（纯逻辑：不落盘、不 audit——diff 留痕归 Task 8 编排层）。

    幂等三查（C9 红线——「以柜台实况修 state」，同参数重复调用结果稳定）：
      ① 订单正向：柜台有 state 无 → 吸收（PENDING/部成照单收编；成交部分转持仓，
         exec_params={}——柜台没有信号信息，不伪造快照）；
      ② 订单反向：state 有柜台无且【非终态】→ 标记 CANCELLED（视为已撤/未挂——
         get_orders 只返日内单，昨日单属正常缺席，终态单绝不能被改写）；
      ③ 持仓双向：qty 以柜台为准修 state（remaining_qty=柜台 volume；qty 保留
         max(建仓量, volume) 作历史峰值），entry/exec_params 以 state 为准保留
         （exec_params 是信号定终身的快照，柜台没有这个信息）；柜台有 state 无 →
         吸收（entry 用柜台 vwap，exec_params={} 留人工补）；state 有柜台无 →
         remaining_qty 归零（柜台已无此仓；entry/exec_params 档案保留供复盘）。

    成交→持仓转换增量口径：filled_volume 相对 state 已记 filled 的增量 Δ 才转仓
    （重复对账 Δ=0 不双计——幂等的实现核心）；entry_date 用 state 订单的 date
    （挂单日是 state 内最接近成交日的真值，成交精确时刻柜台在 updated_at、试点
    不为此加依赖）。买向加仓 / 卖向减仓双向处理（tp1 减仓卖单的柜台视角）。

    api_orders/api_positions 形态：gm get_orders()/get_position() 返回项（Q4/Q5：
    cl_ord_id/symbol(gm 格式)/side/status(int)/volume/filled_volume/created_at/
    vwap）。symbol 经 from_gm_symbol 折 ts——不可映射（非沪深 A 股 gm 符号）时
    **WARN 留痕 + 跳过**（2026-08-25 实锤修正：仿真账户持北交所人工仓 920679，
    gm 持仓返回裸代码无交易所前缀，原 fail-loud 把整条 init 链炸死——策略无法
    启动管理自己的仓位。与 on_tick 的 M-6 终审降级同型同语义：试点账户白名单
    语义由 C1 账户闸承担，symbol 级遇到试点外品种（北交所/人工仓/异变注入）
    只降级留痕（order/position_symbol_unmappable），策略对其不吸收不管理，
    人工仓人工管）。
    """
    # ── ①② 订单对账（先正向吸收/同步，再反向补撤——顺序保证同轮内先见实况再定性）──
    api_order_ids = set()
    for ao in (api_orders or []):
        oid = (ao or {}).get("cl_ord_id")
        if not oid:
            continue                              # 无主键行（异变）：跳过不炸，audit 归 Task 8
        try:
            sym = from_gm_symbol(ao["symbol"])    # gm → ts（不可映射 → WARN+skip，见 docstring）
        except Exception as e:
            audit_log("WARN", type="order_symbol_unmappable",
                      symbol=(ao or {}).get("symbol"),
                      err=f"{type(e).__name__}: {e}")
            continue
        api_order_ids.add(oid)
        filled = int(ao.get("filled_volume") or 0)
        st_o = state["orders"].get(oid)
        if st_o is None:
            # 柜台有 state 无 → 吸收（未成交 PENDING/部成照收；成交转仓在下方增量段）
            st_o = {"symbol": sym, "date": _date_str_of(ao.get("created_at")),
                    "price": ao.get("price"), "qty": int(ao.get("volume") or 0),
                    "purpose": "UNKNOWN", "cancel_on": None, "formed_at": None,
                    "exec_params": {}, "filled": 0,
                    "status": _gm_status_to_local(ao.get("status")),
                    "account": ao.get("account_id")}
            state["orders"][oid] = st_o
        else:
            # 双向在场 → 状态/量价以柜台为准（status 覆盖 + qty/price 刷新）
            st_o["status"] = _gm_status_to_local(ao.get("status"))
            st_o["qty"] = int(ao.get("volume") or 0)
            st_o["price"] = ao.get("price")
        prev_filled = int(st_o.get("filled") or 0)
        st_o["filled"] = filled
        if filled > prev_filled:
            delta = filled - prev_filled          # 增量转仓（幂等核心：重放 Δ=0）
            if ao.get("side") == 2:               # OrderSide_Sell：卖向成交 → 减持仓
                pos = state["positions"].get(sym)
                if pos is not None:
                    pos["remaining_qty"] = max(0, int(pos.get("remaining_qty") or 0) - delta)
            else:                                 # 买向成交 → 持仓转换/累加
                pos = state["positions"].get(sym)
                vwap = ao.get("filled_vwap") or ao.get("price")
                if pos is None:
                    state["positions"][sym] = {
                        "entry_date": st_o.get("date"), "entry_price": vwap,
                        "qty": delta, "remaining_qty": delta, "stop": None,
                        "tp1_price": None, "tp1_done": False, "tp2_price": None,
                        "tp2_done": False, "force_exit": False,
                        "trailing": {},
                        "exec_params": dict(st_o.get("exec_params") or {})}
                else:
                    pos["qty"] = int(pos.get("qty") or 0) + delta
                    pos["remaining_qty"] = int(pos.get("remaining_qty") or 0) + delta
    for oid, st_o in state["orders"].items():
        if oid not in api_order_ids and st_o.get("status") not in _TERMINAL_ORDER_STATES:
            st_o["status"] = "CANCELLED"          # state 有柜台无且非终态 → 已撤/未挂
    # ── ③ 持仓对账（qty 柜台为准 / entry+exec_params state 保留 / 双向吸收归零）──
    api_pos_syms = set()
    for ap in (api_positions or []):
        try:
            sym = from_gm_symbol(ap["symbol"])    # 不可映射（北交所人工仓等）→ WARN+skip
        except Exception as e:
            audit_log("WARN", type="position_symbol_unmappable",
                      symbol=(ap or {}).get("symbol"),
                      err=f"{type(e).__name__}: {e}")
            continue
        api_pos_syms.add(sym)
        volume = int(ap.get("volume") or 0)
        st_p = state["positions"].get(sym)
        if st_p is None:
            # 柜台有 state 无 → 吸收（人工仓/丢档仓）：entry 用柜台 vwap，
            # exec_params/trailing 留空不伪造（信号信息柜台没有——晨检人工补）
            state["positions"][sym] = {
                "entry_date": None, "entry_price": ap.get("vwap"),
                "qty": volume, "remaining_qty": volume, "stop": None,
                "tp1_price": None, "tp1_done": False, "tp2_price": None,
                "tp2_done": False, "force_exit": False,
                "trailing": {}, "exec_params": {}}
        else:
            # 双向在场：数量柜台真值修 state（entry/exec_params/tp1_done 等档案保留）
            st_p["remaining_qty"] = volume
            if volume > int(st_p.get("qty") or 0):
                st_p["qty"] = volume              # 柜台加仓（人工/多单）：建仓量峰值上调
    for sym, st_p in state["positions"].items():
        if sym not in api_pos_syms and int(st_p.get("remaining_qty") or 0) > 0:
            st_p["remaining_qty"] = 0             # 柜台已无此仓 → 剩余归零（档案保留）
    return state


# ============================ §6 事件编排（盘前五阶段 / 盘中 tick 巡检 / 盘后收尾）============================
# 物理定位（设计 §6）：本段把 §2-§5 的全部接口接成可运行的事件闭环——PilotRuntime 是
# 编排核心（一个实例管一个交易日的 state/audit/日历缓存/订阅面），模块级 init/
# pre_open_job/on_tick/after_close_job 是 gm 回调入口（Q3：模块级全局函数名 getattr
# 约定，run() 里逐一抓取——不是装饰器不是 run 参数）。判定与下单的数学全部收口在
# §5 纯函数，本段只做时序调度、audit 留痕与降级决策。
#
# 日期口径：全段走 _today_str()（本地机器日期=终端机器=北京时间），不用 context.now
# （gm Context 的该属性未入 Task 2 核对文档的已验证面——seam 留 _today_str 供测试
# 注入与未来切换，不押未核实的 SDK 行为）。
#
# audit 写失败纪律（Task 5 minor ③ 对账）：一切审计经 self._audit（try/except 接住→
# print 降级→继续）——观测通道损失不拦交易（磁盘满不该让止损单挂不出去）；read_cap
# 内嵌 WARN 的同类风险由 _read_cap_resilient 承接（见其头注）。数据层 _audit_warn
# （§2）同哲学，Task 6 已落。
#
# 对账节流：on_tick 高频触发（订阅标的每 ~3s 一 tick），柜台对账（get_orders+
# get_position 两次本机 HTTP）按 _ABSORB_THROTTLE_SECONDS 节流——成交转持仓的时延
# 窗口 ≤ 节流阈值（30s 量级对试点止损管理足够；首跳必对账保证启动即真值）。
_ABSORB_THROTTLE_SECONDS = 30.0
# 对账失败退避（M-4）：_last_absorb 只在成功时前移，若只有上面的节流闸，柜台故障期
# 每根 tick 都要打满两次本机 HTTP 查询（对故障中的柜台/终端雪上加霜，且注定失败）。
# 失败后 _ABSORB_FAILURE_BACKOFF_SECONDS 窗内 on_tick 不再重试，巡检判定继续吃上一份
# state 真值；窗口到期由下一根 tick 自然承接重试。60s 取「节流窗翻倍」量级——闪断
# 故障一窗即过，长故障也不会把成交转持仓的时延拖出分钟级（试点止损管理可容忍上界）。
# 注意退避只拦 on_tick 热路径：bootstrap/pre_open 的 reconcile 不受影响（每日一次的
# 全量对账值得无条件重试，不在退避语义内）。
_ABSORB_FAILURE_BACKOFF_SECONDS = 60.0


def _today_str() -> str:
    """当日 YYYY-MM-DD（本地机器日期=终端北京时间口径；测试 monkeypatch 本函数钉日期）。"""
    return f"{date.today():%Y-%m-%d}"


def _prev_trading_day(cal, today):
    """cal（升序 YYYY-MM-DD 列表）中今日前一根 T-1——超期/扫描的基准日（C9 红线）。

    今日不在历中也按「< today 的最大者」取（节假日/周末补跑场景：T-1 语义仍是最近
    一根已收盘交易日）；无前根（历空/今日是历首）→ None（调用方显式降级，见 pre_open）。
    """
    i = bisect.bisect_left(cal, today)
    return cal[i - 1] if i > 0 else None


def _read_runtime_config(config_path=None) -> dict:
    """读 config/runtime.json（token/strategy_id/account_id 三键）——缺文件/缺 token 即 raise。

    Why fail-loud 而非静默空跑：token 缺位时 gm run() 会在连接期才炸（或更坏——连上
    了匿名态），「启动期就指出按 README 填写」比「盘中炸出半跑状态」便宜一个数量级。
    绝不硬编码 token（C8）：值只活在 runtime.json（gitignore），测试用 'test-token'。
    """
    p = Path(config_path) if config_path is not None else CONFIG_DIR / "runtime.json"
    if not p.exists():
        raise RuntimeError(
            f"缺少 {p}（token/strategy_id/account_id）——按 README runbook 创建后再启动试点")
    cfg = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict) or not str(cfg.get("token") or "").strip():
        raise RuntimeError(f"runtime.json 缺 token（{p}）——按 README runbook 填写后重启")
    return cfg


def _c1_guard(account_id) -> None:
    """C1 仿真守卫（08-21 修订版·账户白名单制）：account 必须 == PILOT_ACCOUNT_ID。

    Why 白名单而非模式常量：gm 无 MODE_SIMULATION（Task 2 文档 Q1/D1——终端「仿真
    交易」= MODE_LIVE 绑仿真柜台账户，SDK Python 层查不到账户类型标记），唯一可自动
    化的防线是「绑定的是我们指定的那个账户」。env PILOT_ALLOW_LIVE=I_KNOW_REAL_MONEY
    是唯一逃生门（知情实盘，字符串本身即确认动作）。
    """
    if account_id == PILOT_ACCOUNT_ID:
        return
    if os.environ.get("PILOT_ALLOW_LIVE") == "I_KNOW_REAL_MONEY":
        return
    raise RuntimeError(
        f"C1 仿真守卫拒绝：account_id={account_id!r} 不在试点仿真账户白名单"
        f"（PILOT_ACCOUNT_ID={PILOT_ACCOUNT_ID}）。掘金终端仿真交易与实盘在 SDK 层同为 "
        f"MODE_LIVE，绑错账户=真金白银。确认要跑实盘请设 env PILOT_ALLOW_LIVE="
        f"I_KNOW_REAL_MONEY（知情确认）；否则改 runtime.json 的 account_id 为仿真账户。")


def _open_buy_amount(state):
    """未终态买单占额合计（state_store.py:1470 get_open_buy_amount 的 pilot 同义）。

    口径：(qty−filled)×price 逐单合计——成交部分已体现在持仓市值不重复扣，卖单是
    退出方向不占增量额度；UNKNOWN（柜台吸收、方向未知）保守计入（多估占额 → 多拦
    一单，方向性不对称下错拦可自愈、错放不可逆）。任一单 price/qty 残缺无法计值 →
    None（fail-closed：「不知道占多少就盲放」是最坏组合，对齐 check_caps ①）。
    """
    total = 0.0
    for o in (state.get("orders") or {}).values():
        if o.get("status") in _TERMINAL_ORDER_STATES:
            continue
        if o.get("purpose") not in ("OPEN", "UNKNOWN"):
            continue
        try:
            total += (int(o["qty"]) - int(o.get("filled") or 0)) * float(o["price"])
        except (TypeError, ValueError, KeyError):
            return None
    return total


class PilotRuntime:
    """试点编排核心：五阶段 pre_open / tick 巡检 on_tick / 盘后 after_close / 对账 reconcile。

    构造（api, workdir=None）：api=None → 生产（_a() 经 _api() 惰性取 gm.api）；测试
    直注 FakeGm 离线。workdir=None → 生产布局（BASE_DIR 下 state/audit/config 三目录
    就地）；显式 workdir → 三目录整体重定向（测试隔离：state/audit/runtime.json 全落
    tmp，不污染仓库真值区）。
    """

    def __init__(self, api, workdir=None):
        self.api = api
        root = Path(workdir) if workdir is not None else BASE_DIR
        self.state_file = root / "state" / "state.pkl"
        self.audit_dir = root / "audit"
        self.risk_flag = root / "state" / "RISK_BLOCK.flag"
        self.cap_file = root / "state" / "CAP.txt"
        self.config_file = root / "config" / "runtime.json"
        self.state = load_state(path=self.state_file)
        # bootstrap（gm init 回调）从 runtime.json 覆写；构造期缺省白名单账户——
        # 测试/直构路径无 config 也能跑（C1 真守卫在 run_pilot/bootstrap 双点执行）
        self.account = PILOT_ACCOUNT_ID
        self.strategy_id = ""
        self._cal = None            # 日历缓存（按日失效：跨日首用重建）
        self._cal_date = None
        self._subscribed = []       # 当日巡检订阅面（ts 口径；「已 subscribe 未 unsubscribe」的唯一账本，after_close 清）
        self._last_absorb = None    # 上次柜台对账【成功】的 time.time()（on_tick 节流锚；失败不前移）
        self._absorb_retry_after = None  # 对账失败退避到期时刻（M-4：失败时=now+退避窗；成功复位 None）

    # ---------------------------------------------------------- 小工具
    def _a(self):
        """gm api seam：显式注入优先（测试），否则 _api() 惰性取（生产，含中文诊断）。"""
        return self.api if self.api is not None else _api()

    def _audit(self, event, **fields):
        """audit 写失败接住（Task 5 minor ③ 对账）：print 降级 + 继续——观测通道损失
        绝不拦交易（磁盘满时止损单照挂；print 落终端 stdout 双通道兜底，晨检仍可见）。"""
        try:
            audit_log(event, audit_dir=self.audit_dir, **fields)
        except Exception as e:  # 审计通道自身故障：降级 print，不上抛编排层
            print(f"[audit 降级 print] event={event} {fields}（audit_log 失败：{e!r}）")

    def _read_cap_resilient(self) -> float:
        """read_cap 的防炸包装：CAP.txt 分支语义在审计不可用时保持不变。

        两分支刻意不对称（read_cap 自身口径的忠实延伸）：
          - 文件缺（正常态，缺省 1.0 不限制）：WARN 写失败 → 降级 print 后仍返 1.0
            ——失败源是审计通道而非 CAP 语义，观测降级不改变交易口径；
          - 文件在场（值合法原样返；值非法应 fail-closed 0.0）：WARN 写失败 → 返 0.0
            ——无法区分「值合法」与「值非法但审计先炸」，保守全拦（误拦人工改文件，
            误放=真金白银，方向性不对称同 §4）。
        """
        if not self.cap_file.exists():
            try:
                return read_cap(path=self.cap_file)          # 正常路径：返 1.0 + cap_missing WARN
            except Exception as e:
                print(f"[audit 降级 print] CAP.txt 缺失分支的 WARN 写失败，按缺省 1.0 继续：{e!r}")
                return 1.0
        try:
            return read_cap(path=self.cap_file)
        except Exception as e:
            print(f"[audit 降级 print] CAP.txt 值判定阶段 WARN 写失败，保守视同 0（全拦）：{e!r}")
            return 0.0

    def _calendar(self, today):
        """日历按日缓存：pre_open 建一次、on_tick 复用（per-tick 拉指数日线不可接受）。

        空历 [] 也缓存——当日已定性降级（audit calendar_missing 已留痕），重试留给
        次日（_cal_date 变化自然重建），不给 tick 热路径塞隐藏的 gm 调用。
        """
        if self._cal_date != today or self._cal is None:
            self._cal = build_calendar(self._a(), today)
            self._cal_date = today
        return self._cal

    def _has_open_sell(self, sym) -> bool:
        """该标的是否有在途（非终态）卖出委托——重复卖出防重的依据。

        Why 必须有：卖出挂出后 state 的 remaining_qty 要等下一轮对账吸收成交才减——
        无此守卫，同价第二根 tick 会再挂一张同量卖单（双倍卖出=裸露空头方向错误）。
        """
        for o in self.state["orders"].values():
            if (o.get("symbol") == sym
                    and o.get("purpose") in ("EXPIRE", "STOP_LOSS", "TP1", "TP2")
                    and o.get("status") not in _TERMINAL_ORDER_STATES):
                return True
        return False

    def _query_equity(self):
        """总权益（get_cash().nav，Q5：nav=市值+余额的柜台总权益口径）→ float | None。

        空表/异常/非正数 → None（Q1/Q5 权威结论：查询失败静默返空——空=故障；编排层
        显式转 None 触发 check_caps ① fail-closed 当日不挂，绝不拿 0 冒充真值）。
        """
        try:
            cash = self._a().get_cash(self.account)
        except Exception as e:
            self._audit("WARN", type="get_cash_fail", err=f"{type(e).__name__}: {e}")
            return None
        if not cash:                                           # {} = 空表 = 故障
            self._audit("WARN", type="get_cash_empty", account=self.account)
            return None
        nav = cash.get("nav")
        try:
            nav = float(nav)
        except (TypeError, ValueError):
            nav = None
        if nav is None or not (nav > 0):                       # 反向写法拦 NaN
            self._audit("WARN", type="get_cash_invalid", nav=nav)
            return None
        return nav

    def _query_positions_mv(self):
        """持仓市值合计（get_position 逐行 market_value，缺则 vwap×volume 兜底）→ float | None。

        与 _query_equity 的刻意不对称（Why）：get_position 返 [] 是【正常空仓态】——
        试点账户首日天然空仓，把 [] 当故障会让 placement 永久死锁（day-1 deadlock）；
        查询失败的主形态是异常上抛（check_gm_status 抛 GmError，Task 2 文档 Q8），
        异常 → None fail-closed。空表=故障的权威警示（Q1）针对的是 context.accounts
        缓存路径（_get_account_info 静默 return），此处的直查 API 以异常为主故障面。
        """
        try:
            plist = self._a().get_position(self.account)
        except Exception as e:
            self._audit("WARN", type="get_position_fail", err=f"{type(e).__name__}: {e}")
            return None
        if plist is None:
            return None
        total = 0.0
        for p in plist:
            mv = (p or {}).get("market_value")
            if mv is None:
                mv = (p.get("volume") or 0) * (p.get("price") or p.get("vwap") or 0)
            try:
                total += float(mv)
            except (TypeError, ValueError):
                self._audit("WARN", type="positions_mv_invalid", symbol=p.get("symbol"))
                return None
        return total

    def _cancel_and_sync(self, oid, sym, stage, reason):
        """撤单 + audit + state 同步落终态（撤单发起即视为不再挂——终态确认以下一轮
        对账为准，此处同步只为让同轮后续额度计算不把已撤单继续占额）。"""
        try:
            cancel(self._a(), oid, self.account)
        except Exception as e:
            self._audit("WARN", type=f"{stage}_cancel_fail", cl_ord_id=oid, symbol=sym,
                        err=f"{type(e).__name__}: {e}")
            return
        o = self.state["orders"].get(oid)
        if o is not None and o.get("status") not in _TERMINAL_ORDER_STATES:
            o["status"] = "CANCELLED"
        self._audit("CANCEL", cl_ord_id=oid, symbol=sym, stage=stage, reason=reason)

    # ---------------------------------------------------------- 事件：init 链
    def bootstrap(self, context):
        """gm init(context) 的编排体：读配置 → C1 复核 → 对账 → 订阅 → 注册双定时。

        C1 双保险说明：run_pilot（入口）已拦一次；此处再拦覆盖「终端/人工绕过
        run_pilot 直接驱动模块回调」的路径——守卫成本一次字符串比较，裸奔成本无上限。
        """
        cfg = _read_runtime_config(self.config_file)           # 缺 token → raise（中文指向 README）
        _c1_guard(cfg.get("account_id"))
        self.account = cfg.get("account_id") or PILOT_ACCOUNT_ID
        self.strategy_id = cfg.get("strategy_id") or ""
        # context.accounts 核验（终审 M-1，Task 2 文档 Q1 权威）：_set_accounts 拉取
        # 失败是【静默 return 空表】——空表不是「无账户正常态」而是故障；白名单账户
        # 不在场 = 终端绑定异变（策略↔账户绑定由终端 tradegw 按 strategy_id 维护，
        # SDK 层查不到账户类型标记，唯一可自动化的验证就是「指定的那个账户确实在场」
        # ——C1 配置侧守卫的运行态对侧证据）。两形态均 WARN 留痕（中文、可处置）
        # 不 raise：init 链炸掉终端侧只看到策略退出、audit 连 INIT 行都没有，观测面
        # 反而更差；WARN 进晨检清单（README 三）由人工核对终端绑定后处置。
        accts = getattr(context, "accounts", None) or {}
        if not accts:
            self._audit("WARN", type="bootstrap_accounts_empty", account=self.account,
                        msg="context.accounts 为空：账户表拉取失败或终端未绑定账户"
                            "（Q1：静默空表=故障非正常态），后续柜台查询将不可用——"
                            "须人工核对终端账户绑定后重启策略")
        elif self.account not in accts:
            self._audit("WARN", type="bootstrap_account_absent", account=self.account,
                        msg=f"白名单账户 {self.account} 不在 context.accounts"
                            f"（在场：{sorted(accts)}）：终端绑定与下单账户不一致，"
                            f"须人工核对策略的账户绑定")
        self.reconcile(context)
        self._subscribe_watchlist()
        a = self._a()
        # 定时注册（Q2：schedule(schedule_func, date_rule, time_rule)，回调只收 context；
        # 实时模式仅 1d 可靠）。09:15:00 盘前时刻：源码与官方文档均无禁令（time_rule 是
        # 纯时钟时刻，无时段校验代码），机制上应可触发——但属 Task 2 文档 §五残余
        # 不确定清单第 1 条，live 首夜须验证 09:15 是否如约触发（不触发则五阶段
        # 整体后移到首个可触发时刻，届时回评）。
        a.schedule(pre_open_job, "1d", "09:15:00")
        a.schedule(after_close_job, "1d", "15:35:00")
        # build_stamp：§0 注入的版本锚（build_pilot 生成时取 git HEAD 提交时间+短
        # hash）——globals().get 防御：pilot_body 单独 exec（无 §0 段）时降级
        # "unknown"，晨检对 INIT 行即可核对版本，不用开编辑器搜文件头。
        self._audit("INIT", account=self.account, strategy_id=self.strategy_id,
                    subscribed=len(self._subscribed),
                    build_stamp=globals().get("PILOT_BUILD_STAMP", "unknown"))

    def _subscribe_watchlist(self):
        """订阅巡检标的：持仓 ∪ 未终态挂单（ts→gm 符号，tick 频率）——增量差集下发。

        Why 这个集合：tick 巡检只为「pending 撤单判定 + positions 离场判定」供价——
        无仓无挂的标的订阅是纯带宽浪费。

        订阅面三时点生命周期（C-1 评审修复——每时点各答一个「谁在什么场景补订阅」，
        缺任何一层都有一段巡检失明窗口）：
          1. bootstrap（init 一次）：进程崩溃/部署重启后，state 在途单与持仓的巡检
             面由此恢复——新会话 gm 侧订阅从零开始，此层覆盖「盘中重启」场景；
          2. pre_open ⓪后重建（见 pre_open 内调用点）：前日 after_close 清空订阅
             （省带宽）后，常驻进程 day 2+ 若无人重建，当日持仓/在途单的止损/止盈/
             cancel_on 巡检整体失明——pre_open 是常驻进程每日必经的第一事件，在
             此按 ⓪ 对账后的最新 state 重建；
          3. 阶段⑤后同日增补：当日新挂的买/卖当日就要被 tick 巡检管理（新买单的
             cancel_on 触价撤单、成交转仓后的止损止盈），不等次日 pre_open。

        幂等口径：gm subscribe 对已订阅标的是否幂等无权威结论（Task 2 核对文档未
        覆盖）——统一按 self._subscribed（「已 subscribe 未 unsubscribe」的唯一账本）
        取差集，差集空不下发，任意时点重复调用零副作用；after_close 的清退也以
        同一账本为准，不漏清不错清。
        """
        syms = set()
        for sym, pos in self.state["positions"].items():
            if int(pos.get("remaining_qty") or 0) > 0:
                syms.add(sym)
        for o in self.state["orders"].values():
            if o.get("status") not in _TERMINAL_ORDER_STATES and o.get("symbol"):
                syms.add(o["symbol"])
        self._subscribe_incremental(syms)

    def _subscribe_incremental(self, syms):
        """增量订阅：差集去重后下发 subscribe（已订阅标的零重复——幂等性不赌 SDK，
        去重后调用对「幂等/非幂等」两种 SDK 行为都安全）。

        容错（Task 8 评审遗留 Minor，Task 9 收口）：subscribe 抛错 → WARN 留痕降级
        继续，不上抛。Why：本函数的两处调用点（pre_open ⓪' 重建 / ⑤' 增补）都钉在
        五阶段主干上，行情通道瞬时异常（断连/限流/GmError）若直接炸出会中止整个
        pre_open——①撤昨日单②超期平仓⑤挂单等【交易通道】动作全部陪跳（行情断≠
        交易断的双通道现实组合，谁断谁降级，不许单通道故障劫持另一通道）。失败时
        订阅账本刻意不动（账本只在 subscribe 成功后并入），下次调用自然重试全差集
        ——与既有「失败不吞真值、窗口到期重试」语义一致（同 reconcile 退避口径）。
        代价自觉：订阅失败窗口内 tick 巡检对相关标的断供（无价不判定），比「整段
        pre_open 蒸发」可控——WARN 行进当日晨检面。
        """
        pending = sorted(set(syms) - set(self._subscribed))
        if not pending:
            return
        try:
            self._a().subscribe([to_gm_symbol(s) for s in pending], frequency="tick")
        except Exception as e:
            self._audit("WARN", type="subscribe_fail", symbols=pending,
                        err=f"{type(e).__name__}: {e}")
            return                                          # 账本不动：下次重试全差集
        self._subscribed = sorted(set(self._subscribed) | set(pending))

    # ---------------------------------------------------------- 事件：对账
    def reconcile(self, context=None):
        """柜台↔state 幂等三查入口（absorb_reality 的调度壳 + 持仓富化 + 落盘）。

        调用点：init（启动全量）/ pre_open 前置（隔夜成交撤单先落 state，五阶段吃
        最新真值）/ on_tick 节流（盘中成交 30s 窗口内转持仓）。查询失败（异常）→
        本轮不对账（state 不动）+ WARN 留痕 + 布置失败退避锚（M-4：on_tick 在
        _ABSORB_FAILURE_BACKOFF_SECONDS 窗内不再重试——故障期不每 tick 打两次注定
        失败的查询）——绝不拿空表当「柜台已清空」去修 state。
        """
        a = self._a()
        try:
            api_orders = a.get_orders()
        except Exception as e:
            self._audit("WARN", type="reconcile_orders_fail", err=f"{type(e).__name__}: {e}")
            self._absorb_retry_after = time.time() + _ABSORB_FAILURE_BACKOFF_SECONDS
            return
        try:
            api_positions = a.get_position(self.account)
        except Exception as e:
            self._audit("WARN", type="reconcile_positions_fail", err=f"{type(e).__name__}: {e}")
            self._absorb_retry_after = time.time() + _ABSORB_FAILURE_BACKOFF_SECONDS
            return
        absorb_reality(self.state, api_orders, api_positions)
        self._enrich_positions_from_orders()
        self._audit("RECONCILE", orders=len(self.state["orders"]),
                    positions=len(self.state["positions"]))
        save_state(self.state, path=self.state_file)
        self._last_absorb = time.time()
        self._absorb_retry_after = None                   # 成功复位退避（不残留死退避）

    def _enrich_positions_from_orders(self):
        """成交持仓的止损/止盈富化：按来源 OPEN 订单的信号几何补 trailing/stop/tp1/tp2。

        Why 必须有：absorb_reality 转仓只拷量价与 exec_params（柜台没有信号几何），
        trailing{}/stop=None 的持仓在 decide_position 里走不了活口径也触发不了止盈——
        无人富化=成交仓裸奔。幂等性：stop/tp1 已在（非 None）即跳过，重复对账零副作用。
        价位公式（§1 _detect_core_window ④ 同源）：stop=颈线−stop_atr_mult×ATR
        （compute_stop_price holding_days=0 退化式）、tp1=颈线+tp1_h_mult×H、
        tp2=颈线+tp_h_mult×H，H=颈线−谷底。
        """
        for sym, pos in self.state["positions"].items():
            if int(pos.get("remaining_qty") or 0) <= 0:
                continue
            if pos.get("stop") is not None or pos.get("tp1_price") is not None:
                continue                                      # 已富化（幂等闸）
            origin = None
            for o in self.state["orders"].values():           # dict 插入序：后挂的 OPEN 覆盖先挂
                if (o.get("symbol") == sym and o.get("purpose") == "OPEN"
                        and o.get("neckline") is not None and o.get("atr") is not None):
                    origin = o
            if origin is None:
                continue                                      # 人工仓/丢档仓：无几何，晨检人工补
            try:
                neckline = float(origin["neckline"])
                atr = float(origin["atr"])
                bottom = float(origin.get("bottom") or neckline)
            except (TypeError, ValueError):
                self._audit("WARN", type="enrich_geometry_invalid", symbol=sym)
                continue
            h_geom = neckline - bottom
            if not (h_geom > 0):                              # 反向写法拦 NaN/非正深度
                self._audit("WARN", type="enrich_geometry_invalid", symbol=sym,
                            neckline=neckline, bottom=bottom)
                continue
            ep = origin.get("exec_params") or {}
            stop_mult = float(ep.get("stop_atr_mult", 1.0))
            grace = int(ep.get("trailing_grace") or 0)
            step = float(ep.get("trailing_step") or 0.0)
            floor = ep.get("trailing_floor")
            pos["trailing"] = {"neckline": neckline, "atr": atr, "stop_atr_mult": stop_mult,
                               "grace": grace, "step": step, "floor": floor}
            # 盘后预算固定价兜底（decide_position 的 pos["stop"] 回退路径）；当日活口径
            # 由 trailing 六件套在 decide_position 内重算（holding_days 实时）
            pos["stop"] = compute_stop_price(neckline, atr, 0, stop_mult, grace, step, floor)
            # R6-10 B3 对称（C 线④）：H/ATR 超阈值时 tp2/tp1 乘数 ×scale——与
            # price_levels.compute_price_levels 同式（价位单源语义：深形态锚缩近）。
            _tp1m = float(ep.get("tp1_h_mult", 1.0))
            _tp2m = float(ep.get("tp_h_mult", 2.0))
            _thr = ep.get("tp_adapt_h_atr")
            if _thr is not None and atr > 0 and (h_geom / atr) > float(_thr):
                _sc = float(ep.get("tp_adapt_scale", 0.5))
                _tp1m *= _sc
                _tp2m *= _sc
            pos["tp1_price"] = neckline + _tp1m * h_geom
            pos["tp2_price"] = neckline + _tp2m * h_geom
            self._audit("POS_ENRICHED", symbol=sym, stop=pos["stop"],
                        tp1_price=pos["tp1_price"], tp2_price=pos["tp2_price"])

    # ---------------------------------------------------------- 事件：盘前五阶段
    def pre_open(self, context):
        """盘前五阶段（红线序，顺序不可换——每阶段动作独立 audit 留痕）：

            ⓪（前置对账，非五阶段之一）absorb_reality：隔夜成交/撤单先落 state；
            ⓪' 订阅重建（C-1 三时点之二）：前日 after_close 清空的 tick 订阅面按
               最新 state 恢复——常驻进程 day 2+ 的巡检不断供；
            ① 撤【昨日】非终态买（get_orders 柜台实况驱动，audit 逐单；柜台单
               created_at 日期==今日 → 跳过——同日重跑不自杀当日进场，I-2）；
            ② 超期平仓：trading_days_between(cal, entry_date, T-1) > max_holding
               （严格大于；T-1=cal 中今日前一根，C9 基准日红线）→ fetch_limit_down
               （API 值优先）挂跌停价卖；
            ③ 扫描：UNIVERSE 逐 fetch_df_upto(end=T-1) → detect_signal → cooldown
               跨日去重（last_signal 锚点）→ SIGNAL 行落 audit（scan_done 幂等防重扫）；
            ④ 闸序：is_blocked → 跳过挂单段（存量管理 ①② 已跑完）；
            ⑤ 挂限价买：entry=Signal.entry_price（=颈线+buy_limit_atr_mult×ATR，§1
               装配式单源）、qty=⌊equity×pos_cap/entry/100⌋×100（pos_cap 取
               TRADE_CFG，equity 经 get_cash，空表/异常→None→当日不挂）；
            ⑤' 订阅增补（C-1 三时点之三）：当日新挂（②超期卖/⑤买）即入 tick 巡检面
               ——增量差集下发，已订阅标的零重复。

        空历降级（Task 6→8 必记）：T-1 取不到（build_calendar 返 []/今日是历首）→
        audit calendar_missing，②③ 停判（超期/扫描都依赖 T-1 或日数差，挂单依赖扫描
        产物自然为零）——①是柜台实况驱动无历依赖，照跑。不炸不静默。
        """
        a = self._a()
        today = _today_str()
        st = self.state

        # ── ⓪ 前置对账：五阶段全部吃最新真值（不动五阶段相对序——对账在所有阶段之前）──
        self.reconcile(context)

        # ── ⓪' 订阅重建（C-1 三时点之二）：前日 after_close 已清空订阅（省带宽），
        #    常驻进程 day 2+ 的持仓/在途单若无人重建订阅，当日 tick 巡检（止损/止盈/
        #    cancel_on）整体失明——按 ⓪ 对账后的最新 state 重建。崩溃重启场景由
        #    bootstrap 承担（三时点全图见 _subscribe_watchlist 头注）；增量差集下发，
        #    同日重复触发 pre_open 时已订阅标的零重复。──
        self._subscribe_watchlist()

        # ── ① 撤【昨日】非终态买（get_orders 无参返日内全部委托，Q4；只撤买——卖是退出方向）──
        try:
            api_orders = a.get_orders()
        except Exception as e:
            api_orders = []
            self._audit("WARN", type="pre_open_get_orders_fail", err=f"{type(e).__name__}: {e}")
        for ao in api_orders or []:
            oid = (ao or {}).get("cl_ord_id")
            if not oid or ao.get("side") != 1:                # OrderSide_Buy=1
                continue
            if _gm_status_to_local(ao.get("status")) in _TERMINAL_ORDER_STATES:
                continue                                      # 已成/已撤/已拒不再碰
            # 「昨日单」判据（I-2）：柜台 created_at 日期为准——state 可能滞后（⑤ 挂单
            # 后进程崩溃未落盘、或 ⓪ 对账查询失败致 state 未吸收该单，两种场景下只有
            # 柜台知道它是今日单）；柜台字段缺失（created_at 非 datetime/无值）再退回
            # state 侧 orders[oid].date。Why 必须有此守卫：同日二次触发 pre_open（人工
            # 补跑/catchup）时 scan_done 防重扫使 ⑤ 不会重挂——撤了当日单=当日进场
            # 静默丢失；当日单的退出交给 on_tick 巡检（cancel_on/max_wait/成交后止损）。
            placed_day = _date_str_of(ao.get("created_at")) \
                or (st["orders"].get(oid) or {}).get("date")
            if placed_day == today:
                continue                                      # 当日单：不自杀
            try:
                sym = from_gm_symbol(ao["symbol"])            # gm→ts（fail-loud 见 §5 头注）
            except ValueError as e:
                self._audit("WARN", type="pre_open_symbol_unmappable",
                            cl_ord_id=oid, symbol=ao.get("symbol"), err=str(e))
                continue
            self._cancel_and_sync(oid, sym, "pre_open", "撤昨日非终态买单")

        # ── 日历与 T-1（②③ 的基准日）──
        cal = self._calendar(today)
        t_minus_1 = _prev_trading_day(cal, today)
        if t_minus_1 is None:
            self._audit("WARN", type="calendar_missing", today=today,
                        msg="build_calendar 空/无 T-1：①照跑；②超期与③扫描停判"
                            "（max_wait/cooldown 同停）；挂单依赖扫描产物自然为零")

        # ── ② 超期平仓（T-1 基准；恰等 max_holding 不平——与 decide_pending max_wait
        #    同款「窗口内含第 max_holding 日」边界语义，双腿一进一出不错位）──
        if t_minus_1 is not None:
            for sym in sorted(st["positions"]):
                pos = st["positions"][sym]
                if int(pos.get("remaining_qty") or 0) <= 0:
                    continue
                if self._has_open_sell(sym):
                    continue                                  # 在途卖单未终结：不重复挂
                entry_date = pos.get("entry_date")
                if not entry_date:
                    continue                                  # 人工吸收仓无锚：留人工处置
                ep = pos.get("exec_params") or {}
                mh = int(ep["max_holding"]) if ep.get("max_holding") is not None \
                    else int(EXEC_PARAMS["max_holding"])      # 信号定终身快照优先（快照实弹 20）
                holding = trading_days_between(cal, entry_date, t_minus_1)
                if not (holding > mh):
                    continue
                # 跌停价三级回退链（§5.2，终审 I-3）：API 值 → 同行 pre_close 自算
                # → T-1 收盘自算（prev_date 喂 t_minus_1——盘前查当日证券信息行失败
                # 的兜底，防「缺一行=整日放弃超期平仓」）
                ld = fetch_limit_down(a, sym, end_date=today, prev_date=t_minus_1)
                if ld is None:
                    self._audit("WARN", type="expire_skip_no_limit_down", symbol=sym,
                                entry_date=entry_date, holding_days=holding,
                                msg="跌停价不可得，今日放弃超期平仓（不造错价顶上）")
                    continue
                qty = int(pos["remaining_qty"])
                try:
                    cid = sell_limit(a, sym, ld, qty, self.account)
                except Exception as e:                         # GmError/断连：该标的放弃，不炸整段（后续标的照平）
                    self._audit("WARN", type="expire_sell_fail", symbol=sym,
                                err=f"{type(e).__name__}: {e}")
                    continue
                if cid is None:
                    self._audit("WARN", type="expire_sell_empty_receipt", symbol=sym)
                    continue
                st["orders"][cid] = {"symbol": sym, "date": today, "price": ld, "qty": qty,
                                     "purpose": "EXPIRE", "cancel_on": None,
                                     "formed_at": None, "exec_params": {},
                                     "status": "SUBMITTED", "filled": 0,
                                     "account": self.account}
                self._audit("EXPIRE_SELL", symbol=sym, entry_date=entry_date,
                            holding_days=holding, max_holding=mh, price=ld,
                            qty=qty, cl_ord_id=cid)

        # ── ② 后防御性落盘（M-5）：超期卖单此刻已上柜台，若 ③④⑤ 中途异常炸出而
        #    state 只等函数尾统一落盘，「柜台有单、state 无单」的窗口敞开——重启后
        #    reconcile 固能吸收柜台单自愈，但吸收窗口内 _has_open_sell 失守会再挂一张
        #    同量卖单（双倍卖出=致命方向错误）。state 先落把窗口压到零。──
        if t_minus_1 is not None:
            save_state(st, path=self.state_file)

        # ── ③ 扫描（scan_done 幂等防重扫：识别是纯函数重扫零风险，重扫只产重复噪声行）──
        signals = []
        if t_minus_1 is not None and today not in st["scan_done"]:
            cooldown = int(EXEC_PARAMS["cooldown"])
            for sym in UNIVERSE:
                # 单标的挡板（engine.py:1034 _eod scan_live 同款）：识别内核对脏数据
                # 抛错只损失该标的当日信号，不炸扫描环（scan_done/已收信号必须落袋）
                try:
                    df = fetch_df_upto(a, sym, t_minus_1)     # None → WARN 已留痕，跳过该标的
                    if df is None:
                        continue
                    sig = detect_signal(sym, df, ID_PARAMS, EXEC_PARAMS, t_minus_1)
                except Exception as e:
                    self._audit("WARN", type="scan_fail", symbol=sym,
                                err=f"{type(e).__name__}: {e}")
                    continue
                if sig is None:
                    continue
                # cooldown 跨日去重（engine.py:1036-1050 复刻）：最近 cooldown 交易日内
                # 已产出过信号的标的丢弃新信号（防同形态连续触发连续挂单）
                last = (st.get("last_signal") or {}).get(sym)
                if last is not None and trading_days_between(cal, last, today) < cooldown:
                    self._audit("SIGNAL_COOLDOWN_SKIP", symbol=sym, last_signal=last,
                                cooldown=cooldown)
                    continue
                # 单仓一次性模型防重（decide_position 同源口径；本地 has_order(OPEN)+
                # UNIQUE 约束的 pilot 对应）：已持仓（有剩余）或已有在途买单的标的不接新信号
                held = int((st["positions"].get(sym) or {}).get("remaining_qty") or 0) > 0
                open_buy = any(o.get("symbol") == sym and o.get("purpose") == "OPEN"
                               and o.get("status") not in _TERMINAL_ORDER_STATES
                               for o in st["orders"].values())
                if held or open_buy:
                    self._audit("SIGNAL_SKIP_HELD", symbol=sym, held=held, open_buy=open_buy)
                    continue
                formed = (pd.Timestamp(sig.formed_at).strftime("%Y-%m-%d")
                          if sig.formed_at is not None else t_minus_1)
                signals.append(sig)
                st.setdefault("last_signal", {})[sym] = formed  # 锚点更新=信号被采信（≈本地 plan）
                self._audit("SIGNAL", symbol=sym, neckline=sig.neckline,
                            entry_price=sig.entry_price, rr=sig.rr, formed_at=formed,
                            atr=sig.atr)
            st["scan_done"].add(today)

        # ── ④ 闸序：人工风控开关只拦增量（ADR-16；存量管理 ①② 已跑完）──
        if is_blocked(path=self.risk_flag):
            if signals:
                self._audit("BLOCK_SKIP", msg="RISK_BLOCK.flag 在场：跳过挂单段"
                            "（①撤单②超期平仓等存量管理照跑）")
            signals = []

        # ── ⑤ 挂限价买（逐单 check_caps；equity 一次查询逐单复用——CAP 额度式单调）──
        if signals:
            equity = self._query_equity()                     # 空表/异常 → None → 当日不挂
            positions_mv = self._query_positions_mv()         # 异常 → None；空仓 [] → 0.0
            open_buy = _open_buy_amount(st)                   # 残缺 → None（fail-closed）
            cap = self._read_cap_resilient()
            pos_cap = float(TRADE_CFG.get("pos_cap", 0.05))   # 快照 trade_cfg.pos_cap=0.05
            for sig in signals:
                try:
                    entry = float(sig.entry_price)
                except (TypeError, ValueError):
                    # 防御档：detect 契约保证 entry_price 数值（§1 _post_detect 装配式），
                    # 走到这=Signal 形态异变——拒该单不炸整段（后续信号照挂、state 照落盘）
                    self._audit("ORDER_BLOCKED", symbol=sig.symbol,
                                reason=f"信号 entry_price 残缺（{sig.entry_price!r}），拒挂")
                    continue
                qty = int(equity * pos_cap / entry / 100) * 100 if equity is not None else 0
                if equity is not None and qty <= 0:
                    # 定尺不足一手（终审 M-5）：equity×pos_cap 按当前 entry 定不出
                    # 100 股整数倍——不是参数残缺（check_caps ① 的旧文案会误导晨检
                    # 去查查询通道），是「额度买不起一手」的正常业务拒绝，独立文案。
                    self._audit("ORDER_BLOCKED", symbol=sig.symbol, reason=(
                        f"定尺不足一手（equity×pos_cap 不够 100 股："
                        f"{equity:.2f}×{pos_cap:g}={equity * pos_cap:.2f} < 100×"
                        f"{entry:.2f}={entry * 100:.2f}）"))
                    continue
                ok, why = check_caps(st, equity, positions_mv, open_buy,
                                     price=entry, qty=qty, today=today, cap=cap)
                if not ok:
                    self._audit("ORDER_BLOCKED", symbol=sig.symbol, reason=why)
                    continue
                try:
                    cid = place_limit_buy(a, sig.symbol, entry, qty, self.account)
                except Exception as e:                         # GmError/断连：该标的放弃，不炸挂单环
                    self._audit("WARN", type="place_buy_fail", symbol=sig.symbol,
                                err=f"{type(e).__name__}: {e}")
                    continue
                if cid is None:
                    self._audit("WARN", type="place_buy_empty_receipt", symbol=sig.symbol)
                    continue
                # cancel_on = 颈线+cancel_thresh_mult×H（§1 _post_detect R1 守卫同式——
                # 识别期挡多少、挂单期就撤多少，两层数学必须同源）
                ep = sig.exec_params or {}
                ctm = ep.get("cancel_thresh_mult", EXEC_PARAMS.get("cancel_thresh_mult"))
                h_geom = (float(sig.neckline) - float(sig.bottom)
                          if sig.bottom is not None else 0.0)
                cancel_on = (float(sig.neckline) + float(ctm) * h_geom
                             if (ctm is not None and h_geom > 0) else None)
                formed = (pd.Timestamp(sig.formed_at).strftime("%Y-%m-%d")
                          if sig.formed_at is not None else today)
                st.setdefault("placed", {}).setdefault(today, []).append(cid)
                st["orders"][cid] = {"symbol": sig.symbol, "date": today, "price": entry,
                                     "qty": qty, "purpose": "OPEN", "cancel_on": cancel_on,
                                     "formed_at": formed, "exec_params": dict(ep),
                                     "neckline": sig.neckline, "atr": sig.atr,
                                     "bottom": sig.bottom,          # 信号几何：成交富化（_enrich）的原料
                                     "status": "SUBMITTED", "filled": 0,
                                     "account": self.account}
                open_buy = float(open_buy) + entry * qty       # 逐单扣减（check_caps ②口径）
                self._audit("ORDER_PLACED", symbol=sig.symbol, price=entry, qty=qty,
                            cl_ord_id=cid, cancel_on=cancel_on, formed_at=formed)

        # ── ⑤' 同日增补订阅（C-1 三时点之三）：当日新挂的 OPEN 买（与 ② 的超期卖）
        #    当日就要被 tick 巡检管理（新买单的 cancel_on 触价撤单、成交转仓后的
        #    止损止盈），不等次日 pre_open。按最新 state 重算 watchlist 增量差集
        #    下发——已订阅标的零重复（幂等口径见 _subscribe_watchlist 头注）。──
        self._subscribe_watchlist()

        save_state(st, path=self.state_file)

    # ---------------------------------------------------------- 事件：盘中巡检
    def on_tick(self, context, tick):
        """tick 巡检：pending → decide_pending → 撤；positions → decide_position → 卖。

        tick 字段（Q3/D5 权威）：最新价=tick["price"]（非 last_price）、symbol 是 gm
        格式（回调内折 ts）。卖出价口径：止损挂【现价】（跳空穿价也能即成交——限价挂
        stop 价在跳空下方会永不成交，硬风控优先成交性）；tp1/tp2 挂【触发价】（市场
        已在触发价上方，限价即成交且保底触发价）。
        """
        today = _today_str()
        # 对账双闸：成功节流（30s 窗）× 失败退避（60s 窗，M-4）——首跳必对账；成功后
        # 窗内不重查（省查询）；失败后窗内不重试（故障期不每 tick 打两次注定失败的
        # 查询，巡检判定继续吃上一份 state 真值，窗口到期由下一根 tick 自然承接）。
        _now = time.time()
        if ((self._last_absorb is None
             or _now - self._last_absorb >= _ABSORB_THROTTLE_SECONDS)
                and (self._absorb_retry_after is None or _now >= self._absorb_retry_after)):
            self.reconcile(context)                           # 首跳必对账；此后按节流窗吸收成交
        cal = self._calendar(today)
        st = self.state
        # gm→ts 符号折算（终审 M-6：fail-loud 改 WARN-skip 单事件降级）——tick 事件
        # 是高频入口，一支试点外标的（柜台/行情侧异变注入的 CFFEX 等）把整根回调炸
        # 出=巡检环死一只 tick 事件全部陪跳，且 gm 侧不会因我们炸了就停推；改为
        # WARN 留痕（type=tick_symbol_unmappable）+ 跳过本事件（该 tick 无从折算
        # symbol，巡检判定本就无从做起），其余标的的后续 tick 照常。启动期/对账期的
        # from_gm_symbol 仍 fail-loud（那里的异变=结构性问题该炸给人工）。
        try:
            sym = from_gm_symbol(tick["symbol"])
        except Exception as e:
            self._audit("WARN", type="tick_symbol_unmappable",
                        symbol=(tick or {}).get("symbol"),
                        err=f"{type(e).__name__}: {e}")
            return
        # tick 价防御（0821 评审遗留项 on_tick price 防御，2026-08-26 落地）：行情侧
        # 异变（None/0/NaN/非数值）原样 float() 会把整根回调炸出——巡检环死一只脏
        # tick 全部陪跳。改为 WARN 留痕（type=tick_price_invalid）+ 跳过本事件：
        # 无有效价，pending/positions 判定本就无从做起；其余标的后续 tick 照常。
        try:
            px = float(tick["price"])
            if not (px > 0.0) or px == float("inf") or px != px:
                raise ValueError(f"非正/非有限 tick 价 {tick.get('price')!r}")
        except Exception as e:
            self._audit("WARN", type="tick_price_invalid",
                        symbol=(tick or {}).get("symbol"),
                        err=f"{type(e).__name__}: {e}")
            return
        acted = False

        # ── pending：挂单等待期撤单判定（只判 OPEN——卖单无 cancel_on/max_wait 语义锚）──
        for oid, o in list(st["orders"].items()):
            if o.get("symbol") != sym or o.get("purpose") != "OPEN":
                continue
            if o.get("status") in _TERMINAL_ORDER_STATES:
                continue
            verdict = decide_pending(px, o, today, cal)       # 空历 → trading_days_between=0 → max_wait 恒不触发（Task 6 头注口径）
            if verdict == "chase":
                # R6-10 chase 追入（C 线② · 2026-08-25）：等待期届满不弃——守卫现价
                # ≥ tp2（形态目标透支）仍弃；否则撤旧限价单、按现价限价追入（≈市价）。
                # tp2 从本单几何算（enrich 同式：颈线+tp_h_mult×H，价格单源）。
                # 守卫链 fail-closed：tp2 缺几何→None（不拦，追后 decide_position
                # tp2 全平兜底）；追入挂单失败→本 tick 放弃下 tick 重判重试。
                tp2 = None
                try:
                    _nl = float(o.get("neckline"))
                    _bt = float(o.get("bottom"))
                    if _nl > _bt:
                        _tp_h = float((o.get("exec_params") or {}).get("tp_h_mult", 2.0))
                        tp2 = _nl + _tp_h * (_nl - _bt)
                except (TypeError, ValueError):
                    tp2 = None
                if tp2 is not None and px >= tp2:
                    self._cancel_and_sync(oid, sym, "on_tick", "max_wait")
                    self._audit("WARN", type="chase_target_exhausted", symbol=sym,
                                px=px, tp2=tp2)
                    acted = True
                    continue
                self._cancel_and_sync(oid, sym, "on_tick", "chase")
                try:
                    cid = place_limit_buy(self._a(), sym, px, int(o.get("qty") or 0),
                                          self.account)
                except Exception as e:
                    self._audit("WARN", type="chase_buy_fail", symbol=sym,
                                err=f"{type(e).__name__}: {e}")
                    cid = None
                if cid is not None:
                    st["orders"][cid] = {"symbol": sym, "date": today, "price": px,
                                         "qty": int(o.get("qty") or 0), "purpose": "CHASE",
                                         "cancel_on": None, "formed_at": o.get("formed_at"),
                                         "exec_params": dict(o.get("exec_params") or {}),
                                         "neckline": o.get("neckline"),
                                         "bottom": o.get("bottom"), "atr": o.get("atr"),
                                         "status": "SUBMITTED", "filled": 0,
                                         "account": self.account}
                    self._audit("CHASE_BUY", symbol=sym, price=px,
                                qty=int(o.get("qty") or 0), cl_ord_id=cid)
                    acted = True
                continue
            if verdict:
                self._cancel_and_sync(oid, sym, "on_tick", verdict)
                acted = True

        # ── positions：离场判定（在途卖单守卫防重复挂卖）──
        pos = st["positions"].get(sym)
        if (pos is not None and int(pos.get("remaining_qty") or 0) > 0
                and not self._has_open_sell(sym)):
            verdict = decide_position(px, pos, today, cal)
            if verdict:
                _, qty, reason = verdict
                if qty <= 0:
                    # 反转 regime 的 tp2_dust（lot2 份额不足一手）：份额沉 lot1，
                    # 置位 tp2_done 不落单（防每 tick 重判空转 + 防 after_close 误扫）。
                    pos["tp2_done"] = True
                    self._audit("WARN", type="tp2_dust_sinks", symbol=sym,
                                known_divergence="tp2_dust_sinks")
                else:
                    if reason in ("stop_loss", "tp2_eod_sweep"):
                        price = px                                # 止损/盘后 sweep 跟现价（见头注）
                    elif reason in ("tp2", "tp2_share"):
                        price = float(pos.get("tp2_price") or px)
                    else:
                        price = float(pos.get("tp1_price") or px)
                    try:
                        cid = sell_limit(self._a(), sym, price, qty, self.account)
                    except Exception as e:                         # GmError/断连：本 tick 放弃，下 tick 重判重试
                        self._audit("WARN", type="tick_sell_fail", symbol=sym, reason=reason,
                                    err=f"{type(e).__name__}: {e}")
                        cid = None
                    if cid is not None:
                        st["orders"][cid] = {"symbol": sym, "date": today, "price": price,
                                             "qty": qty, "purpose": reason.upper(),
                                             "cancel_on": None, "formed_at": None,
                                             "exec_params": {}, "status": "SUBMITTED",
                                             "filled": 0, "account": self.account}
                        detail = {"symbol": sym, "reason": reason, "qty": qty, "price": price,
                                  "cl_ord_id": cid}
                        if reason == "tp2_share":
                            # 反转 regime lot2 一档一次：落单即置位（tp1_done 同款语义）
                            pos["tp2_done"] = True
                        if reason == "tp2_eod_sweep":
                            # 盘后 sweep 出场已落单：清标记（remaining 归零后自然无害，
                            # 清标记防极端部分成交场景下次日重复强平）
                            pos["force_exit"] = False
                            detail["known_divergence"] = "tp1_eod_sweep_next_open"
                        if reason == "tp1":
                            pos["tp1_done"] = True                # 一档一次：落单即置位（单仓一次性模型）
                            _tp1 = pos.get("tp1_price")
                            _tp2 = pos.get("tp2_price")
                            _inverted = (_tp1 is not None and _tp2 is not None
                                         and float(_tp1) > float(_tp2))
                            if not _inverted and qty == int(pos.get("remaining_qty") or 0):
                                # 已知分歧标记（Task 7 评审指令）：不足一手清剩余——本地两腿
                                # 模型此档份额「沉到 tp2 腿」，pilot 清剩余；双轨复盘剔除用
                                # （反转 regime 的 tp1 全卖是 lot1 正常出场，非 dust 分歧）
                                detail["known_divergence"] = "tp1_dust_clears"
                        self._audit("SELL", **detail)
                        acted = True
        if acted:
            save_state(st, path=self.state_file)              # 只在有动作时落盘（tick 热路径不写盘）

    # ---------------------------------------------------------- 事件：盘后
    def after_close(self, context):
        """盘后三件套：EOD 审计收尾行 + 清当日动态订阅 + state 落盘。"""
        today = _today_str()
        st = self.state
        placed_today = st.get("placed", {}).get(today, [])
        open_cnt = sum(1 for o in st["orders"].values()
                       if o.get("status") not in _TERMINAL_ORDER_STATES)
        self._audit("EOD", date=today, positions=len(st["positions"]),
                    open_orders=open_cnt, placed_today=len(placed_today),
                    params_fingerprint=PARAMS_FINGERPRINT)
        # ── 反转形态盘后 sweep（R6-6 冠军形态对齐，2026-08-26）：tp2 已触（lot2 已出）
        #    但 lot1 未出 → 置 force_exit，次日首 tick 市价出。对齐 R6-4 修复后回测
        #    语义「首触 tp2 当日 lot1 未摸 tp1 → 随 tp2 同价平」——tick 腿无法当日
        #    收盘卖，次日首 tick 是最近似（隔夜跳空风险入对照台账，
        #    known_divergence=tp1_eod_sweep_next_open，on_tick 落单时标注）。
        for sym, pos in st["positions"].items():
            if (int(pos.get("remaining_qty") or 0) > 0
                    and pos.get("tp2_done") and not pos.get("tp1_done")
                    and not pos.get("force_exit")):
                pos["force_exit"] = True
                self._audit("WARN", type="tp1_eod_sweep_marked", symbol=sym,
                            remaining_qty=pos.get("remaining_qty"))
        if self._subscribed:
            # 清订阅容错（终审 M-4）：unsubscribe 抛错（断连/终端已收市）只 WARN 不炸
            # 盘后收尾（EOD 行已落、state 落盘在后，炸了=丢尾），且【无论成败都清
            # 账本】——失败不清的后果是次日差集恒空：gm 侧订阅可能已死（会话断开），
            # 账本却记着在场，pre_open ⓪' 的增量订阅永远不下发 = 巡检静默断供且无人
            # 知道。清账本后次日 ⓪' 按最新 state 全量重订（gm subscribe 幂等性不赌
            # SDK、已订阅标的重复下发也无害——_subscribe_incremental 头注口径），
            # 语义自洽：账本恒等于「本会话已请求订阅面」，跨日恢复以重订为准。
            try:
                self._a().unsubscribe([to_gm_symbol(s) for s in self._subscribed],
                                      frequency="tick")
            except Exception as e:
                self._audit("WARN", type="unsubscribe_fail",
                            symbols=list(self._subscribed),
                            err=f"{type(e).__name__}: {e}")
            self._subscribed = []
        save_state(st, path=self.state_file)


# ---- §6 模块级 gm 回调入口（Q3：模块级全局函数名 getattr 约定；首参恒 context）----
RT = None   # 全局运行时（init 建立后在场；回调先于 init 触发=编排事故，fail-loud）


def _require_rt() -> "PilotRuntime":
    """回调前置守卫：RT 未初始化即炸（中文诊断）——静默吞事件=策略假活着，更危险。"""
    if RT is None:
        raise RuntimeError("RT 未初始化：gm init 回调未执行（定时/tick 事件先于 init 触发"
                           "属编排事故，须人工排查终端事件序）")
    return RT


def init(context):
    """gm init 回调：建全局 RT → bootstrap（配置/对账/订阅/双定时注册）。"""
    global RT
    RT = PilotRuntime(api=None, workdir=None)                 # 生产布局：api 惰性 _api()、目录就地
    RT.bootstrap(context)


def pre_open_job(context):
    """09:15:00 定时（Q2；live 待验证项）：盘前五阶段。"""
    _require_rt().pre_open(context)


def after_close_job(context):
    """15:35:00 定时：盘后收尾（收尾行留给收盘后的人工复核窗口）。"""
    _require_rt().after_close(context)


def on_tick(context, tick):
    """tick 巡检回调（订阅面=持仓∪未终态挂单）。"""
    _require_rt().on_tick(context, tick)


def run_pilot():
    """试点主入口：C1 仿真守卫 → gm run（MODE_LIVE 绑仿真账户，阻塞至 stop）。

    运行形态（Task 2 文档 Q9）：run(filename=__file__) 剥 sys.path 前缀 → import_module
    二次导入策略模块（该实例 __name__ 非 __main__，模块级回调 init/pre_open_job/
    on_tick/after_close_job 被 run() getattr 抓取注册）；终端注入的命令行参数
    （--strategy_id/--token/--mode/--serv_addr）优先于本函数实参——本函数传的是
    兜底值。MODE_LIVE 主循环 gmi_poll 阻塞在 run() 内直到 stop()——本函数不返回。
    """
    cfg = _read_runtime_config()                              # 缺文件/缺 token → raise（中文）
    _c1_guard(cfg.get("account_id"))                          # C1：账户白名单 + env 逃生门
    a = _api()
    # filename 必须传**裸文件名**（如 'main.py'），绝不能传 __file__ 绝对路径（2026-08-21
    # 终端首启实测踩坑）：gm run() 内部（basic.py:574-587）会剥掉「与 sys.path 的公共
    # 前缀」再转成模块名 import——Windows 盘符路径的最长公共前缀往往只剩盘符根，
    # 剥完剩 `\Users\...` → 转点成 `.Users...`（前导点）→ import_module 误判相对导入
    # 直接 TypeError。裸文件名则走「脚本目录已在 sys.path[0]」的 Python 原生解析，
    # import_module('main') 命中同目录副本——该副本 __name__='main'（非 __main__），
    # 顶部入口抑制守卫恰好令其不再调 run_pilot（无递归），SDK 从该副本抓事件回调。
    a.run(strategy_id=str(cfg.get("strategy_id") or ""),
          filename=os.path.basename(__file__),
          mode=a.MODE_LIVE, token=str(cfg.get("token")))


if _IS_MAIN:                                                  # 入口用捕获值（见顶部 hoist 块头注）
    run_pilot()
