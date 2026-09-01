import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'
import { readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

// 版本号只维护在 notmyfault/version.py，构建时读取它供侧栏和关于页使用。
const versionSource = readFileSync(new URL('../notmyfault/version.py', import.meta.url), 'utf8')
const versionMatch = versionSource.match(/^__version__\s*=\s*["']([^"']+)["']/m)
if (!versionMatch) throw new Error('无法从 notmyfault/version.py 读取版本号')
const appVersion = versionMatch[1]

function apiTokenPath() {
  if (process.platform === 'win32') {
    return join(process.env.APPDATA || join(homedir(), 'AppData', 'Roaming'), 'NotmyFault', '.api_token')
  }
  return join(process.env.XDG_CONFIG_HOME || join(homedir(), '.config'), 'notmyfault', '.api_token')
}

function readApiToken() {
  try {
    const token = readFileSync(apiTokenPath(), 'utf8').trim()
    return /^[a-f0-9]{64}$/i.test(token) ? token : ''
  } catch { return '' }
}

// 开发服务器使用 5173 端口并代理 /api，构建结果输出到 dist 供 dashboard.pyw 托管。
export default defineConfig({
  plugins: [vue(), tailwindcss()],
  base: './',
  define: {
    __APP_VERSION__: JSON.stringify(appVersion)
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:19198',
        configure(proxy) {
          proxy.on('proxyReq', request => {
            const token = readApiToken()
            if (token) request.setHeader('Authorization', `Bearer ${token}`)
          })
        },
      },
    },
  },
  build: { outDir: 'dist', emptyOutDir: true }
})
