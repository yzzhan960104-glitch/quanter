/**
 * 静态快照读取（VITE_STATIC_DATA=1 公网只读模式 · yzzhan.xin）。
 *
 * 物理定位：公网站点=纯静态部署（Cloudflare Pages Direct Upload），数据来自
 * 构建期随包发布的脱敏 JSON 快照（ops/public_snapshot.py 生成到 public/data/），
 * 不连任何后端——写入通路在结构上不存在（物理只读红线）。
 *
 * 约定：快照文件形状=对应 facade 的「返回值」形状；缺失文件按 fallback 语义
 * 降级（audit/ab 缺历史日 → 空数组/抛错由组件 catch，与在线模式同降级路径）。
 */
export const STATIC_MODE = !!import.meta.env.VITE_STATIC_DATA

const BASE = import.meta.env.BASE_URL || '/'

export async function staticGet<T>(name: string, fallback?: T): Promise<T> {
  const res = await fetch(`${BASE}data/${name}.json`)
  if (!res.ok) {
    if (fallback !== undefined) return fallback
    throw new Error(`快照缺失: ${name} (${res.status})`)
  }
  return res.json() as Promise<T>
}

/** 本地日期 YYYY-MM-DD（静态态 audit/ab 缺省日参数时与视图默认今天对齐）。 */
export function staticToday(): string {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}
