<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { getEngineStatus, hasBridge, listRuns, readDiagnostics } from '../../lib/api'
import { store } from '../../lib/store'
import { snackbar } from '../../lib/notify'
import { useEngineControl } from '../../composables/useEngineControl'

const { starting, stopping, shuttingDown, syncStatus, startEngine, stopEngine, shutdownEngine } = useEngineControl()
const stats = ref({ rules: '-', triggers: '-', actions: '-' })
const schedulerText = ref('0 / 0')
const diag = ref(null)
const recentRuns = ref([])
const runsError = ref('')
let statsLoading = false
let disposed = false
let diagTimer = null

const isRunning = computed(() => store.engineStatus.engine_running === true)
const isControllerOnline = computed(() => store.engineStatus.api_alive === true)
const engineState = computed(() => store.engineStatus.engine_state || (isRunning.value ? 'running' : 'offline'))
const isStarting = computed(() => starting.value || engineState.value === 'starting')
const isStopping = computed(() => stopping.value || engineState.value === 'stopping')
const pausedError = computed(() => store.engineStatus.last_error || '')
const showFirstAutomationGuide = computed(() => store.configLoaded
  && isControllerOnline.value
  && !store.configData.rules?.length)
const startupPlugin = computed(() => {
  const match = pausedError.value.match(/[\\/](?:user_plugins|plugins)[\\/]([^\\/:]+)[\\/]/i)
  if (!match) return null
  const id = match[1]
  const kind = store.pluginsData.triggers?.[id] ? 'trigger' : 'action'
  const meta = store.pluginsData[kind === 'trigger' ? 'triggers' : 'actions']?.[id]
  return meta?.origin === 'user' ? { kind, id } : null
})
const installationError = computed(() => /缺少签名文件|核心文件完整性校验失败|安装文件/.test(pausedError.value))
const diagLines = computed(() => {
  if (!diag.value) return []
  return [
    ...(diag.value.last_errors || []).map(text => ({ level: 'error', icon: 'error', text })),
    ...(diag.value.last_warns || []).map(text => ({ level: 'warning', icon: 'warning', text })),
  ].slice(0, 3)
})
const engineLabel = computed(() => {
  if (isStarting.value) return '启动中…'
  if (isStopping.value) return '暂停中…'
  if (isRunning.value) return '运行中'
  return isControllerOnline.value ? '已暂停' : '引擎未运行'
})
const engineDescription = computed(() => {
  if (isRunning.value) return `${stats.value.rules} 条规则 · ${schedulerText.value} 正在运行 / 排队`
  if (isControllerOnline.value) return '自动化当前不会触发，仍可编辑规则和查看记录。'
  return '后台控制服务尚未运行。'
})

const runStatusMeta = {
  running: { label: '运行中', icon: 'progress_activity' },
  deferred: { label: '等待重试', icon: 'schedule' },
  succeeded: { label: '成功', icon: 'check_circle' },
  failed: { label: '失败', icon: 'error' },
  cancelled: { label: '已停止', icon: 'stop_circle' },
}

function runMeta(status) {
  return runStatusMeta[status] || runStatusMeta.running
}

function formatRunTime(timestamp) {
  if (!timestamp) return '时间未知'
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  }).format(new Date(timestamp * 1000))
}

function formatDuration(milliseconds) {
  if (milliseconds === null || milliseconds === undefined) return '尚未完成'
  if (milliseconds < 1000) return `${milliseconds} 毫秒`
  if (milliseconds < 60000) return `${(milliseconds / 1000).toFixed(1)} 秒`
  return `${Math.floor(milliseconds / 60000)} 分 ${Math.round(milliseconds % 60000 / 1000)} 秒`
}

function goAutomations() {
  store.pendingAutomationCreate = true
  store.pendingAutomationSection = 'rules'
  window.__nmf?.switchPage?.('rules')
}

function goLibrary() {
  store.pendingAutomationSection = 'rules'
  window.__nmf?.switchPage?.('rules')
}

function goPlugins() {
  window.__nmf?.switchPage?.('plugins')
}

function goRuns(runId = '') {
  store.pendingRunId = runId
  store.pendingAutomationSection = 'runs'
  window.__nmf?.switchPage?.('rules')
}

function handleStartupIssue() {
  if (startupPlugin.value) {
    store.pendingPluginFocus = {
      ...startupPlugin.value,
      state: 'unavailable',
      reason: pausedError.value,
    }
    window.__nmf?.switchPage?.('plugins')
    return
  }
  store.pendingSettingsSection = 'security'
  window.__nmf?.switchPage?.('settings')
}

function goDiagnostics() {
  store.pendingSettingsSection = 'diagnostics'
  window.__nmf?.switchPage?.('settings')
}

async function loadStats() {
  if (statsLoading || disposed) return
  statsLoading = true
  try {
    const status = await getEngineStatus()
    if (disposed) return
    await syncStatus(status)
    const scheduler = status.scheduler || {}
    schedulerText.value = `${scheduler.running ?? 0} / ${scheduler.queued ?? 0}`
    stats.value = status.api_alive === true
      ? {
          rules: status.rules_count ?? '-',
          triggers: status.triggers_count ?? '-',
          actions: status.actions_count ?? '-',
        }
      : { rules: '-', triggers: '-', actions: '-' }
    if (status.api_alive === true) {
      try {
        recentRuns.value = await listRuns(5)
        runsError.value = ''
      } catch (error) {
        runsError.value = error.message
      }
    }
    diag.value = isRunning.value ? await readDiagnostics() : null
  } catch {
    stats.value = { rules: '-', triggers: '-', actions: '-' }
  } finally {
    statsLoading = false
  }
}

function refreshHome() {
  loadStats()
  snackbar('已刷新')
}

watch(() => store.engineStatus, status => {
  stats.value = status.api_alive === true
    ? {
        rules: status.rules_count ?? '-',
        triggers: status.triggers_count ?? '-',
        actions: status.actions_count ?? '-',
      }
    : { rules: '-', triggers: '-', actions: '-' }
}, { immediate: true })
watch(() => store.refreshSignal, loadStats)
watch(isControllerOnline, online => {
  if (online) loadStats()
})
watch(isRunning, running => {
  if (!running) diag.value = null
  else loadStats()
})

onMounted(() => {
  if (isControllerOnline.value) loadStats()
  if (hasBridge()) diagTimer = setInterval(loadStats, 30000)
})
onUnmounted(() => { disposed = true; if (diagTimer) clearInterval(diagTimer) })
</script>

<template>
  <section class="page active dashboard-home dashboard-home-compact">
    <div class="page-head">
      <div><h2>首页</h2><p class="page-subtitle">查看这台电脑的自动化状态与最近活动</p></div>
      <div class="actions"><button class="icon-btn" aria-label="刷新首页" title="刷新首页" @click="refreshHome"><span class="material-symbols-outlined">refresh</span></button><button v-if="isControllerOnline && !showFirstAutomationGuide" class="btn btn-filled" @click="goAutomations"><span class="material-symbols-outlined">add</span>创建自动化</button></div>
    </div>

    <section v-if="showFirstAutomationGuide" class="dashboard-first-run dashboard-first-run-compact">
      <span class="material-symbols-outlined">account_tree</span>
      <div><small>开始使用</small><h3>创建第一条自动化</h3><p>从常见用途开始，或自己指定触发条件和动作。</p></div>
      <button class="btn btn-filled" @click="goAutomations">创建自动化</button>
    </section>

    <section class="home-engine-strip" :class="engineState">
      <div class="home-engine-state">
        <span class="home-engine-icon material-symbols-outlined">{{ isRunning ? 'task_alt' : isControllerOnline ? 'pause_circle' : 'cloud_off' }}</span>
        <div><small>引擎状态</small><h3>{{ engineLabel }}</h3><p>{{ engineDescription }}</p></div>
      </div>
      <div class="home-engine-metrics">
        <span><strong>{{ stats.rules }}</strong>规则</span>
        <span><strong>{{ stats.triggers }}</strong>使用的触发器</span>
        <span><strong>{{ stats.actions }}</strong>使用的动作</span>
      </div>
      <div class="actions home-engine-actions">
        <button v-if="!isRunning && !isStarting && !isStopping" class="btn btn-filled" @click="startEngine">
          <span class="material-symbols-outlined">play_arrow</span>{{ isControllerOnline ? '启动自动化' : '启动引擎' }}
        </button>
        <button v-else-if="isStarting" class="btn btn-filled" disabled><span class="spinner"></span>启动中…</button>
        <button v-else-if="isRunning && !isStopping" class="btn btn-tonal" @click="stopEngine"><span class="material-symbols-outlined">pause</span>暂停</button>
        <button v-else class="btn btn-tonal" disabled><span class="spinner"></span>暂停中…</button>
        <button v-if="isControllerOnline && !isRunning && !isStarting && !isStopping" class="btn btn-text btn-danger" :disabled="shuttingDown" @click="shutdownEngine">
          {{ shuttingDown ? '停止中…' : '彻底停止' }}
        </button>
      </div>
    </section>

    <section v-if="isControllerOnline && !isRunning && !isStarting && pausedError" class="engine-startup-alert home-actionable-alert">
      <span class="material-symbols-outlined engine-startup-alert-icon">report</span>
      <div class="engine-startup-alert-copy"><b>{{ installationError ? '安装文件异常，请重新安装 NotmyFault' : '引擎启动失败' }}</b><p>{{ installationError ? '打开安全设置查看安装文件检查结果和重新安装说明。' : pausedError }}</p></div>
      <button class="btn btn-outlined" @click="handleStartupIssue">
        <span class="material-symbols-outlined">{{ startupPlugin ? 'extension' : 'security' }}</span>
        {{ startupPlugin ? `查看 ${startupPlugin.id}` : installationError ? '查看重新安装说明' : '打开安全设置' }}
      </button>
    </section>

    <nav v-if="isControllerOnline" class="home-shortcuts" aria-label="常用操作">
      <button @click="goLibrary"><span class="material-symbols-outlined">account_tree</span><span><b>管理自动化</b><small>编辑规则与测试动作</small></span><span class="material-symbols-outlined">chevron_right</span></button>
      <button @click="goPlugins"><span class="material-symbols-outlined">extension</span><span><b>查看插件</b><small>扩展触发条件与动作</small></span><span class="material-symbols-outlined">chevron_right</span></button>
    </nav>

    <div class="home-content-grid">
      <section class="dashboard-card home-recent-runs">
        <header class="dashboard-card-head">
          <div><h3>最近运行</h3><p>最新的自动化执行结果</p></div>
          <button class="btn btn-text" @click="goRuns()">查看全部<span class="material-symbols-outlined">arrow_forward</span></button>
        </header>
        <p v-if="runsError" class="danger-text" role="status">{{ runsError }}，可刷新重试。</p>
        <div v-if="recentRuns.length" class="home-run-list">
          <button v-for="run in recentRuns" :key="run.run_id" class="home-run-row" @click="goRuns(run.run_id)">
            <span class="material-symbols-outlined" :class="`run-${run.status}`">{{ runMeta(run.status).icon }}</span>
            <span class="home-run-copy"><b>{{ run.rule_name || '未命名自动化' }}</b><small>{{ formatRunTime(run.started_at) }} · {{ formatDuration(run.duration_ms) }}</small></span>
            <span class="home-run-status" :class="`run-${run.status}`">{{ runMeta(run.status).label }}</span>
            <span class="material-symbols-outlined">chevron_right</span>
          </button>
        </div>
        <div v-else class="home-empty-state"><span class="material-symbols-outlined">history</span><span>还没有运行记录</span></div>
      </section>

      <section class="dashboard-card home-diagnostic-card">
        <header class="dashboard-card-head">
          <div><h3>需要关注</h3><p>引擎与插件的最近异常</p></div>
          <button class="btn btn-text" @click="goDiagnostics">系统日志<span class="material-symbols-outlined">arrow_forward</span></button>
        </header>
        <div v-if="diagLines.length" class="home-issue-list">
          <button v-for="(line, index) in diagLines" :key="index" class="home-issue-row" @click="goDiagnostics">
            <span class="material-symbols-outlined" :class="line.level">{{ line.icon }}</span>
            <span>{{ line.text }}</span>
            <span class="material-symbols-outlined">chevron_right</span>
          </button>
        </div>
        <div v-else class="home-empty-state" :class="{ ok: isRunning && diag }"><span class="material-symbols-outlined">{{ isRunning && diag ? 'check_circle' : 'monitor_heart' }}</span><span>{{ isRunning && diag ? '暂无异常记录' : isRunning ? '暂未获取系统状态，可刷新重试' : '启动自动化后查看系统状态' }}</span></div>
      </section>
    </div>
  </section>
</template>
