<script setup>
import { ref, watch, onMounted, onUnmounted } from 'vue'
import NavRail from './components/NavRail.vue'
import HomeView from './components/views/HomeView.vue'
import PluginsView from './components/views/PluginsView.vue'
import SecurityView from './components/views/SecurityView.vue'
import RulesView from './components/views/RulesView.vue'
import LogsView from './components/views/LogsView.vue'
import AboutView from './components/views/AboutView.vue'
import { store } from './lib/store'
import { snack } from './lib/notify'
import { loadConfig, loadPlugins, getSchema, getEngineStatus } from './lib/api'
import { useTheme } from './composables/useTheme'

const { init: initTheme } = useTheme()
const currentPage = ref('home')
const views = { home: HomeView, plugins: PluginsView, security: SecurityView, rules: RulesView, logs: LogsView, about: AboutView }

function switchPage(p) { currentPage.value = p }

let sseSource = null
let sseRetry = 0
const SSE_MAX = 10
let sseReconnectTimer = null
const timers = []
function setTracked(fn, ms) { const id = setInterval(fn, ms); timers.push(id); return id }

function updateStatus(s) {
  store.engineStatus = { ...store.engineStatus, ...s }
  if ('api_alive' in s) store.controllerOnline = s.api_alive === true
  if ('engine_running' in s) store.engineOnline = s.engine_running === true
  document.body.classList.toggle('controller-online', store.controllerOnline)
  document.body.classList.toggle('engine-online', store.engineOnline)
}

async function refreshAll() {
  const status = await getEngineStatus().catch(() => ({
    api_alive: false, engine_running: false, engine_state: 'offline',
  }))
  updateStatus(status)
  if (!status.api_alive) return
  try {
    const [sch, plugins] = await Promise.all([getSchema(), loadPlugins()])
    store.schema = sch
    store.pluginsData = plugins
  } catch (e) { /* 引擎离线，静默 */ }
}

async function refreshStatus() {
  const wasOnline = store.controllerOnline
  const status = await getEngineStatus().catch(() => ({
    api_alive: false, engine_running: false, engine_state: 'offline',
  }))
  updateStatus(status)
  if (!wasOnline && status.api_alive) {
    refreshAll()
    connectSSE()
  }
}

async function connectSSE() {
  sseReconnectTimer = null
  if (sseSource) { sseSource.close(); sseSource = null }
  let token = ''
  try { token = await window.pywebview?.api?.get_api_token() || '' } catch (e) { /* offline */ }
  if (!token) { updateStatus({ engine_running: false }); return }
  const es = new EventSource('http://127.0.0.1:19198/api/events?token=' + encodeURIComponent(token))
  sseSource = es
  es.addEventListener('open', () => { sseRetry = 0 })
  es.addEventListener('engine_state_changed', (e) => {
    const d = JSON.parse(e.data)
    updateStatus({
      api_alive: true,
      engine_state: d.state,
      engine_running: d.state === 'running',
    })
    if (d.state === 'running') refreshAll()
  })
  es.addEventListener('action_executed', () => { store.refreshSignal++ })
  es.onerror = () => {
    if (sseSource !== es) return
    es.close(); sseSource = null
    sseRetry++
    if (sseRetry > SSE_MAX) updateStatus({ engine_running: false })
    // 长时间休眠、WebView 网络栈重置或 token 自愈期间都可能连续失败。
    // 达到阈值只改变显示状态，不永久放弃重连。
    const delay = Math.min(1000 * 2 ** Math.min(sseRetry - 1, 4), 15000)
    if (sseReconnectTimer) clearTimeout(sseReconnectTimer)
    sseReconnectTimer = setTimeout(connectSSE, delay)
  }
}

// 只有后台控制服务真正离线时才离开管理页面；自动化暂停期间仍可编辑。
watch(() => store.controllerOnline, (on) => {
  if (!on && ['plugins', 'rules', 'logs', 'security'].includes(currentPage.value)) {
    currentPage.value = 'home'
  }
})

// 暴露刷新入口给子视图（启动/停止引擎后调用）
window.__nmf = { refreshAll, updateStatus }

onMounted(async () => {
  initTheme()
  // main.js 已确保 bridge 就绪才挂载 Vue，这里不用再等
  try {
    const cfg = await loadConfig()
    store.configData = cfg && cfg.rules ? cfg : { rules: [] }
  } catch (e) { store.configData = { rules: [] } }
  await refreshAll()
  if (store.controllerOnline) connectSSE()
  setTracked(refreshStatus, 2000)
  setTracked(() => store.refreshSignal++, 30000)
})

onUnmounted(() => {
  if (sseSource) sseSource.close()
  if (sseReconnectTimer) clearTimeout(sseReconnectTimer)
  timers.forEach(id => clearInterval(id))
})
</script>

<template>
  <NavRail :current="currentPage" @switch="switchPage" />
  <main class="app-main">
    <Transition name="page" mode="out-in">
      <component :is="views[currentPage]" :key="currentPage" />
    </Transition>
  </main>
  <div class="snackbar" :class="{ show: snack.show }">{{ snack.msg }}</div>
</template>
