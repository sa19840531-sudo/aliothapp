/**
 * 管理后台模块的请求入口：复用工作台同一个 HTTP 客户端（同一份登录态、同一套 401 处理）。
 * 原独立 admin 应用的 api/*.ts 只依赖 `request`，这里按原名导出即可。
 */
export { requestJson as request, AliothHttpError as ApiError, configureHttpClient } from '@/services/http'
