/**
 * 前端统一日志工具（F12 排查用）。
 *
 * 背景：上一轮「K 线不显示」根因是 SSE 流处理中的 catch{return} 静默吞掉
 * JSON.parse 失败的 result 帧，F12 毫无线索。本 logger 提供统一前缀 + 级别，
 * 关键链路（网络收发、Vue 异常）打点，让同类问题在 console 一眼可见。
 *
 * 设计原则（极简）：
 * - 不引第三方依赖，纯 console 封装
 * - debug/info 仅 DEV 输出（避免污染生产）；warn/error 任何环境都输出（错误必须可见）
 * - 统一 [quanter] 前缀，便于 F12 按 prefix 过滤本应用日志
 */

const PREFIX = '[quanter]'
const DEV = import.meta.env.DEV

export const logger = {
  debug: (...args: unknown[]) => {
    if (DEV) console.debug(PREFIX, ...args)
  },
  info: (...args: unknown[]) => {
    if (DEV) console.info(PREFIX, ...args)
  },
  warn: (...args: unknown[]) => {
    console.warn(PREFIX, ...args)
  },
  error: (...args: unknown[]) => {
    console.error(PREFIX, ...args)
  },
}
