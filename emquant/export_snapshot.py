# -*- coding: utf-8 -*-
"""参数与 universe 定稿快照导出器（.venv310 侧单源 · emquant 试点 Task 3 / spec FR1）。

物理定位：
    把本地 QMT 引擎【当前生效】的「识别参数 + 执行参数 + 交易参数 + 扫描标的池」在导出
    时刻一次性定稿为两份 json（emquant/config/params_snapshot.json + universe.json）。
    它们是掘金单文件 §0 参数区的唯一数据源——pilot 不再读仓库代码 / 实验 DB / .env，
    从根上杜绝「双轨参数漂移」（本地引擎改参数而 pilot 不知情 = 对照实验失效）。

三层参数的真相源与实弹层级（2026-08-21 逐点核实）：
    ① id_params   = DEFAULTS ⊕ ACTIVE 实验 params（识别层 11 键）
    ② exec_params = EXEC_DEFAULTS ⊕ ACTIVE 实验 params（执行层 13 键，
                    含 trailing 三件 + buy_limit_atr_mult + 费率三键——费率键不在
                    实验 schema 内，恒为 EXEC_DEFAULTS 值）
    ③ trade_cfg   = trading.critical._trade_cfg() 的 .env 实弹值（14 键）
    合并点 = strategies/neckline/strategy.py:78-79（build_strategy 构造内
    ``{**DEFAULTS, **ov}`` / ``{**EXEC_DEFAULTS, **ov}``），本导出器走与引擎 _eod
    （trading/engine.py:993）完全相同的 ``build_strategy(name, cfg_override=params)``
    调用取合并后 cfg——复用同一合并语义，不自行拼 dict（防第二套合并逻辑漂移）。
    ⚠️ 引擎实弹层级（engine.py:1258-1286 单源收敛注释）：生命周期/止损参数以
    SIGNAL.meta.exec_params（实验口径定终身）为主，env _trade_cfg 仅作缺键 fallback
    ——故快照主口径必须合并 ACTIVE 实验；只导 EXEC_DEFAULTS 会让 pilot 拿
    window=60/cooldown=5/buy_limit_atr_mult=1.0 的"纸面默认"去对照实跑
    window=80/cooldown=8/buy_limit_atr_mult=0.5 的引擎（参数错档 = 对照事故）。

universe 口径（⚠️ spec/plan 措辞勘误，方向相反）：
    spec FR1 / plan Task 3 写「_filter_chuangke_kechuang 剔 300/301/688/689 前缀」，
    与代码真身方向相反——trading/data_ctx.py:56 load_universe 与
    strategies/neckline/backtest.py:130-131 _filter_chuangke_kechuang 都是
    【只保留】300/301/688/689 前缀（创板科创池；主板/北交所不在可交易池，
    tests/test_neckline_core.py:421 钉死同口径）。且设计文档「双轨对照域 = 两腿
    universe 交集」——若 pilot 剔创板科创而引擎只扫创板科创，交集为空、双轨对照
    直接失效。故本导出器复刻【只保留】口径，勘误全文留档 universe.json 的 source
    字段与 params_snapshot.json 的 notes.universe_semantics_erratum。

幂等红线（重复运行产物逐字节一致）：
    - exported_at 不取 now()（时间戳破坏逐字节一致），改取 data_lake 末根 K 线
      日期——快照语义本来就是「截至数据末日的定稿」，数据不变则产物逐字节不变；
    - fingerprint 只哈希 {id_params, exec_params, trade_cfg, universe_symbols} 本体，
      排除 exported_at/sources/notes（哈希非本体字段 = 时间/注记变化误伤指纹）；
    - 成交额并列时按 symbol 升序破平（总序确定，防排序不稳定漂移指纹）。

用法（须在项目根 cwd 或任意 cwd 均可——脚本自 chdir 项目根）：
    PYTHONUTF8=1 .venv310/Scripts/python.exe emquant/export_snapshot.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

# ============================================================================
# 项目根锚定（范式抄 trading/tools/veto_plan.py:30-33）：两级 dirname（本脚本 →
# emquant → 项目根）+ chdir。Why chdir：experiment/store.py:_DEFAULT_DB 与
# data_lake 路径都是 cwd 相对路径，引擎进程 cwd=项目根，导出器必须同口径取数，
# 否则从别的目录裸跑会静默读不到实验 DB / 数据湖（快照退化为纯默认值 = 事故）。
# ============================================================================
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_PROJECT_ROOT)
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Windows 控制台默认 GBK：中文 print 防 UnicodeEncodeError（veto_plan 同款）
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ============================================================================
# .env 加载（引擎入口范式 · trading/__main__.py:71-75）
# Why 必须：_trade_cfg() 直接 os.getenv——不加载 .env 时拿到的全是函数内缺省值，
# 快照会静默把"纸面默认"当"实弹值"导出（最隐蔽的快照事故）。override=True 对齐
# 引擎入口语义（.env 优先于进程已有环境变量），显式传绝对路径对齐 tools 范式
# （不依赖 cwd——虽然上方已 chdir，双保险零成本）。
# ============================================================================
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=True)
except ImportError:  # dotenv 缺失 → 下方 pos_cap 自检断言会拦（fail-closed 不静默）
    print("⚠️ python-dotenv 不可用：.env 未加载，pos_cap 自检断言预期将失败", file=sys.stderr)

# —— 延迟 import（sys.path/cwd/.env 必须先就绪，import 期报错栈才清晰）——
import pandas as pd  # noqa: E402

from experiment.resolver import resolve_active  # noqa: E402
from strategies.registry import build_strategy  # noqa: E402
from trading.critical import _mode, _trade_cfg  # noqa: E402

# 识别/执行层默认单源（快照的"基线半边"；实验 override 半边见下方 resolve_active）
from strategies.neckline.backtest import EXEC_DEFAULTS  # noqa: E402
from strategies.neckline.method_v0 import DEFAULTS  # noqa: E402

# 输出产物路径（git 提交物：pilot §0 唯一数据源）
_OUT_DIR = os.path.join(_PROJECT_ROOT, "emquant", "config")
_PARAMS_JSON = os.path.join(_OUT_DIR, "params_snapshot.json")
_UNIVERSE_JSON = os.path.join(_OUT_DIR, "universe.json")

# 创板科创前缀池（与 trading/data_ctx.py:56 / backtest.py:130-131 逐字同款元组——
# 单独提常量是为了 universe 断言与过滤共用一份，防两处字面量漂移）
_CK_PREFIXES = ("300", "301", "688", "689")

# 执行层必备键（C9 漏键防御 · 防 signal.py:117 型"键存在但快照漏导"事故：
# 下游 pilot §0 消费这些键做挂单价/去重/移动止损，缺一键 = 对应行为静默退默认）
_EXEC_REQUIRED_KEYS = {
    "buy_limit_atr_mult", "cooldown",
    "trailing_grace", "trailing_step", "trailing_floor",
}

# 池子上限（spec FR1：UNIVERSE ≤300——掘金仿真订阅/扫描成本可控的试点规模）
_UNIVERSE_CAP = 300


def _resolve_live_strategy_cfg() -> tuple[dict, dict, dict]:
    """取引擎实弹的 (id_params, exec_params, experiment_meta)。

    实验通道（引擎 _eod 真实链路）：experiment/experiments.db → resolve_active()
    （status=ACTIVE 且 weight>0）→ build_strategy(strategy_name, cfg_override=params)。
    合并语义复用 NecklineMethodStrategy.__init__（strategy.py:78-79）——本函数不
    自行拼 dict，保证与引擎逐键同源。

    多 ACTIVE 灰度时的选择口径：取 max(weight)（= resolver.resolve_champion 的
    单一选择口径，SSoT review-fix2 P2 定稿）——pilot 单文件只能装一套 §0 参数，
    主导版本是唯一合理选择；全部 ACTIVE 的 id 记入 meta 供审计（引擎本身会跑
    全部 ACTIVE 并按 experiment_id 归因，这是引擎与单文件 pilot 的已知差异，
    留档而非隐瞒）。无 ACTIVE → 纯 DEFAULTS/EXEC_DEFAULTS（此时 meta 记 None）。
    """
    actives = resolve_active()
    if not actives:
        # fail-fast 同引擎（_eod 无在线实验直接 return）——但快照层选择"导出纯默认
        # + meta 记空"，让 pilot 拿到显式定稿而非报错半途（导出器可独立复跑）。
        return dict(DEFAULTS), dict(EXEC_DEFAULTS), {"active_experiments": []}

    champion = max(actives, key=lambda e: e.weight)
    # 与 engine.py:993 完全相同的构造调用（注册表按 strategy_name 反射到
    # NecklineMethodStrategy，cfg_override 分流 id/exec 两层）
    strategy = build_strategy(champion.strategy_name, cfg_override=champion.params)
    meta = {
        "champion_experiment_id": champion.experiment_id,
        "champion_weight": champion.weight,
        "champion_params": dict(champion.params),
        # 多 ACTIVE 时引擎逐实验扫并归因；pilot 只装冠军一套——差异显式留档
        "active_experiments": [
            {"experiment_id": e.experiment_id, "weight": e.weight}
            for e in actives
        ],
    }
    return dict(strategy.id_cfg), dict(strategy.exec_cfg), meta


def _build_universe(lake: pd.DataFrame) -> tuple[list[str], dict]:
    """复刻引擎 _load_universe 口径取创板科创池，按近 60 日日均成交额降序截前 300。

    Returns:
        (symbols, provenance)：symbols 为 ts 格式（如 "300750.SZ"）按流动性降序；
        provenance 记录池子规模/排名口径（写进 universe.json 的 source 与 notes）。
    """
    # ① 池子定义复刻 trading/data_ctx.py:55-56（load_universe 真身）：
    #    全市场 symbol 去重 → 只保留创板科创前缀。绝不能复用 _filter_chuangke_kechuang
    #    import（语义相同但 import 回测模块只为一个列表推导不值当，且两处口径由
    #    tests/test_neckline_core.py:421 同钉）。
    syms_all = lake.index.get_level_values("symbol").unique().tolist()
    ck_pool = [s for s in syms_all if str(s).split(".")[0].startswith(_CK_PREFIXES)]

    # ② 流动性排名：近 60 根日线 amount（千元）均值降序。Why 60 根而非 60 自然日：
    #    日线索引本身即交易日，tail(60) = 近 60 个交易日（与 _eod 的 df_upto.tail(window)
    #    消费口径同轴）；比 backtest main() 的 tail(30) 更保守（新近次新股也能排上），
    #    与 plan Task 3 明文的"近 60 日日均成交额"一致。
    ck_set = set(ck_pool)
    mask = lake.index.get_level_values("symbol").isin(ck_set)
    amt_tail60 = (
        lake.loc[mask, ["amount"]]
        .groupby(level="symbol").tail(60)      # 每 symbol 只取最近 60 根（含末根=数据末日）
        .groupby(level="symbol")["amount"].mean()  # 停牌日 amount=NaN 被 skipna 自然跳过
    ).dropna()  # 全 NaN（长期停牌无成交）不参与排名——无流动性语义，排了也是噪声

    # ③ 总序确定化：成交额降序、并列按 symbol 升序破平（sorted 稳定但 key 必须全序，
    #    否则跨次运行的字节序漂移会破坏 fingerprint 逐字节稳定红线）
    ranked = sorted(amt_tail60.index.tolist(), key=lambda s: (-float(amt_tail60[s]), str(s)))
    universe = ranked[:_UNIVERSE_CAP]  # 池子 >300 截前 300；<300 全取（plan Task 3 口径）

    provenance = {
        "market_symbols_total": len(syms_all),
        "ck_pool_size": len(ck_pool),
        "ranked_size": int(len(amt_tail60)),
        "selected": len(universe),
        "liquidity_rank_basis": "近 60 根日线 amount（千元）均值降序，并列按 symbol 升序破平",
    }
    return universe, provenance


def _fingerprint(payload: dict) -> str:
    """定稿指纹：sha256(json.dumps({id,exec,trade,universe_symbols}))[:16]。

    Why 只哈希四本体（plan Task 3 明文公式）：exported_at/sources/notes 是注记非
    参数本体——时间戳或注记变化不应误伤指纹（幂等红线）；下游（Task 4+/§7 审计）
    用同一公式对 json 重算即可验证快照未被篡改。
    """
    canon = {
        "id_params": payload["id_params"],
        "exec_params": payload["exec_params"],
        "trade_cfg": payload["trade_cfg"],
        "universe_symbols": payload["universe_symbols"],
    }
    return hashlib.sha256(
        json.dumps(canon, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]


def _dump_json(path: str, obj: dict) -> None:
    """UTF-8 + indent=2 + sort_keys 稳定序列化落盘（末根补换行，diff 友好）。

    Why sort_keys/固定 indent：键序与空白逐字节确定 = 幂等红线的序列化半边
    （计算半边由 _fingerprint 的 sort_keys 承担）。
    """
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def main() -> int:
    os.makedirs(_OUT_DIR, exist_ok=True)

    # ── ① 三层参数取数（实验合并 + .env 实弹）────────────────────────────────
    id_params, exec_params, exp_meta = _resolve_live_strategy_cfg()
    trade_cfg = _trade_cfg()  # 只读 TRADE_*/AUTO_TRADE_MODE 相关键，无 token 类值
    mode = _mode()            # AUTO_TRADE_MODE 实弹（live/dry_run），入 notes 留档

    # ── ② 导出期自检断言（fail-closed：任何一条不满足=快照错误=直接拒绝落盘）──
    # C9 漏键防御：执行层必备键（挂单价倍数/信号冷却/trailing 三件）必须齐全。
    missing = _EXEC_REQUIRED_KEYS - set(exec_params)
    assert not missing, f"exec_params 漏键（C9）: {missing}——快照不完整，拒绝导出"
    # 费率三键（Task 12 P1-9 对齐实盘的成本口径）不在实验 schema 内=恒默认，也须在：
    cost_missing = {"commission_rate", "stamp_rate", "transfer_rate"} - set(exec_params)
    assert not cost_missing, f"exec_params 漏费率键: {cost_missing}——回测/实盘成本口径断裂"
    # 键集完整锚：合并后 id/exec 键集必须与默认单源完全一致（多键=脏 override 漏检，
    # 少键=合并吞键；键值允许被实验覆盖，键集不允许漂移）
    assert set(id_params) == set(DEFAULTS), (
        f"id_params 键集漂移: {set(id_params) ^ set(DEFAULTS)}")
    assert set(exec_params) == set(EXEC_DEFAULTS), (
        f"exec_params 键集漂移: {set(exec_params) ^ set(EXEC_DEFAULTS)}")
    # .env 加载自检（任务红线，两段式）：
    # ① 键在场证明——AUTO_TRADE_MODE 只存在于 .env（不在任何代码缺省里外泄到进程
    #    环境的正常路径），fresh 进程 getenv 到它 = dotenv 确实把 .env 灌进了 environ；
    #    缺失说明 dotenv 缺载/文件缺失，此时 _trade_cfg 全走函数内缺省 = 纸面默认
    #    冒充实弹（最隐蔽的快照事故），拒绝导出。
    assert os.getenv("AUTO_TRADE_MODE") is not None, (
        ".env 未加载（AUTO_TRADE_MODE 不在环境）——_trade_cfg 将全走缺省值，"
        "拒绝导出（防纸面默认冒充实弹）")
    # ② 实弹值钉死——pos_cap 当前 .env 实弹=0.075（2026-08-28 c243da10 用户裁决对齐
    #    R6-11b「4并×7.5%」，快照手术指纹 7fe3d5b3f4a04786；旧值 0.05/旧指纹
    #    8858982628989013 留档）。本条是【已知实弹值防漂移】锚：.env 若再改值，此
    #    断言强制导出者有意识同步更新（快照任务就该把已核实的实弹钉死，而非来者不拒）。
    #    硬闸联动：§0 的 PILOT_MAX_NEW_ORDERS_PER_DAY=4 / PILOT_MAX_POSITION_PCT=0.075
    #    与本锚同源（build_pilot 写死），改 pos_cap 须三处同查。
    assert trade_cfg["pos_cap"] == 0.075, (
        f"trade_cfg.pos_cap={trade_cfg['pos_cap']} != 0.075——实弹已变或有异常，"
        "请核实 .env 并有意更新本锚（联动 build_pilot 硬闸三处）")

    # ── ③ data_lake 取数 + universe 构建 ────────────────────────────────────
    # 与 _eod（engine.py:956）同路径同读法；455MB 单次读入（本脚本一次性消费）。
    lake = pd.read_parquet("data_lake/a_shares_daily.parquet")
    universe, uni_prov = _build_universe(lake)

    # universe 约束自检：≤300、全部创板科创前缀、ts 六位代码.市场 后缀格式。
    assert len(universe) <= _UNIVERSE_CAP, f"universe 超上限: {len(universe)}"
    bad_prefix = [s for s in universe if str(s).split(".")[0][:3] not in _CK_PREFIXES]
    assert not bad_prefix, f"universe 混入非创板科创标的: {bad_prefix[:5]}"
    bad_fmt = [s for s in universe if "." not in str(s) or len(str(s).split(".")[0]) != 6]
    assert not bad_fmt, f"universe 非 ts 六位代码格式: {bad_fmt[:5]}"
    assert len(set(universe)) == len(universe), "universe 存在重复标的"

    # exported_at = 数据末根 K 线日期（非 now）：幂等红线的注记半边——快照语义
    # 本来就是「截至数据末日的定稿」，重复运行同数据产物逐字节一致。
    data_as_of = str(lake.index.get_level_values("date").max().date())

    # ── ④ 组装 params_snapshot.json ─────────────────────────────────────────
    params_payload = {
        "id_params": id_params,       # 识别层 11 键（DEFAULTS ⊕ ACTIVE 实验）
        "exec_params": exec_params,   # 执行层 13 键（EXEC_DEFAULTS ⊕ ACTIVE 实验，含费率）
        "trade_cfg": trade_cfg,       # .env 实弹 14 键（仓位/生命周期 env 口径）
        "universe_symbols": universe, # 指纹参与项（与 universe.json 同源同序）
        "exported_at": data_as_of,
        "sources": {
            "id_params": (
                "strategies/neckline/method_v0.py:49 DEFAULTS ⊕ ACTIVE 实验 override"
                "（合并点 strategies/neckline/strategy.py:78；实验通道 experiment/experiments.db"
                " → experiment/resolver.py:15 resolve_active → build_strategy，同引擎 _eod"
                " trading/engine.py:993 调用链）"
            ),
            "exec_params": (
                "strategies/neckline/backtest.py:53 EXEC_DEFAULTS ⊕ ACTIVE 实验 override"
                "（合并点 strategies/neckline/strategy.py:79；费率三键 commission_rate/"
                "stamp_rate/transfer_rate 不在实验 schema=恒为默认值）"
            ),
            "trade_cfg": (
                "trading/critical.py:187 _trade_cfg()——.env 实弹（加载范式="
                "trading/__main__.py:71-75 load_dotenv(override=True)；只读 TRADE_*/"
                "AUTO_TRADE_MODE 相关键，无 token 类敏感值）"
            ),
            "universe": (
                "trading/data_ctx.py:37-56 load_universe 同口径（只保留 300/301/688/689"
                " 前缀）+ 近 60 根日线 amount 均值降序取前 300（并列按 symbol 升序破平）"
            ),
            "experiment": exp_meta,
        },
        "notes": {
            # cooldown 语义核实结论（plan FR1 明文要求，逐条 file:line 证据）：
            "cooldown_semantics": {
                "conclusion": (
                    "scan_live 本身无 cooldown；引擎层 _eod 在 scan 后补跨日去重——"
                    "pilot 必须复刻该语义（按 formed_at 锚点、cooldown 实弹=8 个交易日"
                    "窗口、自然日回溯 cooldown+2 余量）"
                ),
                "evidence": [
                    "strategies/neckline/strategy.py:117-119（scan_at 回测路径：内存"
                    " _last_signal_pos 锚点，T_pos 差 < cooldown 跳过）",
                    "strategies/neckline/strategy.py:231-232（scan_live 实盘路径：纯当日"
                    "识别，无任何 cooldown 去重）",
                    "trading/engine.py:1036-1050（_eod 在 scan 后按 cooldown 过滤同标的："
                    "cooldown=resolve_cooldown_days(experiments)，最近 cooldown+2 自然日"
                    " trade_event SIGNAL.meta.formed_at 标的集内的新信号丢弃）",
                    "trading/data_ctx.py:140-159（resolve_cooldown_days：取全部 ACTIVE 实验"
                    " params['cooldown'] 的 max，保守去重；当前 ACTIVE 实验=8）",
                    "trading/data_ctx.py:92-137（load_recent_plan_symbols：formed_at 锚点"
                    "（非 timestamp/非 plan_date）+ 自然日回溯的 Why 全文）",
                ],
                "live_cooldown_days": exec_params.get("cooldown"),
            },
            # 实弹层级说明（为何必须合并实验参数，而非只导 EXEC_DEFAULTS）
            "exec_live_hierarchy": (
                "引擎生命周期/止损参数以 SIGNAL.meta.exec_params（实验口径定终身）为主、"
                "env _trade_cfg 仅缺键 fallback（trading/engine.py:1258-1286 _decide_cfg_for"
                " 单源收敛，注释原文点名 trailing 5/0.1/0.5 vs 实验 0、max_holding 15 vs 20、"
                "tp1_portion 0.5 vs 0.3 的双源分叉已消灭；pre_open 同口径：pre_open.py:444-460"
                " per-symbol max_holding、pre_open.py:573-575 order_max_wait=o.get('max_wait')"
                " or cfg_max_wait）。故本快照 id/exec 均为实验合并后实弹值。"
            ),
            # trailing 双轨注记：trade_cfg 的 grace/step/floor 是 env 死键（仅 fallback），
            # 实弹 trailing 以 exec_params 为准（当前实验 0/0.0/0.0 = 退化固定止损=本地现状）
            "trailing_dual_source_note": (
                "trade_cfg.grace/step/floor（env TRADE_STOPLOSS_*，当前 5/0.1/0.5）在引擎内"
                "仅作 SIGNAL.meta 缺 exec_params 键时的 fallback 基线（engine.py:1260-1267）；"
                "新信号实弹 trailing = exec_params 的 trailing_grace/step/floor（当前实验"
                " 0/0.0/0.0 → 退化为固定止损，与 spec FR5『快照默认 grace=0/step=0.0 →"
                " 退化固定止损=本地现状』一致）。pilot 以 exec_params 为准。"
            ),
            # universe 勘误（spec/plan 措辞与代码真身方向相反，全文留档防后人误改回去）
            "universe_semantics_erratum": (
                "spec FR1/plan Task 3 写『_filter_chuangke_kechuang 剔 300/301/688/689 前缀』"
                "——与代码真身方向相反：load_universe（trading/data_ctx.py:37-56）与"
                " _filter_chuangke_kechuang（strategies/neckline/backtest.py:122-131）均为"
                "【只保留】创板科创前缀（主板/北交所不在可交易池，tests/test_neckline_core"
                ".py:421 钉死）。双轨对照域=两腿 universe 交集，故本快照复刻【只保留】口径。"
            ),
            "auto_trade_mode": mode,  # .env 实弹（当前 live）；仅留档，不参与指纹
            "fingerprint_formula": (
                "sha256(json.dumps({'id_params','exec_params','trade_cfg',"
                "'universe_symbols'}, sort_keys=True, ensure_ascii=False)"
                ".encode('utf-8')).hexdigest()[:16]——排除 exported_at/sources/notes"
            ),
            "idempotency": (
                "exported_at=data_lake 末根 K 线日期（非 now），数据不变则重复运行产物"
                "逐字节一致；fingerprint 亦排除 exported_at/sources/notes"
            ),
        },
    }
    fingerprint = _fingerprint(params_payload)
    params_payload["fingerprint"] = fingerprint

    # 序列化保真自检：round-trip 后重算指纹必须相等（防浮点/编码序列化漂移）——
    # 这是"下游拿 json 重算 == 落盘 fingerprint"的最强本地证据。
    rt = json.loads(json.dumps(params_payload, ensure_ascii=False))
    assert _fingerprint(rt) == fingerprint, "指纹 round-trip 不一致（序列化漂移）"

    # ── ⑤ 组装 universe.json（schema 按 brief：symbols/source/exported_at 三键）──
    universe_payload = {
        "symbols": universe,
        "source": (
            "复刻引擎实盘 _load_universe 口径（trading/data_ctx.py:37-56，只保留"
            " 300/301/688/689 创板科创前缀，主板/北交所不在池）+ 近 60 根日线 amount"
            "（千元）均值降序取前 300（并列按 symbol 升序破平，池 "
            f"{uni_prov['ck_pool_size']} 只全市场 {uni_prov['market_symbols_total']} 只、"
            f"参与排名 {uni_prov['ranked_size']} 只）。⚠️ spec FR1『剔 300/301/688/689』"
            "为措辞勘误——代码真身与 tests/test_neckline_core.py:421 均为只保留创板科创"
            "（双轨对照域=两腿交集，剔了交集即空）。列表顺序=流动性降序。"
        ),
        "exported_at": data_as_of,
    }

    # ── ⑥ 落盘 + 摘要打印 ────────────────────────────────────────────────────
    _dump_json(_PARAMS_JSON, params_payload)
    _dump_json(_UNIVERSE_JSON, universe_payload)

    champ = exp_meta.get("champion_experiment_id") or "（无 ACTIVE 实验，纯默认）"
    print(f"[export_snapshot] fingerprint        = {fingerprint}")
    print(f"[export_snapshot] id_params 键数     = {len(id_params)}")
    print(f"[export_snapshot] exec_params 键数   = {len(exec_params)}（含费率三键+trailing 三件）")
    print(f"[export_snapshot] trade_cfg 键数     = {len(trade_cfg)}（.env 实弹，pos_cap={trade_cfg['pos_cap']}）")
    print(f"[export_snapshot] universe 数量      = {len(universe)} / 创板科创池 {uni_prov['ck_pool_size']}")
    print(f"[export_snapshot] exported_at(data)  = {data_as_of}")
    print(f"[export_snapshot] ACTIVE 实验冠军    = {champ}")
    print(f"[export_snapshot] cooldown 实弹      = {exec_params.get('cooldown')}（_eod 跨日去重，见 notes）")
    print(f"[export_snapshot] 产物: {_PARAMS_JSON}")
    print(f"[export_snapshot] 产物: {_UNIVERSE_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
