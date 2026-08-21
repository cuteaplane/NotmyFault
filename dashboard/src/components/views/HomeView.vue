<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { store } from '../../lib/store'
import { getEngineStatus, readDiagnostics, hasBridge } from '../../lib/api'
import { snackbar } from '../../lib/notify'
import { useEngineControl } from '../../composables/useEngineControl'

// 引擎启停逻辑与 NavRail 快捷按钮共享 starting、stopping 等状态。
const { starting, stopping, shuttingDown, syncStatus, startEngine, stopEngine, shutdownEngine } = useEngineControl()
const stats = ref({ rules: '-', triggers: '-', actions: '-', pid: '-' })
const diag = ref(null)
let diagTimer = null

const isRunning = computed(() => store.engineStatus.engine_running === true)
const isControllerOnline = computed(() => store.engineStatus.api_alive === true)
const engineState = computed(() => store.engineStatus.engine_state || (isRunning.value ? 'running' : 'offline'))
const isStarting = computed(() => starting.value || engineState.value === 'starting')
const isStopping = computed(() => stopping.value || engineState.value === 'stopping')
// 引擎启动失败或被拒绝时，pausedError 保存后台返回的原因。
const pausedError = computed(() => store.engineStatus.last_error || '')
const showFirstAutomationGuide = computed(() => store.configLoaded
  && isControllerOnline.value
  && !store.configData.rules?.length)

function goSecurity() {
  if (window.__nmf && window.__nmf.switchPage) window.__nmf.switchPage('security')
}

function goAutomations() {
  store.pendingAutomationCreate = true
  window.__nmf?.switchPage?.('rules')
}

async function loadStats() {
  try {
    const s = await getEngineStatus()
    await syncStatus(s)
    if (s.api_alive === true) {
      stats.value = {
        rules: s.rules_count != null ? s.rules_count : '-',
        triggers: s.triggers_count != null ? s.triggers_count : '-',
        actions: s.actions_count != null ? s.actions_count : '-',
        pid: s.pid || '-',
      }
    } else {
      // 引擎未运行时清空统计，运行态数字只来自当前引擎。
      stats.value = { rules: '-', triggers: '-', actions: '-', pid: '-' }
    }
  } catch (e) {
    stats.value = { rules: '-', triggers: '-', actions: '-', pid: '-' }
  }
  loadDiag()
}

async function loadDiag() {
  // 引擎未运行时清空诊断结果，后台请求只在运行状态下进行。
  if (!isRunning.value) { diag.value = null; return }
  diag.value = await readDiagnostics()
}

const diagPlugins = computed(() => {
  const d = diag.value
  if (!d) return { txt: '引擎未启动', cls: 'diag-warn', icon: 'remove' }
  const n = (d.plugin_errors || []).length
  const crashes = d.trigger_crash_details || []
  const parts = []
  if (n) parts.push(n + ' 个加载失败')
  if (crashes.length) {
    const names = [...new Set(crashes.map(c => c.trigger_id))].join('、')
    parts.push('触发器 ' + names + ' 崩溃')
  }
  return parts.length ? { txt: parts.join(' · '), cls: 'diag-err', icon: 'error' }
                      : { txt: '正常', cls: 'diag-ok', icon: 'check_circle' }
})
const diagRules = computed(() => {
  const d = diag.value
  if (!d) return { txt: '-', cls: '', icon: '' }
  const n = (d.rule_issues || []).length
  return n ? { txt: n + ' 条规则有问题', cls: 'diag-warn', icon: 'warning' }
           : { txt: '正常', cls: 'diag-ok', icon: 'check_circle' }
})
const diagActions = computed(() => {
  const d = diag.value
  if (!d) return { txt: '-', cls: '', icon: '' }
  const f = d.action_fails || 0
  if (f === 0) {
    const total = (d.action_ok || 0) + (d.action_fails || 0)
    return { txt: d.action_ok !== undefined ? '已执行 ' + total + ' 次' : '正常', cls: 'diag-ok', icon: 'check_circle' }
  }
  return { txt: f + ' 次失败', cls: 'diag-err', icon: 'error' }
})
const diagErrors = computed(() => {
  const d = diag.value
  if (!d) return { txt: '-', cls: '', icon: '' }
  const ec = d.error_count || 0, wc = d.warn_count || 0
  return { txt: ec + ' 错误 · ' + wc + ' 警告', errCls: ec > 0 ? 'diag-err' : 'diag-ok', warnCls: wc > 0 ? 'diag-warn' : 'diag-ok', ec, wc }
})
const diagLines = computed(() => {
  const d = diag.value
  if (!d) return []
  const lines = []
  ;(d.last_errors || []).forEach(e => lines.push({ cls: 'diag-err', icon: 'error', text: e }))
  ;(d.last_warns || []).forEach(w => lines.push({ cls: 'diag-warn', icon: 'warning', text: w }))
  return lines
})

function refreshHome() {
  loadStats()
  snackbar('已刷新')
}

function syncStats(status) {
  stats.value = status.api_alive === true
    ? {
        rules: status.rules_count != null ? status.rules_count : '-',
        triggers: status.triggers_count != null ? status.triggers_count : '-',
        actions: status.actions_count != null ? status.actions_count : '-',
        pid: status.pid || '-',
      }
    : { rules: '-', triggers: '-', actions: '-', pid: '-' }
}

watch(() => store.engineStatus, (status) => {
  syncStats(status)
  loadDiag()
}, { immediate: true, deep: true })

onMounted(() => {
  if (hasBridge()) diagTimer = setInterval(loadDiag, 30000)
})
onUnmounted(() => { if (diagTimer) clearInterval(diagTimer) })
// SSE 事件到达时重新读取统计，并由 loadStats 更新诊断。
watch(() => store.refreshSignal, () => loadStats())
// 引擎停止时清空诊断，启动后立即读取统计。
watch(isRunning, (running) => {
  if (!running) {
    diag.value = null
  } else {
    loadStats()
  }
})
</script>

<template>
  <section class="page active dashboard-home">
    <div class="page-head"><h2>首页</h2><div class="actions">
      <button class="btn btn-outlined" @click="refreshHome"><span class="material-symbols-outlined">refresh</span>刷新</button>
    </div></div>

    <section v-if="showFirstAutomationGuide" class="dashboard-first-run">
      <span class="material-symbols-outlined">account_tree</span>
      <div>
        <small>第一次使用</small>
        <h3>创建第一条自动化</h3>
        <p>前往“自动化”页，从常见用途开始，或者自己指定什么时候开始、接着做什么。</p>
      </div>
      <button class="btn btn-filled" @click="goAutomations">前往自动化<span class="material-symbols-outlined">arrow_forward</span></button>
    </section>

    <h2 class="dashboard-section-title">引擎状态</h2>

    <div class="apatch-hero" :class="isStarting ? 'starting' : isStopping ? 'stopping' : isRunning ? 'running' : isControllerOnline ? 'stopped' : 'offline'">
      <div class="hero-left">
        <div class="hero-icon">
          <span v-if="isStarting || isStopping" class="spinner"></span>
          <span v-else class="material-symbols-outlined">{{ isRunning ? 'task_alt' : isControllerOnline ? 'pause_circle' : 'cloud_off' }}</span>
        </div>
        <div>
          <h3>{{ isStarting ? '启动中…' : isStopping ? '暂停中…' : isRunning ? '运行中' : isControllerOnline ? '已暂停' : '引擎未运行' }}</h3>
        </div>
      </div>
      <!-- 按钮区域在启动、运行、停止和关闭中只显示一个状态。 -->
      <div class="actions">
        <button v-if="!isRunning && !isStarting && !isStopping" class="btn hero-control hero-control-primary" @click="startEngine">
          <span class="material-symbols-outlined">play_arrow</span>{{ isControllerOnline ? '启动自动化' : '启动引擎' }}</button>
        <button v-if="isStarting" class="btn hero-control hero-control-primary" disabled>
          <span class="spinner"></span>启动中...</button>
        <button v-if="isRunning && !isStopping" class="btn hero-control hero-control-primary" @click="stopEngine">
          <span class="material-symbols-outlined">pause</span>暂停自动化</button>
        <button v-if="isStopping" class="btn hero-control hero-control-primary" disabled>
          <span class="spinner"></span>暂停中...</button>
        <!-- 彻底停止按钮只在暂停状态出现，运行时先显示暂停按钮。 -->
        <button v-if="isControllerOnline && !isRunning && !isStarting && !isStopping && !shuttingDown"
          class="btn hero-control hero-control-danger" @click="shutdownEngine">
          <span class="material-symbols-outlined">power_settings_new</span>彻底停止引擎</button>
        <button v-if="isControllerOnline && !isRunning && !isStarting && !isStopping && shuttingDown"
          class="btn hero-control hero-control-danger" disabled>
          <span class="spinner"></span>停止中...</button>
      </div>
    </div>

    <!-- 启动被拒绝时显示后台返回的配置错误。 -->
    <div v-if="isControllerOnline && !isRunning && !isStarting && pausedError" class="mt-3 flex items-start gap-3 rounded-lg border border-error/40 bg-error/10 p-4">
      <span class="material-symbols-outlined text-error">shield_person</span>
      <div class="min-w-0 flex-1">
        <b class="text-error">引擎启动被拒绝</b>
        <p class="mt-0.5 break-all text-body-s text-on-surface-variant">{{ pausedError }}</p>
        <button class="btn btn-outlined mt-2" @click="goSecurity">
          <span class="material-symbols-outlined">security</span>前往安全页查看配置并处理
        </button>
      </div>
    </div>

    <!-- 引擎运行时显示统计、诊断和操作区。 -->
    <template v-if="isControllerOnline">
      <section v-if="isRunning" class="dashboard-card dashboard-overview">
        <header class="dashboard-card-head">
          <div>
            <h3>运行概览</h3>
          </div>
          <span class="dashboard-live"><i></i>运行中</span>
        </header>
        <div class="dashboard-metrics">
          <div v-for="s in [
              { icon: 'rule', val: stats.rules, lbl: '规则数量' },
              { icon: 'memory', val: stats.triggers, lbl: '活跃触发器' },
              { icon: 'bolt', val: stats.actions, lbl: '动作类型' },
              { icon: 'dns', val: stats.pid, lbl: '进程 PID' },
            ]" :key="s.lbl" class="dashboard-metric">
            <span class="material-symbols-outlined dashboard-metric-icon">{{ s.icon }}</span>
            <div>
              <strong>{{ s.val }}</strong>
              <span>{{ s.lbl }}</span>
            </div>
          </div>
        </div>
      </section>

      <section class="dashboard-card dashboard-diagnostics">
        <header class="dashboard-card-head">
          <div>
            <h3>引擎诊断</h3>
          </div>
        </header>
        <div class="dashboard-diagnostic-grid">
          <div v-for="row in [
              { key: '插件状态', d: diagPlugins },
              { key: '规则状态', d: diagRules },
              { key: '动作执行', d: diagActions },
              { key: '错误 / 警告', d: { txt: diagErrors.txt, cls: diagErrors.ec > 0 ? 'diag-err' : 'diag-ok', icon: diagErrors.ec > 0 ? 'error' : 'check_circle' } },
            ]" :key="row.key" class="dashboard-diagnostic-row">
            <span>{{ row.key }}</span>
            <span class="dashboard-diagnostic-value" :class="row.d.cls">
              <span v-if="row.d.icon" class="material-symbols-outlined">{{ row.d.icon }}</span>{{ row.d.txt }}
            </span>
          </div>
        </div>
        <div v-if="diagLines.length" class="dashboard-diagnostic-detail">
          <div v-for="(l, i) in diagLines" :key="i" :class="l.cls">
            <span class="material-symbols-outlined">{{ l.icon }}</span>
            <span>{{ l.text }}</span>
          </div>
        </div>
        <div v-else class="dashboard-diagnostic-empty diag-ok">
          <span class="material-symbols-outlined">check_circle</span>暂无异常记录
        </div>
      </section>
    </template>

  </section>
</template>
