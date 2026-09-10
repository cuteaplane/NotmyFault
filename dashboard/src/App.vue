<script setup>
import { defineAsyncComponent, nextTick, ref, watch, onMounted, onUnmounted } from 'vue'
import NavRail from './components/NavRail.vue'
import HomeView from './components/views/HomeView.vue'
import AppDialog from './components/AppDialog.vue'
import { store, syncEngineStatus as updateStatus } from './lib/store'
import { snack } from './lib/notify'
import { fetchAuthenticated, hasBridge, loadConfig, loadPlugins, getSchema, getEngineStatus, getPluginExtensions, getAIDraftingSetting, getRun } from './lib/api'
import { ensureRuleIds } from './lib/bindings'
import { useTheme } from './composables/useTheme'

const { init: initTheme } = useTheme()
const currentPage = ref('home')
const pageTransitioning = ref(false)
let queuedPage = ''
const PluginsView = defineAsyncComponent(() => import('./components/views/PluginsView.vue'))
const SettingsView = defineAsyncComponent(() => import('./components/views/SettingsView.vue'))
const RulesView = defineAsyncComponent(() => import('./components/views/RulesView.vue'))
const FirstRunSetup = defineAsyncComponent(() => import('./components/FirstRunSetup.vue'))
const views = { home: HomeView, plugins: PluginsView, settings: SettingsView, rules: RulesView }
const firstRun = ref(new URLSearchParams(window.location.search).get('first_run') === '1'
  || !!localStorage.getItem('nmf-first-run'))
if (firstRun.value && !localStorage.getItem('nmf-first-run')) {
  localStorage.setItem('nmf-first-run', JSON.stringify({ step: 0 }))
}

function finishFirstRun() {
  firstRun.value = false
  const url = new URL(window.location.href)
  url.searchParams.delete('first_run')
  window.history.replaceState(null, '', url)
  nextTick(() => document.querySelector('.app-main')?.focus({ preventScroll: true }))
}

function switchPage(p) {
  if (p === 'security') {
    store.pendingSettingsSection = 'security'
    p = 'settings'
  } else if (p === 'logs') {
    store.pendingAutomationSection = 'runs'
    p = 'rules'
  }
  if (p === currentPage.value || !views[p]) return
  if (pageTransitioning.value) {
    queuedPage = p
    return
  }
  currentPage.value = p
}

function finishPageTransition() {
  pageTransitioning.value = false
  const main = document.querySelector('.app-main')
  if (main) main.scrollTop = 0
  nextTick(() => main?.focus({ preventScroll: true }))
  if (!queuedPage) return
  const page = queuedPage
  queuedPage = ''
  switchPage(page)
}

let sseAbort = null
let disposed = false
let sseGeneration = 0
let sseRetry = 0
const SSE_MAX = 10
let sseReconnectTimer = null
let sseEventSeq = 0
let refreshSignalTimer = null
// 规则测试回显只关心这几类执行事件。
const RULE_EVENTS = ['action_executed', 'action_skipped', 'action_cancelled', 'action_timed_out', 'workflow_failed', 'workflow_deferred', 'workflow_completed', 'test_assertions_completed', 'error', 'run_dropped', 'run_replaced']
const timers = []
function setTracked(fn, ms) { const id = setInterval(fn, ms); timers.push(id); return id }

const OFFLINE_STATUS = { api_alive: false, engine_running: false, engine_state: 'offline' }
const CONFIG_LOAD_ATTEMPTS = 30
const CONFIG_LOAD_INTERVAL = 150
const REFRESH_SIGNAL_DELAY = 100
let configLoadPromise = null

function delay(ms) {
  return new Promise(resolve => setTimeout(resolve, ms))
}

function applyConfig(cfg) {
  if (!cfg || !Array.isArray(cfg.rules)) return false
  store.configData = { ...cfg, rules: ensureRuleIds(cfg.rules) }
  store.configLoaded = true
  return true
}

function loadConfigData() {
  if (!configLoadPromise) {
    configLoadPromise = loadConfig()
      .then(applyConfig)
      .finally(() => { configLoadPromise = null })
  }
  return configLoadPromise
}

async function loadConfigAtStartup() {
  for (let attempt = 0; attempt < CONFIG_LOAD_ATTEMPTS; attempt++) {
    if (store.configLoaded) return true
    if (await loadConfigData().catch(() => false)) return true
    await delay(CONFIG_LOAD_INTERVAL)
  }
  store.configData = { rules: [] }
  store.configLoaded = true
  return false
}

async function refreshAll(status = null) {
  const currentStatus = status || await getEngineStatus().catch(() => OFFLINE_STATUS)
  updateStatus(currentStatus)
  if (!currentStatus.api_alive) return
  void loadAISettingsOnce()
  const configPromise = loadConfigData().catch(() => {})
  const catalogPromise = Promise.all([
    getSchema(), loadPlugins(), getPluginExtensions(),
  ]).then(([sch, plugins, extensions]) => {
    store.schema = sch
    store.pluginsData = plugins
    store.extensions = extensions
  }).catch(() => { /* 后台短暂不可用时保留已加载的数据。 */ })
  await Promise.all([configPromise, catalogPromise])
}

async function refreshStatus() {
  const wasOnline = store.controllerOnline
  const status = await getEngineStatus().catch(() => OFFLINE_STATUS)
  updateStatus(status)
  if (!status.api_alive) return
  if (!wasOnline) {
    refreshAll(status)
    connectSSE()
  }
}

// AI 设置只在启动后读一次；设置页里未保存的编辑不该被引擎重启刷新覆盖。
let aiSettingsLoaded = false
async function loadAISettingsOnce() {
  if (aiSettingsLoaded) return
  try {
    const { api_key_status: aiApiKeyStatus, ...aiDrafting } = await getAIDraftingSetting()
    store.aiDrafting = { ...store.aiDrafting, ...aiDrafting }
    store.aiApiKeyStatus = aiApiKeyStatus || 'none'
    aiSettingsLoaded = true
  } catch { /* API 未就绪时下次引擎事件再试。 */ }
}

async function connectSSE() {
  if (disposed) return
  const generation = ++sseGeneration
  if (sseReconnectTimer) clearTimeout(sseReconnectTimer)
  sseReconnectTimer = null
  if (sseAbort) { sseAbort.abort(); sseAbort = null }
  let token = ''
  try { token = await window.pywebview?.api?.get_api_token() || '' } catch (e) { /* bridge 暂时不可用时按无 token 处理。 */ }
  if (disposed || generation !== sseGeneration) return
  if (hasBridge() && !token) {
    // 后台服务重启时 token 文件可能尚未发布，这里按退避重连直到 token 出现。
    updateStatus({ engine_running: false })
    sseRetry++
    const delay = Math.min(1000 * 2 ** Math.min(sseRetry - 1, 4), 15000)
    sseReconnectTimer = setTimeout(connectSSE, delay)
    return
  }
  // fetch 流把 token 放在 Authorization 请求头中，URL 只带路径，访问日志中的凭据字段为空。
  const abort = new AbortController()
  sseAbort = abort
  try {
    const res = await fetchAuthenticated('/api/events', { signal: abort.signal })
    if (!res.ok || !res.body) throw new Error('SSE HTTP ' + res.status)
    if (disposed || generation !== sseGeneration) { abort.abort(); return }
    sseRetry = 0
    consumeSSE(res, abort)
    const activeRun = store.activeManualRun
    if (activeRun) {
      getRun(activeRun.runId).then(run => {
        if (disposed || store.activeManualRun !== activeRun) return
        if (run && ['succeeded', 'failed', 'cancelled'].includes(run.status)) {
          dispatchSSEEvent('workflow_completed', JSON.stringify({
            run_id: activeRun.runId, status: run.status, recovered: true,
          }))
        }
      }).catch(() => {})
    }
  } catch (e) {
    if (abort.signal.aborted) return
    scheduleSSEReconnect()
  }
}

function scheduleSSEReconnect() {
  if (disposed) return
  sseRetry++
  if (sseRetry > SSE_MAX) updateStatus({ engine_running: false })
  // 长时间休眠、WebView 网络栈重置或 token 重新发布期间都可能连续失败，超过阈值仍会继续按退避重连。
  const delay = Math.min(1000 * 2 ** Math.min(sseRetry - 1, 4), 15000)
  if (sseReconnectTimer) clearTimeout(sseReconnectTimer)
  sseReconnectTimer = setTimeout(connectSSE, delay)
}

function scheduleRefreshSignal() {
  if (refreshSignalTimer) return
  refreshSignalTimer = setTimeout(() => {
    refreshSignalTimer = null
    store.refreshSignal++
  }, REFRESH_SIGNAL_DELAY)
}

function dispatchSSEEvent(eventName, dataText) {
  if (eventName === 'engine_state_changed') {
    let d = {}
    try { d = JSON.parse(dataText) } catch (err) { return }
    updateStatus({
      api_alive: true,
      engine_state: d.state,
      engine_running: d.state === 'running',
    })
    if (d.state === 'running') refreshAll()
  } else if (eventName === 'action_executed' || eventName === 'workflow_completed') {
    scheduleRefreshSignal()
  }
  if (RULE_EVENTS.includes(eventName)) {
    let d = {}
    try { d = JSON.parse(dataText) } catch (err) { return }
    store.engineEvents.push({ name: eventName, data: d, seq: ++sseEventSeq })
    if (store.engineEvents.length > 40) store.engineEvents.splice(0, store.engineEvents.length - 40)
    if (
      ['workflow_failed', 'workflow_completed', 'run_dropped', 'run_replaced'].includes(eventName)
      && store.activeManualRun?.runId === d.run_id
    ) store.activeManualRun = null
  }
}

async function consumeSSE(res, abort) {
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let eventName = ''
  let dataLines = []
  let lastActivity = Date.now()
  // 服务端每 15 秒发送心跳，45 秒没有数据时重建连接。
  const watchdog = setInterval(() => {
    if (Date.now() - lastActivity > 45000) abort.abort()
  }, 15000)
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      lastActivity = Date.now()
      buffer += decoder.decode(value, { stream: true })
      let idx
      while ((idx = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, idx).replace(/\r$/, '')
        buffer = buffer.slice(idx + 1)
        if (line === '') {
          if (dataLines.length) {
            dispatchSSEEvent(eventName, dataLines.join('\n'))
            eventName = ''
            dataLines = []
          }
          continue
        }
        if (line.startsWith(':')) continue // SSE 冒号行只表示心跳，不带事件数据。
        if (line.startsWith('event:')) eventName = line.slice(6).trim()
        else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart())
      }
    }
  } catch (e) {
    /* 流断开或主动终止时结束读取 */
  } finally {
    clearInterval(watchdog)
    reader.releaseLock()
    if (sseAbort === abort) {
      sseAbort = null
      scheduleSSEReconnect()
    }
  }
}

// 后台控制服务离线时返回首页，自动化暂停时仍可编辑。
watch(() => store.controllerOnline, (on) => {
  if (!on && ['plugins', 'rules', 'settings'].includes(currentPage.value)) {
    switchPage('home')
  }
})

// 子视图在引擎启停后调用这里暴露的刷新入口。
window.__nmf = { refreshAll, updateStatus, switchPage }

onMounted(async () => {
  window.addEventListener('pywebviewready', refreshStatus)
  initTheme()
  void loadConfigAtStartup()
  const status = await getEngineStatus().catch(() => OFFLINE_STATUS)
  if (disposed) return
  updateStatus(status)
  if (status.api_alive) {
    await refreshAll(status)
    if (disposed) return
    connectSSE()
  }
  setTracked(refreshStatus, 2000)
  setTracked(() => store.refreshSignal++, 30000)
})

onUnmounted(() => {
  disposed = true
  sseGeneration++
  window.removeEventListener('pywebviewready', refreshStatus)
  if (sseAbort) sseAbort.abort()
  if (sseReconnectTimer) clearTimeout(sseReconnectTimer)
  if (refreshSignalTimer) clearTimeout(refreshSignalTimer)
  timers.forEach(id => clearInterval(id))
})
</script>

<template>
  <FirstRunSetup v-if="firstRun" @complete="finishFirstRun" />
  <template v-else>
  <NavRail :current="currentPage" @switch="switchPage" />
  <main class="app-main" tabindex="-1" aria-label="页面内容">
    <Transition name="page" mode="out-in" @before-leave="pageTransitioning = true" @after-enter="finishPageTransition">
      <component :is="views[currentPage]" :key="currentPage" />
    </Transition>
  </main>
  </template>
  <div class="snackbar" :class="{ show: snack.show }" role="status">{{ snack.show ? snack.msg : '' }}</div>
  <AppDialog />
</template>
