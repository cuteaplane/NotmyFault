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

// pywebview 在页面加载后注入 API。
if (window.pywebview?.api || import.meta.env.DEV) {
  start()
} else {
  window.addEventListener('pywebviewready', start, { once: true })
}
