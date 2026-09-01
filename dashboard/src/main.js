import { createApp } from 'vue'
import App from './App.vue'
import { appLogoUrl } from './lib/branding'
import './styles.css'

const favicon = document.querySelector('link[rel="icon"]') || document.createElement('link')
favicon.rel = 'icon'
favicon.type = 'image/png'
favicon.href = appLogoUrl
if (!favicon.parentNode) document.head.appendChild(favicon)

function start() {
  createApp(App).mount('#app')
}

// 桌面 WebView 等桥接就绪，普通浏览器直接挂载供预览和开发调试使用。
if (window.pywebview && window.pywebview.api) {
  start()
} else if (!window.pywebview) {
  start()
} else {
  window.addEventListener('pywebviewready', start, { once: true })
}
