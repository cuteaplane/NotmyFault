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

// Dashboard 只在 pywebview 中运行，收到 pywebviewready 后才挂载 Vue。
if (window.pywebview && window.pywebview.api) {
  start()
} else {
  window.addEventListener('pywebviewready', start, { once: true })
}
