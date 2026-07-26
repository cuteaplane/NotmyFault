import { createApp } from 'vue'
import App from './App.vue'
import './styles.css'

function start() {
  createApp(App).mount('#app')
}

// Dashboard 只在 pywebview 中运行。必须等桌面 bridge 注入后再挂载，
// 不提供浏览器模式或无 bridge 降级路径。
if (window.pywebview && window.pywebview.api) {
  start()
} else {
  window.addEventListener('pywebviewready', start, { once: true })
}
