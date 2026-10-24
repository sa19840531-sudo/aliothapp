import { fileURLToPath, URL } from 'node:url'
import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({ mode }) => {
  // 允许用 .env.development 配置代理；容器部署仍可通过进程环境覆盖。
  const env = loadEnv(mode, process.cwd(), '')
  const proxyTarget =
    process.env.ALIOTH_WEB_API_PROXY_TARGET ||
    env.ALIOTH_WEB_API_PROXY_TARGET ||
    'http://127.0.0.1:8020'

  return {
    plugins: [vue()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    server: {
      host: '0.0.0.0',
      // 区网／Cloudflare 隧道会带非 localhost 的 Host，否则 Vite 直接 403。
      allowedHosts: true,
      watch: {
        // Windows 宿主编辑经 Docker 挂载不产生 inotify 事件，容器内 vite 收不到
        // 文件变更，只能轮询；仅影响 dev server，构建产物不变。
        usePolling: true,
        interval: 300,
      },
      proxy: {
        '/api': {
          // 开发代理：请求 /api/* 转发到后端，避免跨域。
          // 本机联调默认 8020；Docker 请显式设置 ALIOTH_WEB_API_PROXY_TARGET。
          target: proxyTarget,
          changeOrigin: true,
          timeout: 180_000,
          proxyTimeout: 180_000,
        },
      },
    },
  }
})
