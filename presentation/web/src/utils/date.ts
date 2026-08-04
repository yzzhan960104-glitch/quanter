/**
 * 本地时区业务日期工具。
 *
 * Why 不用 toISOString().slice(0,10)：
 *  toISOString 始终返回 UTC 日期字符串。北京凌晨 0:00-8:00（UTC 前一日 16:00-24:00）
 *  会取到"昨天"，导致驾驶舱/流水查询把当日台账误当作昨日，数据整体错位一天。
 *  改用本地时区的 getFullYear/getMonth/getDate 显式拼装 YYYY-MM-DD，杜绝 UTC 偏移。
 *
 * 默认参数 new Date()：调用方零参即取"当下本地日期"；也接受指定 Date（如区间端点）。
 */
export function toLocalDateStr(d: Date = new Date()): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0"); // getMonth 0-based
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}
