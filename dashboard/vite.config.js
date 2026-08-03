import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'
import { readFileSync } from 'node:fs'

// 版本号只维护在 notmyfault/version.py，构建时读取它供侧栏和关于页使用。
const versionSource = readFileSync(new URL('../notmyfault/version.py', import.meta.url), 'utf8')
const versionMatch = versionSource.match(/^__version__\s*=\s*["']([^"']+)["']/m)
if (!versionMatch) throw new Error('无法从 notmyfault/version.py 读取版本号')
const appVersion = versionMatch[1]

// 开发服务器使用 5173 端口并代理 /api，构建结果输出到 dist 供 dashboard.pyw 托管。
export default defineConfig({
  plugins: [vue(), tailwindcss()],
  base: './',
  define: {
    __APP_VERSION__: JSON.stringify(appVersion)
  },
  server: {
    port: 5173,
    proxy: { '/api': 'http://127.0.0.1:19198' }
  },
  build: { outDir: 'dist', emptyOutDir: true }
})
