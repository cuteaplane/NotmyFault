<script setup>
import { ref, watch, onMounted, onUnmounted } from 'vue'
import NavRail from './components/NavRail.vue'
import HomeView from './components/views/HomeView.vue'
import PluginsView from './components/views/PluginsView.vue'
import SecurityView from './components/views/SecurityView.vue'
import SettingsView from './components/views/SettingsView.vue'
import RulesView from './components/views/RulesView.vue'
import LogsView from './components/views/LogsView.vue'
import AppDialog from './components/AppDialog.vue'
import { store } from './lib/store'
import { snack } from './lib/notify'
import { loadConfig, loadPlugins, getSchema, getEngineStatus, getPluginComponents, getPluginExtensions, getAIDraftingSetting } from './lib/api'
import { ensureRuleIds } from './lib/bindings'
import { useTheme } from './composables/useTheme'

const { init: initTheme } = useTheme()
const currentPage = ref('home')
const views = { home: HomeView, plugins: PluginsView, security: SecurityView, settings: SettingsView, rules: RulesView, logs: LogsView }

function switchPage(p) {
  if (p === currentPage.value || !views[p]) return
  currentPage.value = p
}

let sseAbort = null
let sseRetry = 0
const SSE_MAX = 10
let sseReconnectTimer = null
let sseEventSeq = 0
// 规则测试回显只关心这几类执行事件。
const RULE_EVENTS = ['action_executed', 'action_skipped', 'workflow_failed', 'workflow_deferred', 'workflow_completed', 'test_assertions_completed', 'error']
const timers = []
function setTracked(fn, ms) { const id = setInterval(fn, ms); timers.push(id); return id }

const OFFLINE_STATUS = { api_alive: false, engine_running: false, engine_state: 'offline' }
const CONFIG_LOAD_ATTEMPTS = 30
const CONFIG_LOAD_INTERVAL = 150

function delay(ms) {
  return new Promise(resolve => setTimeout(resolve, ms))
}

function applyConfig(cfg) {
  if (!cfg || !Array.isArray(cfg.rules)) return false
  store.configData = { ...cfg, rules: ensureRuleIds(cfg.rules) }
  store.configLoaded = true
  return true
}

async function loadConfigData() {
  return applyConfig(await loadConfig())
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

function updateStatus(s) {
  store.engineStatus = { ...store.engineStatus, ...s }
  if ('api_alive' in s) store.controllerOnline = s.api_alive === true
  if ('engine_running' in s) store.engineOnline = s.engine_running === true
  document.body.classList.toggle('controller-online', store.controllerOnline)
  document.body.classList.toggle('engine-online', store.engineOnline)
}

async function refreshAll(status = null) {
  const currentStatus = status || await getEngineStatus().catch(() => OFFLINE_STATUS)
  updateStatus(currentStatus)
  if (!currentStatus.api_alive) return
  await loadConfigData().catch(() => {})
  void loadAISettingsOnce()
  try {
    const [sch, plugins, components, extensions] = await Promise.all([
      getSchema(), loadPlugins(), getPluginComponents(), getPluginExtensions(),
    ])
    store.schema = sch
    store.pluginsData = plugins
    store.components = components
    store.extensions = extensions
  } catch (e) { /* 后台短暂不可用时保留已加载的数据。 */ }
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
  sseReconnectTimer = null
  if (sseAbort) { sseAbort.abort(); sseAbort = null }
  let token = ''
  try { token = await window.pywebview?.api?.get_api_token() || '' } catch (e) { /* bridge 暂时不可用时按无 token 处理。 */ }
  if (!token) {
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
    const res = await fetch('http://127.0.0.1:19198/api/events', {
      headers: { Authorization: 'Bearer ' + token },
      signal: abort.signal,
    })
    if (!res.ok || !res.body) throw new Error('SSE HTTP ' + res.status)
    sseRetry = 0
    consumeSSE(res, abort)
  } catch (e) {
    if (abort.signal.aborted) return
    scheduleSSEReconnect()
  }
}

function scheduleSSEReconnect() {
  sseRetry++
  if (sseRetry > SSE_MAX) updateStatus({ engine_running: false })
  // 长时间休眠、WebView 网络栈重置或 token 重新发布期间都可能连续失败，超过阈值仍会继续按退避重连。
  const delay = Math.min(1000 * 2 ** Math.min(sseRetry - 1, 4), 15000)
  if (sseReconnectTimer) clearTimeout(sseReconnectTimer)
  sseReconnectTimer = setTimeout(connectSSE, delay)
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
    store.refreshSignal++
  }
  if (RULE_EVENTS.includes(eventName)) {
    let d = {}
    try { d = JSON.parse(dataText) } catch (err) { return }
    store.engineEvents.push({ name: eventName, data: d, seq: ++sseEventSeq })
    if (store.engineEvents.length > 40) store.engineEvents.splice(0, store.engineEvents.length - 40)
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
    if (sseAbort === abort) sseAbort = null
  }
  scheduleSSEReconnect()
}

// 后台控制服务离线时返回首页，自动化暂停时仍可编辑。
watch(() => store.controllerOnline, (on) => {
  if (!on && ['plugins', 'rules', 'logs', 'security', 'settings'].includes(currentPage.value)) {
    switchPage('home')
  }
})

// 子视图在引擎启停后调用这里暴露的刷新入口。
window.__nmf = { refreshAll, updateStatus, switchPage }

onMounted(async () => {
  initTheme()
  void loadConfigAtStartup()
  const status = await getEngineStatus().catch(() => OFFLINE_STATUS)
  updateStatus(status)
  if (status.api_alive) {
    await refreshAll(status)
    connectSSE()
  }
  setTracked(refreshStatus, 2000)
  setTracked(() => store.refreshSignal++, 30000)
})

onUnmounted(() => {
  if (sseAbort) sseAbort.abort()
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
  <AppDialog />
</template>
