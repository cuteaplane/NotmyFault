import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import pkg from './package.json' with { type: 'json' }

// Dashboard 构建配置
// dev: localhost:5173（HMR），proxy /api -> 引擎 API
// build: 输出多文件到 dist/，由 dashboard.pyw 的本地静态服务器托管
export default defineConfig({
  plugins: [vue()],
  base: './',
  define: {
    __APP_VERSION__: JSON.stringify(pkg.version)
  },
  server: {
    port: 5173,
    proxy: { '/api': 'http://127.0.0.1:19198' }
  },
  build: { outDir: 'dist', emptyOutDir: true }
})
