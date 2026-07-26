/**
 * Axios 请求实例（共享 HTTP 单例 + 响应拦截器）
 *
 * 职责：
 * 1. 统一配置 baseURL、默认超时
 * 2. 请求拦截器：从 VITE_API_TOKEN 注入 Authorization Bearer（对齐后端 require_write 鉴权）
 * 3. 响应拦截器：统一提取后端中文错误信息，ElMessage 弹出
 *
 * 设计原则：
 * - 不引入复杂的拦截器链，仅做 token 注入 + 错误提取/Toast（两条直白拦截器，零黑盒）
 * - 单例共享：data/macro/review/trading 等域 facade 复用此实例，
 *   共享响应拦截器（中文错误 Toast / 超时降级），避免每个 facade 各自 create
 *   导致拦截器逻辑漂移。
 * - 不导出业务类型：保持「一个域一个 facade」边界，本文件只承载 HTTP 通道，
 *   业务请求/响应类型由各域 facade 自行声明。
 *
 * 历史背景：本实例原置于 backtest.ts（蔡森专精化 Phase 1 已删），现独立为
 * client.ts，脱离回测语义，供所有保留 facade 共享。
 */
import axios, { type AxiosInstance } from 'axios'
import { ElMessage } from 'element-plus'

/**
 * 创建 Axios 实例
 *
 * 开发环境下 baseURL 为空字符串，由 Vite proxy 转发 /api 到后端
 * 生产环境下可通过 VITE_API_BASE 环境变量覆盖
 */
export const apiClient: AxiosInstance = axios.create({
  baseURL: import.meta.env.VITE_API_BASE || '',
  timeout: 60000,   // 默认 60s 超时；各 facade 可按端点覆写（如 review 90s）
  headers: {
    'Content-Type': 'application/json',
  },
})

// ============ 请求拦截器：鉴权 token 注入（对齐后端 require_write） ============
// 物理意图：后端 server/core/auth.py 的 require_write 校验 `Authorization: Bearer <token>`，
// token 真值由后端环境变量 QUANTER_API_TOKEN 决定。前端经 VITE_API_TOKEN 在构建期注入
// 同一字面量（VITE_ 前缀才会暴露给前端 bundle），请求拦截器统一加头，所有 facade 无需各自处理。
//
// 安全边界（CLAUDE.md 量化风控·极度拷问）：
//   - VITE_API_TOKEN 会被打进前端 bundle，等同对「能访问前端」的用户公开；
//   - 故仅适用于【内网/单用户】自部署场景（与 auth.py 静态 Bearer token 设计同前提）；
//   - 公网部署必须叠加后端 QUANTER_ALLOWED_IPS 白名单，或改由反向代理层注入 token，
//     严禁在此裸填生产密钥后直接公网暴露。
//
// 两端对称（零摩擦）：token 为空（未配置 VITE_API_TOKEN）则不注入，后端开发态
// （未配置 QUANTER_API_TOKEN）同样放行——本地开发与 CI 不受影响。
apiClient.interceptors.request.use((config) => {
  const token = import.meta.env.VITE_API_TOKEN
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// ============ 响应拦截器 ============

apiClient.interceptors.response.use(
  // 正常响应直接返回 data（剥离 axios 包壳，facade 直接拿到业务 payload）
  (response) => response.data,
  // 异常响应：提取后端中文错误信息，ElMessage 弹出
  (error) => {
    let message = '请求失败，请检查网络连接'

    if (error.response) {
      // 后端返回了 HTTP 错误响应
      const status = error.response.status
      const detail = error.response.data?.detail

      if (status === 422) {
        // Pydantic 校验失败，提取字段级错误
        if (Array.isArray(detail)) {
          const errors = detail.map((e: any) => e.msg).join('；')
          message = `参数校验失败：${errors}`
        } else {
          message = `参数校验失败：${detail}`
        }
      } else if (status === 500) {
        message = detail || '服务器内部错误'
      } else if (status === 504) {
        message = '请求执行超时，请缩小范围或重试'
      } else {
        message = detail || `请求失败（HTTP ${status}）`
      }
    } else if (error.code === 'ECONNABORTED') {
      message = '请求超时，请缩小范围或重试'
    }

    ElMessage.error(message)
    return Promise.reject(error)
  }
)
