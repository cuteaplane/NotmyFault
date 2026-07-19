import { createApp } from 'vue'
import App from './App.vue'
import './styles.css'

function start() {
  createApp(App).mount('#app')
}

// pywebview 的 bridge 在页面 load 后注入，如果 Vue 先挂载了，
// 所有子组件的 onMounted 里 hasBridge() 都会返回 false，
// 导致读不到诊断、写操作 403（没带 token）。
// 解决办法：等 bridge 就绪再挂载 Vue，一劳永逸。
if (window.pywebview && window.pywebview.api) {
  start()
} else {
  let started = false
  const boot = () => { if (!started) { started = true; start() } }
  window.addEventListener('pywebviewready', boot, { once: true })
  // 超时兜底（非 pywebview 环境或 bridge 注入极慢）
  setTimeout(boot, 2000)
}
