/**
 * 蔡森策略参数中文映射 + 分组（/lab 参数详情面板与新建回测抽屉共用）。
 *
 * 物理定位（CLAUDE.md 极简）：config.py 的 Field description 已中文但过长（含 rationale），
 * 不适合做表单 label。本文件用扁平 dict 提供「短中文标题 + 分组」，与 config 字段名同源——
 * 前后端参数名仍由 GET /config/schema 的 properties 键反射，杜绝漂移。
 *
 * 同步守护：tests/test_param_meta_sync.py 断言本文件键集 == StrategyConfig.model_fields，
 * config.py 加字段时该测试强制失败 → 此处同步补条目。
 *
 * 分组对齐 config.py 的 7 大物理意图分组：时间跨度/空间高度/量价配合/交易执行/时间止损/风控/蔡森方法学。
 * 【特例】confirm_bars 物理位于 config.py 风控段，但本表归「蔡森方法学」组——它被
 * zigzag_causal/screener 消费（ZigZag pivot 确认窗，形态核心），归方法学组才能让 /lab 表单
 * 把它分进「形态核心」层默认展开（见 CORE_GROUPS）。sync 守护只校验字段名键集，不看 group，安全。
 */
export type ParamGroup =
  | '时间跨度' | '空间高度' | '量价配合' | '交易执行'
  | '时间止损' | '风控' | '蔡森方法学'

/** 分组展示顺序（详情面板/抽屉按此序折叠渲染）。 */
export const PARAM_GROUPS: ParamGroup[] = [
  '时间跨度', '空间高度', '量价配合', '交易执行',
  '时间止损', '风控', '蔡森方法学',
]

export interface ParamMeta {
  title: string           // 短中文标题（表单 label / 详情字段名）
  group: ParamGroup       // 归属分组
}

/** 字段名 → {标题, 分组}。键集须与 caisen.config.StrategyConfig.model_fields 完全一致。 */
export const PARAM_META: Record<string, ParamMeta> = {
  // —— 时间跨度 ——
  min_pattern_bars:   { title: '形态最小跨度',         group: '时间跨度' },
  max_pattern_bars:   { title: '形态最大跨度',         group: '时间跨度' },
  // —— 空间高度 ——
  zigzag_threshold_atr: { title: 'ZigZag波段阈值(ATR倍)', group: '空间高度' },
  min_pattern_depth:    { title: '形态最浅幅度',          group: '空间高度' },
  max_pattern_depth:    { title: 'W底最深幅度阈值',       group: '空间高度' },
  hs_max_pattern_depth: { title: '头肩底深度宽阈值',      group: '空间高度' },
  w_price_tolerance:    { title: 'W底两底价格容忍度',     group: '空间高度' },
  // —— 量价配合 ——
  right_vol_shrink:        { title: '右底缩量比例上限', group: '量价配合' },
  breakout_vol_multiplier: { title: '突破放量倍数',     group: '量价配合' },
  // —— 交易执行 ——
  pullback_window_bars: { title: '回踩触发窗口(K线)', group: '交易执行' },
  pullback_max_pct:     { title: '回踩最高价容忍%',   group: '交易执行' },
  stop_loss_atr_buffer: { title: '止损ATR缓冲',       group: '交易执行' },
  min_rr_ratio:         { title: '盈亏比下限',         group: '交易执行' },
  // —— 时间止损 ——
  max_holding_bars:         { title: '最大持仓周期',    group: '时间止损' },
  timeout_exit_threshold:   { title: '超时砍亏浮盈阈值', group: '时间止损' },
  trailing_activation_bars: { title: '移动止盈激活天数', group: '时间止损' },
  trailing_to_breakeven:    { title: '移动止盈锁本金',   group: '时间止损' },
  // —— 风控 ——
  liquidity_min_amount: { title: '流动性成交额下限',   group: '风控' },
  hv_window:            { title: '历史波动率窗口',     group: '风控' },
  hv_max_quantile:      { title: 'HV异常分位上限',     group: '风控' },
  max_position_pct:     { title: '单标的占总资金上限', group: '风控' },
  macro_regime_veto:    { title: '宏观收缩期一票否决', group: '风控' },
  // —— 蔡森方法学 ——
  confirm_bars:                { title: 'ZigZag末尾pivot确认窗', group: '蔡森方法学' },
  neckline_height_multiple:   { title: '颈线满足级数n',   group: '蔡森方法学' },
  abc_wave_detect:            { title: 'ABC波过程识别',   group: '蔡森方法学' },
  right_above_left:           { title: '右脚>左脚硬规则', group: '蔡森方法学' },
  ma26w_filter:               { title: '26周线打底过滤',  group: '蔡森方法学' },
  ma26w_window:               { title: '26周线计算窗口',  group: '蔡森方法学' },
  pattern_tension_ratio:      { title: '幅宽张力比例下限', group: '蔡森方法学' },
  enable_triangle_bottom:     { title: '启用收敛三角底',   group: '蔡森方法学' },
  triangle_max_pattern_depth: { title: '三角边长比上限',   group: '蔡森方法学' },
  triangle_breakout_min:      { title: '三角突破进度下限', group: '蔡森方法学' },
  triangle_breakout_max:      { title: '三角突破进度上限', group: '蔡森方法学' },
}

/**
 * 参数分层（/lab 表单分层展示用，spec 2026-07-14-param-slim）：
 * 形态核心组默认展开；交易执行/时间止损/风控为高级，折叠在「显示高级参数」开关后。
 * 分层按 group 映射——故 confirm_bars 须在蔡森方法学组（形态核心）而非风控组。
 */
export const CORE_GROUPS: ParamGroup[] = ['时间跨度', '空间高度', '量价配合', '蔡森方法学']
export function isCoreGroup(g: ParamGroup): boolean {
  return CORE_GROUPS.includes(g)
}
