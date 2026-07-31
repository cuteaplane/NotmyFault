<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { store } from '../../lib/store'
import { getEngineStatus, readDiagnostics, hasBridge } from '../../lib/api'
import { snackbar } from '../../lib/notify'
import { useEngineControl } from '../../composables/useEngineControl'

// 引擎启动/暂停/重启逻辑与 NavRail 快捷按钮共享（含 busy 状态）
const { starting, stopping, syncStatus, startEngine, stopEngine } = useEngineControl()
const stats = ref({ rules: '-', triggers: '-', actions: '-', pid: '-' })
const diag = ref(null)
let diagTimer = null
const appVersion = __APP_VERSION__

const isRunning = computed(() => store.engineStatus.engine_running === true)
const isControllerOnline = computed(() => store.engineStatus.api_alive === true)
const engineState = computed(() => store.engineStatus.engine_state || (isRunning.value ? 'running' : 'offline'))
const isStarting = computed(() => starting.value || engineState.value === 'starting')
const isStopping = computed(() => stopping.value || engineState.value === 'stopping')
const modeLabel = computed(() => {
  const m = store.engineStatus.security_mode
  return ({ strict: '严格', normal: '标准', permissive: '宽松' })[m] || '-'
})
// 引擎启动失败/被拒绝的原因（如配置签名密钥缺失），暂停态展示警告
const pausedError = computed(() => store.engineStatus.last_error || '')

function goSecurity() {
  if (window.__nmf && window.__nmf.switchPage) window.__nmf.switchPage('security')
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
      // 引擎未运行：清空数据，避免把配置文件的 rules_count 误报为运行态数据
      stats.value = { rules: '-', triggers: '-', actions: '-', pid: '-' }
    }
  } catch (e) {
    stats.value = { rules: '-', triggers: '-', actions: '-', pid: '-' }
  }
  loadDiag()
}

async function loadDiag() {
  // 引擎未运行时不请求诊断，避免误报
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

onMounted(() => {
  loadStats()
  if (hasBridge()) diagTimer = setInterval(loadDiag, 30000)
})
onUnmounted(() => { if (diagTimer) clearInterval(diagTimer) })
// SSE 事件到达时统一刷新统计 + 诊断
watch(() => store.refreshSignal, () => loadStats())
// 引擎状态切换：关闭时清空数据，启动时立即刷新
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
    <div class="page-head"><h2>引擎状态</h2><div class="actions">
      <button class="btn btn-outlined" @click="refreshHome"><span class="material-symbols-outlined">refresh</span>刷新</button>
    </div></div>

    <!-- APatch 风格状态卡片 -->
    <div class="apatch-hero" :class="isStarting ? 'starting' : isStopping ? 'stopping' : isRunning ? 'running' : isControllerOnline ? 'stopped' : 'offline'">
      <div class="hero-left">
        <div class="hero-icon">
          <span v-if="isStarting || isStopping" class="spinner"></span>
          <span v-else class="material-symbols-outlined">{{ isRunning ? 'task_alt' : isControllerOnline ? 'pause_circle' : 'cloud_off' }}</span>
        </div>
        <div>
          <h3>{{ isStarting ? '启动中…' : isStopping ? '暂停中…' : isRunning ? '自动化运行中' : isControllerOnline ? '自动化已暂停' : '后台服务未启动' }}</h3>
          <p v-if="isStarting">正在连接后台服务并加载规则…</p>
          <p v-else-if="isStopping">触发器正在安全退出，托盘与配置服务会保持在线。</p>
          <p v-else-if="isRunning">PID {{ stats.pid }} · {{ modeLabel }} · 127.0.0.1:19198</p>
          <p v-else-if="isControllerOnline">PID {{ stats.pid }} · 可继续编辑规则或重新启动自动化</p>
          <p v-else>Dashboard 会为你启动后台服务、托盘与自动化引擎</p>
        </div>
      </div>
      <!-- 填充按钮 + 假加载 spinner，4 态互斥 -->
      <div class="actions">
        <button v-if="!isRunning && !isStarting && !isStopping" class="btn" @click="startEngine" style="min-width:148px">
          <span class="material-symbols-outlined">play_arrow</span>{{ isControllerOnline ? '启动自动化' : '启动后台服务' }}</button>
        <button v-if="isStarting" class="btn" disabled style="min-width:148px">
          <span class="spinner"></span>启动中...</button>
        <button v-if="isRunning && !isStopping" class="btn" @click="stopEngine" style="min-width:148px">
          <span class="material-symbols-outlined">pause</span>暂停自动化</button>
        <button v-if="isStopping" class="btn" disabled style="min-width:148px">
          <span class="spinner"></span>暂停中...</button>
      </div>
    </div>

    <!-- 引擎被拒绝启动：配置可能被篡改（暂停横幅下追加红色警告条） -->
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

    <!-- 引擎运行中：整块信息架构，避免碎片化小卡片 -->
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

    <!-- 引擎未运行：显示系统信息 -->
    <template v-else>
    <section class="dashboard-card dashboard-system">
      <header class="dashboard-card-head">
        <div>
          <h3>系统信息</h3>
        </div>
      </header>
      <div class="dashboard-system-grid">
        <div v-for="info in [
            { icon: 'info', key: '版本', val: 'NotmyFault v' + appVersion },
            { icon: 'shield', key: '安全模式', val: modeLabel },
            { icon: 'folder', key: '配置目录', val: '%APPDATA%/NotmyFault/' },
            { icon: 'rule', key: '已配置规则', val: (store.configData?.rules?.length || 0) + ' 条' },
          ]" :key="info.key" class="dashboard-system-row">
          <span class="material-symbols-outlined">{{ info.icon }}</span>
          <div>
            <span>{{ info.key }}</span>
            <strong>{{ info.val }}</strong>
          </div>
        </div>
      </div>
    </section>
    <div class="empty-state" style="padding:32px 20px">
      <div class="material-symbols-outlined">power_off</div>
      <h3>后台服务未启动</h3>
      <p>点击上方按钮，同时启动托盘、配置服务和自动化引擎</p>
    </div>
    </template>
  </section>
</template>
