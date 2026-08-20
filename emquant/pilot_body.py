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
"""
import bisect
import csv
import json
import os
import tempfile
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


# ts↔gm 交易所码表：universe 导出侧已过滤北交所（300/301/688/689 前缀），这里只见
# 沪深两市；表外后缀一律 fail-loud——见到 .BJ 说明上游过滤被绕过，映射错一只 =
# 取数/下单打到错误市场，必须在启动期炸给人工（宁停不错）。
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
            f"无法映射 gm 符号（仅支持沪深 ts 格式如 600000.SH/300750.SZ，北交所已在 "
            f"universe 导出侧过滤，见到即上游异变须人工介入）：{ts!r}")
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

    失败契约：gm 异常/空结果/列缺失 → None + audit WARN（type=fetch_fail/fetch_empty，
    带 symbol/err 详情）——编排层 None-check 跳过该标的，绝不拿残缺 df 喂识别
    （识别器没有「数据可能短一截」的守卫义务）。api 实参 None → 经 _api() 惰性取
    （测试 monkeypatch _GM 注入替身的离线路径）。
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
        return out.tail(n_roots)                                      # 尾部截断：保最近、弃最老
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
    return bisect.bisect_right(cal, end) - bisect.bisect_right(cal, start)


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
    """
    return {"version": 1, "scan_done": set(), "placed": {}, "orders": {}, "positions": {}}


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
    p = d / f"audit_{date.today():%Y%m%d}.csv"                 # 按日分文件：晨检只看当天、复核期自然滚动
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


def run_pilot():
    """试点主入口（Task 8 实现：五阶段事件编排——预开/开盘/盘中巡检/收盘/盘后）。"""
    raise NotImplementedError("Task 8 实现")


if __name__ == "__main__":
    run_pilot()
