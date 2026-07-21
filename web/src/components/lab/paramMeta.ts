/**
 * 颈线法策略参数中文映射 + 分组（/lab 参数详情面板与新建回测抽屉共用）。
 *
 * 阶段D（2026-07-20）：caisen 形态 33 字段 → 颈线法 18 字段（识别层 11 + 执行层 7）。
 * 键集须与 strategies.neckline_schema.NecklineConfig.model_fields 完全一致——
 * 前后端参数名仍由 GET /config/schema 的 properties 键反射，杜绝漂移。
 * 同步守护：tests/test_param_meta_sync.py 断言参数表键集 == NecklineConfig.model_fields。
 *
 * 分组：识别层（颈线形态判定）/ 执行层（挂单/止盈/仓位/撤单）。识别层=形态核心默认展开。
 */
export type ParamGroup = '识别层' | '执行层'

/** 分组展示顺序（详情面板/抽屉按此序折叠渲染）。 */
export const PARAM_GROUPS: ParamGroup[] = ['识别层', '执行层']

export interface ParamMeta {
  title: string           // 短中文标题（表单 label / 详情字段名）
  group: ParamGroup       // 归属分组
}

/** 字段名 → {标题, 分组}。键集须与 strategies.neckline_schema.NecklineConfig.model_fields 一致。 */
export const PARAM_META: Record<string, ParamMeta> = {
  // —— 识别层（颈线形态判定，11 维）——
  window:               { title: '颈线识别窗口',       group: '识别层' },
  min_touches:          { title: '颈线聚集点数下限',   group: '识别层' },
  min_suppression:      { title: '压制时长下限',       group: '识别层' },
  local_extrema_window: { title: '局部极值窗',         group: '识别层' },
  min_bottoms:          { title: '双底下限',           group: '识别层' },
  breakout_vol_mult:    { title: '突破放量倍数',       group: '识别层' },
  min_rr:               { title: '盈亏比下限',         group: '识别层' },
  max_h_atr:            { title: '形态深度上限(ATR倍)', group: '识别层' },
  stop_atr_mult:        { title: '止损ATR倍数',        group: '识别层' },
  tp_h_mult:            { title: '止盈2 H倍数',        group: '识别层' },
  decay_tau:            { title: '颈线时间衰减',       group: '识别层' },
  // —— 执行层（挂单/止盈/仓位/撤单，7 维）——
  max_holding:          { title: '超时持仓日',         group: '执行层' },
  max_wait:             { title: '挂单等回踩期',       group: '执行层' },
  cooldown:             { title: '信号去重冷却',       group: '执行层' },
  buy_limit_atr_mult:   { title: '挂单价ATR倍数',      group: '执行层' },
  tp1_h_mult:           { title: '止盈1 H倍数',        group: '执行层' },
  tp1_portion:          { title: '止盈1减仓比例',      group: '执行层' },
  cancel_thresh_mult:   { title: '撤单阈值',           group: '执行层' },
}

/**
 * 参数分层（/lab 表单分层展示用）：识别层（形态核心）默认展开；
 * 执行层为高级，折叠在「显示高级参数」开关后。
 */
export const CORE_GROUPS: ParamGroup[] = ['识别层']
export function isCoreGroup(g: ParamGroup): boolean {
  return CORE_GROUPS.includes(g)
}
