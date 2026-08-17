<script setup>
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { listRuns, readLogEntries, readLogFileEntries, listLogFiles } from '../../lib/api'
import { store } from '../../lib/store'
import { snackbar } from '../../lib/notify'
import { confirmDialog } from '../../lib/dialog'
import { buildRunExport } from '../../lib/runExport'

const activeTab = ref('runs')
const runs = ref([])
const entries = ref([])
const files = ref([])
const currentFile = ref('')
const statusFilter = ref('all')
const timeFilter = ref('all')
const actionFilter = ref('all')
const levelFilter = ref('all')
const searchText = ref('')
const autoRefresh = ref(true)
const expandedRunId = ref('')
let timer = null

const isLatest = computed(() => currentFile.value === '')
const levelMeta = {
  ERROR: { label: '错误', cls: 'log-err' },
  WARN: { label: '警告', cls: 'log-warn' },
  INFO: { label: '信息', cls: 'log-info' },
}
const runStatusMeta = {
  running: { label: '运行中', icon: 'progress_activity', cls: 'running' },
  deferred: { label: '等待重试', icon: 'schedule', cls: 'deferred' },
  succeeded: { label: '成功', icon: 'check_circle', cls: 'succeeded' },
  failed: { label: '失败', icon: 'error', cls: 'failed' },
  cancelled: { label: '已停止', icon: 'stop_circle', cls: 'cancelled' },
}

const levelCounts = computed(() => {
  const counts = { ERROR: 0, WARN: 0, INFO: 0 }
  entries.value.forEach(entry => {
    if (counts[entry.level] !== undefined) counts[entry.level]++
  })
  return counts
})

const runCounts = computed(() => {
  const counts = { succeeded: 0, failed: 0, cancelled: 0, running: 0, deferred: 0 }
  runs.value.forEach(run => {
    if (counts[run.status] !== undefined) counts[run.status]++
  })
  return counts
})

const actionOptions = computed(() => [...new Set(
  runs.value.flatMap(run => (run.steps || []).map(step => step.action_type).filter(Boolean)),
)].sort((left, right) => actionName(left).localeCompare(actionName(right), 'zh-CN')))

const hasRunFilters = computed(() => statusFilter.value !== 'all'
  || timeFilter.value !== 'all'
  || actionFilter.value !== 'all'
  || searchText.value.trim() !== '')

const filteredRuns = computed(() => {
  const keyword = searchText.value.trim().toLowerCase()
  const rangeSeconds = { '24h': 86400, '7d': 604800, '30d': 2592000 }[timeFilter.value] || 0
  const cutoff = rangeSeconds ? Date.now() / 1000 - rangeSeconds : 0
  return runs.value.filter(run => {
    if (statusFilter.value === 'pending' && !['running', 'deferred'].includes(run.status)) return false
    if (!['all', 'pending'].includes(statusFilter.value) && run.status !== statusFilter.value) return false
    if (cutoff && Number(run.started_at || 0) < cutoff) return false
    if (actionFilter.value !== 'all'
      && !(run.steps || []).some(step => step.action_type === actionFilter.value)) return false
    if (!keyword) return true
    return [
      run.rule_name,
      run.event_type,
      triggerName(run.event_type),
      run.run_id,
      ...(run.steps || []).flatMap(step => [step.action_type, actionName(step.action_type)]),
    ]
      .some(value => String(value || '').toLowerCase().includes(keyword))
  })
})

const filteredEntries = computed(() => {
  const keyword = searchText.value.trim().toLowerCase()
  return entries.value.filter(entry => {
    if (levelFilter.value !== 'all' && entry.level !== levelFilter.value) return false
    return !keyword || String(entry.text || '').toLowerCase().includes(keyword)
  })
})

function runMeta(status) {
  return runStatusMeta[status] || runStatusMeta.running
}

function actionName(type) {
  return store.schema.actions?.[type]?.name || type || '未知动作'
}

function triggerName(type) {
  if (type === 'manual') return '手动运行'
  return store.schema.triggers?.[type]?.name || type || '未知触发方式'
}

function matchingRule(run) {
  const rules = store.configData.rules || []
  return rules.find(rule => run.rule_id && rule.rule_id === run.rule_id)
    || rules.find(rule => rule.name === run.rule_name)
}

function runScope(run) {
  if (!run.start_step_id && !run.end_step_id) return ''
  const actions = matchingRule(run)?.actions || []
  const start = run.start_step_id
    ? actions.findIndex(action => action.binding_id === run.start_step_id) + 1
    : 1
  const end = run.end_step_id
    ? actions.findIndex(action => action.binding_id === run.end_step_id) + 1
    : actions.length
  if (start <= 0 || end <= 0) return `局部测试 · ${run.action_count} 个动作`
  return `局部测试 · 动作 ${start}–${end} / 共 ${actions.length} 个`
}

function stepNumber(run, step, fallback) {
  const index = (matchingRule(run)?.actions || [])
    .findIndex(action => action.binding_id === step.step_id)
  return index >= 0 ? index + 1 : fallback
}

function formatTime(timestamp) {
  if (!timestamp) return '时间未知'
  const date = new Date(timestamp * 1000)
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
  }).format(date)
}

function formatMtime(mtime) {
  if (!mtime) return ''
  const date = new Date(mtime * 1000)
  const pad = number => String(number).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

function formatDuration(milliseconds) {
  if (milliseconds === null || milliseconds === undefined) return '尚未完成'
  if (milliseconds < 1000) return `${milliseconds} 毫秒`
  if (milliseconds < 60000) return `${(milliseconds / 1000).toFixed(milliseconds < 10000 ? 1 : 0)} 秒`
  return `${Math.floor(milliseconds / 60000)} 分 ${Math.round(milliseconds % 60000 / 1000)} 秒`
}

function errorText(error) {
  if (!error) return ''
  if (typeof error === 'string') return error.trim().split('\n').at(-1)
  return error.message || error.code || JSON.stringify(error)
}

function toggleRun(runId) {
  expandedRunId.value = expandedRunId.value === runId ? '' : runId
}

function jumpToRule(run) {
  store.pendingRuleId = run.rule_id || ''
  store.pendingRuleName = run.rule_name || ''
  store.pendingStepId = ''
  window.__nmf?.switchPage?.('rules')
}

function jumpToStep(run, step) {
  store.pendingRuleId = run.rule_id || ''
  store.pendingRuleName = run.rule_name || ''
  store.pendingStepId = step.step_id || ''
  window.__nmf?.switchPage?.('rules')
}

function isNearBottom() {
  const element = document.getElementById('logViewer')
  if (!element) return true
  return element.scrollTop + element.clientHeight >= element.scrollHeight - 48
}

function scrollToEnd() {
  const element = document.getElementById('logViewer')
  if (element) element.scrollTop = element.scrollHeight
}

async function loadFiles() {
  const list = await listLogFiles()
  files.value = Array.isArray(list) ? list.slice(1) : []
}

async function refreshRuns() {
  runs.value = await listRuns(200)
}

async function refreshLogs() {
  const nearBottom = isNearBottom()
  const list = isLatest.value
    ? await readLogEntries(600)
    : await readLogFileEntries(currentFile.value, 600)
  entries.value = Array.isArray(list) ? list : []
  await nextTick()
  if (nearBottom) scrollToEnd()
}

async function refresh() {
  if (activeTab.value === 'runs') await refreshRuns()
  else await refreshLogs()
}

async function copyLog() {
  try {
    const text = filteredEntries.value
      .map(entry => `[${entry.ts || ''}] [${entry.level || 'INFO'}] ${entry.text || ''}`)
      .join('\n')
    await navigator.clipboard.writeText(text)
    snackbar('日志已复制到剪贴板')
  } catch (error) {
    snackbar('复制失败：' + error.message)
  }
}

function clearRunFilters() {
  statusFilter.value = 'all'
  timeFilter.value = 'all'
  actionFilter.value = 'all'
  searchText.value = ''
}

function exportFileName(date) {
  const pad = value => String(value).padStart(2, '0')
  return `NotmyFault-run-diagnostics-${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}.json`
}

async function exportRuns() {
  const count = filteredRuns.value.length
  if (!count) return
  const confirmed = await confirmDialog(
    `导出这 ${count} 条运行记录？`,
    '文件包含规则名、触发方式、步骤状态、耗时和已脱敏的输入输出摘要；不包含触发输入、动作参数、动作返回值或测试期望值。',
    '导出 JSON',
  )
  if (!confirmed) return
  try {
    const now = new Date()
    const data = buildRunExport(filteredRuns.value, {
      status: statusFilter.value,
      timeRange: timeFilter.value,
      actionType: actionFilter.value,
      search: searchText.value.trim(),
    }, now)
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json;charset=utf-8' })
    const url = window.URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = exportFileName(now)
    document.body.appendChild(link)
    link.click()
    link.remove()
    window.URL.revokeObjectURL(url)
    snackbar(`已导出 ${count} 条脱敏运行记录`)
  } catch (error) {
    snackbar('导出失败：' + error.message)
  }
}

function onFileChange() {
  levelFilter.value = 'all'
  refreshLogs()
}

function syncTimer() {
  const shouldRefresh = autoRefresh.value && (activeTab.value === 'runs' || isLatest.value)
  if (shouldRefresh && !timer) timer = setInterval(refresh, 3000)
  else if (!shouldRefresh && timer) {
    clearInterval(timer)
    timer = null
  }
}

watch([autoRefresh, isLatest, activeTab], syncTimer)
watch(activeTab, () => {
  searchText.value = ''
  refresh()
})
watch(() => store.refreshSignal, () => {
  if (activeTab.value === 'runs') refreshRuns()
})

onMounted(async () => {
  await Promise.all([loadFiles(), refreshRuns()])
  if (store.pendingRunId) {
    expandedRunId.value = store.pendingRunId
    store.pendingRunId = ''
  }
  syncTimer()
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

<template>
  <section class="page active run-center-page">
    <div class="page-head">
      <div>
        <h2>运行与日志</h2>
        <p class="page-subtitle">先看每次自动化的结果，需要排障时再查看原始日志。</p>
      </div>
      <div class="actions">
        <label class="check-row"><input type="checkbox" v-model="autoRefresh">自动刷新</label>
        <button v-if="activeTab === 'logs'" class="btn btn-outlined" @click="copyLog">
          <span class="material-symbols-outlined">content_copy</span>复制日志
        </button>
        <button class="btn btn-outlined" @click="refresh">
          <span class="material-symbols-outlined">refresh</span>刷新
        </button>
      </div>
    </div>

    <div class="tabs run-center-tabs" role="tablist" aria-label="运行记录和原始日志">
      <button class="tab" :class="{ active: activeTab === 'runs' }" role="tab" @click="activeTab = 'runs'">运行记录</button>
      <button class="tab" :class="{ active: activeTab === 'logs' }" role="tab" @click="activeTab = 'logs'">原始日志</button>
    </div>

    <template v-if="activeTab === 'runs'">
      <div class="run-summary-grid">
        <button class="run-summary-card" :class="{ active: statusFilter === 'all' }" @click="statusFilter = 'all'">
          <span>最近运行</span><strong>{{ runs.length }}</strong>
        </button>
        <button class="run-summary-card succeeded" :class="{ active: statusFilter === 'succeeded' }" @click="statusFilter = 'succeeded'">
          <span>成功</span><strong>{{ runCounts.succeeded }}</strong>
        </button>
        <button class="run-summary-card failed" :class="{ active: statusFilter === 'failed' }" @click="statusFilter = 'failed'">
          <span>失败</span><strong>{{ runCounts.failed }}</strong>
        </button>
        <button class="run-summary-card cancelled" :class="{ active: statusFilter === 'cancelled' }" @click="statusFilter = 'cancelled'">
          <span>已停止</span><strong>{{ runCounts.cancelled }}</strong>
        </button>
        <button class="run-summary-card pending" :class="{ active: statusFilter === 'pending' }" @click="statusFilter = 'pending'">
          <span>进行中</span><strong>{{ runCounts.running + runCounts.deferred }}</strong>
        </button>
      </div>

      <div class="run-filter-panel">
        <div class="run-filter-sentence">
          <span>查看</span>
          <select v-model="timeFilter" class="select run-time-filter" aria-label="运行时间范围">
            <option value="all">所有时间</option>
            <option value="24h">最近 24 小时</option>
            <option value="7d">最近 7 天</option>
            <option value="30d">最近 30 天</option>
          </select>
          <span>包含</span>
          <select v-model="actionFilter" class="select run-action-filter" aria-label="运行包含的动作">
            <option value="all">任何动作</option>
            <option v-for="type in actionOptions" :key="type" :value="type">{{ actionName(type) }}</option>
          </select>
          <span>的运行</span>
        </div>
        <div class="run-filter-tools">
          <div class="log-search-wrap run-search-wrap">
            <span class="material-symbols-outlined">search</span>
            <input v-model="searchText" type="search" class="text-field log-search" placeholder="搜索自动化、触发方式或动作">
          </div>
          <button class="btn btn-outlined run-export-btn" :disabled="!filteredRuns.length" @click="exportRuns">
            <span class="material-symbols-outlined">download</span>导出这些记录
          </button>
        </div>
        <div class="run-filter-result">
          <span class="run-filter-note">显示 {{ filteredRuns.length }} 条</span>
          <button v-if="hasRunFilters" class="btn btn-text btn-sm" @click="clearRunFilters">清除筛选</button>
        </div>
      </div>

      <div v-if="!filteredRuns.length" class="empty-state run-empty">
        <span class="material-symbols-outlined">history</span>
        <h3>{{ runs.length ? '没有符合条件的运行' : '还没有运行记录' }}</h3>
        <p>{{ runs.length ? '换一个筛选条件或搜索词。' : '规则首次运行后，结果和每一步耗时会显示在这里。' }}</p>
      </div>

      <div v-else class="run-list">
        <article v-for="run in filteredRuns" :key="run.run_id" class="run-card" :class="runMeta(run.status).cls">
          <button class="run-card-main" @click="toggleRun(run.run_id)" :aria-expanded="expandedRunId === run.run_id">
            <span class="run-status-icon material-symbols-outlined">{{ runMeta(run.status).icon }}</span>
            <span class="run-card-copy">
              <span class="run-card-title">{{ run.rule_name }}</span>
              <span class="run-card-meta">{{ triggerName(run.event_type) }} · {{ formatTime(run.started_at) }} · {{ formatDuration(run.duration_ms) }}</span>
              <span v-if="runScope(run)" class="run-scope-label">{{ runScope(run) }}</span>
            </span>
            <span class="run-status-label">{{ runMeta(run.status).label }}</span>
            <span class="material-symbols-outlined run-expand">{{ expandedRunId === run.run_id ? 'expand_less' : 'expand_more' }}</span>
          </button>

          <div v-if="expandedRunId === run.run_id" class="run-details">
            <div v-if="run.status === 'deferred'" class="run-message deferred">
              <span class="material-symbols-outlined">schedule</span>
              {{ run.deferred_reason || '前置条件尚未满足' }}
              <span v-if="run.retry_after_seconds">，约 {{ run.retry_after_seconds }} 秒后重试</span>
            </div>
            <div v-if="run.status === 'cancelled'" class="run-message cancelled">
              <span class="material-symbols-outlined">stop_circle</span>这次运行已由用户停止
            </div>
            <div v-if="run.error" class="run-message failed">
              <span class="material-symbols-outlined">error</span>{{ errorText(run.error) }}
            </div>
            <div v-if="run.assertions_total" class="run-message" :class="run.assertions_passed === run.assertions_total ? 'assertion-ok' : 'failed'">
              <span class="material-symbols-outlined">fact_check</span>
              结果检查 {{ run.assertions_passed }} / {{ run.assertions_total }} 通过
            </div>

            <div class="run-step-list">
              <div v-for="(step, index) in run.steps" :key="step.step_id" class="run-step" :class="step.status">
                <span class="run-step-index">{{ stepNumber(run, step, index + 1) }}</span>
                <span class="run-step-copy">
                  <strong>{{ actionName(step.action_type) }}</strong>
                  <small v-if="step.status === 'skipped'">{{ step.reason || '已跳过' }}</small>
                  <small v-else-if="step.status === 'failed'">{{ errorText(step.error) || '执行失败' }}</small>
                  <small v-else-if="step.status === 'cancelled'">已安全停止</small>
                  <small v-else-if="step.status === 'timed_out'">超过允许的运行时间</small>
                  <small v-else>{{ formatDuration(step.duration_ms) }}<template v-if="step.attempt > 1"> · 第 {{ step.attempt }} 次尝试</template></small>
                  <div v-if="step.input_summary?.length || step.output_summary?.length" class="step-summary-strip run-step-summary">
                    <div v-if="step.input_summary?.length" class="step-summary-side">
                      <span class="step-summary-kind"><span class="material-symbols-outlined">login</span>输入</span>
                      <span v-for="item in step.input_summary" :key="item.name" class="step-summary-item" :class="{ redacted: item.redacted }"><b>{{ item.label }}</b><small>{{ item.display }}</small></span>
                    </div>
                    <span v-if="step.input_summary?.length && step.output_summary?.length" class="material-symbols-outlined step-summary-arrow">arrow_forward</span>
                    <div v-if="step.output_summary?.length" class="step-summary-side">
                      <span class="step-summary-kind"><span class="material-symbols-outlined">logout</span>输出</span>
                      <span v-for="item in step.output_summary" :key="item.name" class="step-summary-item" :class="{ redacted: item.redacted }"><b>{{ item.label }}</b><small>{{ item.display }}</small></span>
                    </div>
                  </div>
                </span>
                <span class="run-step-actions">
                  <span class="material-symbols-outlined run-step-state">{{ step.status === 'succeeded' ? 'check' : step.status === 'skipped' ? 'skip_next' : step.status === 'cancelled' ? 'stop' : step.status === 'timed_out' ? 'timer_off' : 'close' }}</span>
                  <button v-if="step.status === 'failed'" class="btn btn-text btn-sm run-step-open" @click="jumpToStep(run, step)">编辑这一步</button>
                </span>
              </div>
              <div v-if="!run.steps.length" class="run-no-steps">{{ run.action_count ? '尚未收到动作结果' : '这条规则没有动作' }}</div>
            </div>

            <div v-if="run.assertion_results?.length" class="run-assertion-list">
              <div v-for="(assertion, index) in run.assertion_results" :key="`${assertion.step_id}-${index}`" class="run-assertion" :class="{ passed: assertion.passed }">
                <span class="material-symbols-outlined">{{ assertion.passed ? 'check_circle' : 'cancel' }}</span>
                <span><strong>检查项 {{ index + 1 }}{{ assertion.path?.length ? ` · ${assertion.path.join('.')}` : '' }}</strong><small>{{ assertion.message }}</small></span>
              </div>
            </div>

            <div class="run-card-actions">
              <button class="btn btn-text" @click="jumpToRule(run)"><span class="material-symbols-outlined">edit</span>打开自动化</button>
              <button class="btn btn-text" disabled title="运行记录不保存触发输入"><span class="material-symbols-outlined">replay</span>重新运行</button>
              <code>{{ run.run_id }}</code>
            </div>
          </div>
        </article>
      </div>
    </template>

    <template v-else>
      <div class="log-toolbar">
        <select v-model="currentFile" class="select log-file-select" @change="onFileChange">
          <option value="">最新日志</option>
          <option v-for="file in files" :key="file.name" :value="file.name">{{ file.name }} · {{ formatMtime(file.mtime) }} · {{ file.lines }} 行</option>
        </select>
        <div class="log-chips">
          <button class="log-chip" :class="{ active: levelFilter === 'all' }" @click="levelFilter = 'all'">全部 {{ entries.length }}</button>
          <button class="log-chip log-chip-err" :class="{ active: levelFilter === 'ERROR' }" @click="levelFilter = 'ERROR'">错误 {{ levelCounts.ERROR }}</button>
          <button class="log-chip log-chip-warn" :class="{ active: levelFilter === 'WARN' }" @click="levelFilter = 'WARN'">警告 {{ levelCounts.WARN }}</button>
          <button class="log-chip log-chip-info" :class="{ active: levelFilter === 'INFO' }" @click="levelFilter = 'INFO'">信息 {{ levelCounts.INFO }}</button>
        </div>
        <div class="log-search-wrap">
          <span class="material-symbols-outlined">search</span>
          <input v-model="searchText" type="text" class="text-field log-search" placeholder="搜索日志内容">
        </div>
      </div>

      <div v-if="!filteredEntries.length" class="log-viewer log-empty">
        {{ entries.length ? '没有符合筛选条件的日志' : '（日志为空）' }}
      </div>
      <div v-else class="log-viewer log-entries" id="logViewer">
        <div v-for="(entry, index) in filteredEntries" :key="index" class="log-line" :class="levelMeta[entry.level]?.cls || 'log-info'" :title="entry.data ? JSON.stringify(entry.data) : ''">
          <span class="log-ts">{{ entry.ts }}</span>
          <span class="log-badge">{{ levelMeta[entry.level]?.label || entry.level || '信息' }}</span>
          <span class="log-text">{{ entry.text }}</span>
        </div>
      </div>
      <p v-if="!isLatest" class="log-hint">正在查看历史日志，自动刷新已停用。选择“最新日志”返回实时视图。</p>
    </template>
  </section>
</template>
