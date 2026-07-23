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
const timers = []
function setTracked(fn, ms) { const id = setInterval(fn, ms); timers.push(id); return id }

function updateStatus(s) {
  store.engineStatus = { ...store.engineStatus, ...s }
  store.engineOnline = s.engine_running === true
  document.body.classList.toggle('engine-online', store.engineOnline)
}

async function refreshAll() {
  try {
    const [sch, plugins, status] = await Promise.all([getSchema(), loadPlugins(), getEngineStatus()])
    store.schema = sch
    store.pluginsData = plugins
    updateStatus(status)
  } catch (e) { /* 引擎离线，静默 */ }
}

async function connectSSE() {
  if (sseSource) { sseSource.close(); sseSource = null }
  let token = ''
  try { token = await window.pywebview?.api?.get_api_token() || '' } catch (e) { /* offline */ }
  if (!token) { updateStatus({ engine_running: false }); return }
  const es = new EventSource('http://127.0.0.1:19198/api/events?token=' + encodeURIComponent(token))
  sseSource = es
  es.addEventListener('open', () => { sseRetry = 0 })
  es.addEventListener('engine_state_changed', (e) => {
    const d = JSON.parse(e.data)
    updateStatus({ engine_running: d.state === 'running' })
    if (d.state === 'running') refreshAll()
  })
  es.addEventListener('action_executed', () => { store.refreshSignal++ })
  es.onerror = () => {
    es.close(); sseSource = null
    if (++sseRetry > SSE_MAX) { updateStatus({ engine_running: false }); return }
    setTimeout(connectSSE, 5000)
  }
}

// 引擎离线时，若停留在需要引擎的页面则回到首页
watch(() => store.engineOnline, (on) => {
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
  connectSSE()
  if (window.pywebview) setTracked(() => store.refreshSignal++, 30000)
})

onUnmounted(() => {
  if (sseSource) sseSource.close()
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
