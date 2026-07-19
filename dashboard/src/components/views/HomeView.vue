<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { store } from '../../lib/store'
import { getEngineStatus, readDiagnostics, hasBridge, API } from '../../lib/api'
import { snackbar } from '../../lib/notify'

const starting = ref(false)
const stopping = ref(false)
const stats = ref({ rules: '-', triggers: '-', actions: '-', pid: '-' })
const diag = ref(null)
let diagTimer = null

const isRunning = computed(() => store.engineStatus.engine_running === true)
const modeLabel = computed(() => {
  const m = store.engineStatus.security_mode
  return ({ strict: '严格', normal: '标准', permissive: '宽松' })[m] || '-'
})

async function syncStatus(s) {
  // 合并而非整体替换：stopEngine 会传 {engine_running:false} 这样的部分对象，
  // 整体替换会把 security_mode / pid / rules_count 等字段一起冲掉，导致停引擎后
  // 安全模式标签变 '-'、SecurityView 显示“未知”。与 App.vue.updateStatus 对齐。
  store.engineStatus = { ...store.engineStatus, ...s }
  store.engineOnline = s.engine_running === true
  document.body.classList.toggle('engine-online', store.engineOnline)
}

async function loadStats() {
  try {
    const s = await getEngineStatus()
    await syncStatus(s)
    stats.value = {
      rules: s.rules_count != null ? s.rules_count : '-',
      triggers: s.triggers_count != null ? s.triggers_count : '-',
      actions: s.actions_count != null ? s.actions_count : '-',
      pid: s.pid || '-',
    }
  } catch (e) {
    stats.value = { rules: '-', triggers: '-', actions: '-', pid: '-' }
  }
  loadDiag()
}

async function loadDiag() { diag.value = await readDiagnostics() }

const diagPlugins = computed(() => {
  const d = diag.value
  if (!d) return { txt: '引擎未启动', cls: 'diag-warn', icon: 'remove' }
  const n = (d.plugin_errors || []).length
  return n ? { txt: n + ' 个插件加载失败', cls: 'diag-err', icon: 'error' }
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

async function startEngine() {
  if (starting.value) return
  starting.value = true
  if (hasBridge()) {
    try {
      const r = await window.pywebview.api.launch_engine()
      if (!r.ok) throw new Error(r.error || '未知错误')
    } catch (e) { alert('启动失败: ' + e.message); starting.value = false; return }
  }
  let attempts = 0
  const id = setInterval(async () => {
    attempts++
    try {
      const r = await fetch(API + '/api/engine/status')
      const s = await r.json()
      if (s.engine_running) {
        clearInterval(id)
        starting.value = false
        await syncStatus(s)
        if (window.__nmf) window.__nmf.refreshAll()
        loadStats()
      }
    } catch (e) { /* retry */ }
    if (attempts >= 15) {
      clearInterval(id)
      starting.value = false
      alert('启动超时，请手动双击 NOTMYFAULT.pyw，或先通过桌面端 Dashboard 启动')
    }
  }, 1000)
}

async function stopEngine() {
  if (stopping.value) return
  stopping.value = true

  // bridge 是唯一安全的停引擎途径（dashboard.pyw 的 _auth_request 带了 token）
  // 非 bridge 模式下裸 fetch 没 token 会 403，直接拦住不走那条路
  if (!hasBridge()) {
    // bridge 可能还在注入，等一拍再检查
    await new Promise(r => setTimeout(r, 300))
  }
  if (!hasBridge()) {
    alert('Dashboard 尚未就绪，请稍后再试')
    stopping.value = false
    return
  }

  let stopOk = false
  try {
    const r = await window.pywebview.api.shutdown_engine()
    stopOk = r && r.ok
    if (!stopOk) {
      alert('关闭失败: ' + (r?.error || '引擎未响应'))
      stopping.value = false
      return
    }
  } catch (e) {
    // bridge 调用异常--引擎可能已经退了，走轮询确认
  }

  // 轮询等待引擎退出
  let pa = 0
  const id = setInterval(async () => {
    pa++
    try {
      const s = await getEngineStatus()
      if (!s.engine_running) {
        clearInterval(id)
        stopping.value = false
        await syncStatus({ engine_running: false })
        snackbar('引擎已关闭')
      }
      if (pa >= 8) {
        clearInterval(id)
        stopping.value = false
        await syncStatus({ engine_running: false })
      }
    } catch (e) {
      // API 也挂了--引擎确实退了
      clearInterval(id)
      stopping.value = false
      await syncStatus({ engine_running: false })
      snackbar('引擎已关闭')
    }
  }, 1000)
}

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
</script>

<template>
  <section class="page active">
    <div class="page-head"><h2>引擎状态</h2><div class="actions">
      <button class="btn btn-outlined" @click="refreshHome"><span class="material-symbols-outlined">refresh</span>刷新</button>
    </div></div>
    <div class="status-hero">
      <div class="left">
        <div class="status-dot" :class="{ running: isRunning }"><span class="material-symbols-outlined">{{ isRunning ? 'play_circle' : 'stop_circle' }}</span></div>
        <div><h3>{{ isRunning ? '运行中' : '已停止' }}</h3>
          <p v-if="isRunning">PID: {{ stats.pid }} · 安全模式: {{ modeLabel }} · 监听 127.0.0.1:19198</p>
          <p v-else>引擎未运行</p></div>
      </div>
      <div class="actions">
        <button v-if="!isRunning" class="btn btn-tonal" @click="startEngine" :disabled="starting" style="min-width:148px">
          <span v-if="starting" class="spinner"></span><span v-else class="material-symbols-outlined">play_arrow</span>{{ starting ? '启动中...' : '启动引擎' }}</button>
        <button v-if="isRunning" class="btn btn-error" @click="stopEngine" :disabled="stopping" style="min-width:148px">
          <span v-if="stopping" class="spinner"></span><span v-else class="material-symbols-outlined">power_settings_new</span>{{ stopping ? '关闭中...' : '关闭引擎' }}</button>
      </div>
    </div>
    <div class="stat-grid">
      <div class="stat-card"><div class="material-symbols-outlined stat-ico">rule</div><div class="stat-val">{{ stats.rules }}</div><div class="stat-lbl">规则数量</div></div>
      <div class="stat-card"><div class="material-symbols-outlined stat-ico">memory</div><div class="stat-val">{{ stats.triggers }}</div><div class="stat-lbl">活跃触发器</div></div>
      <div class="stat-card"><div class="material-symbols-outlined stat-ico">bolt</div><div class="stat-val">{{ stats.actions }}</div><div class="stat-lbl">动作类型</div></div>
      <div class="stat-card"><div class="material-symbols-outlined stat-ico">tag</div><div class="stat-val">{{ stats.pid }}</div><div class="stat-lbl">进程 PID</div></div>
    </div>
    <h4 class="sec-h">引擎诊断</h4>
    <div class="stat-grid">
      <div class="stat-card"><div class="stat-val" :class="diagPlugins.cls"><span v-if="diagPlugins.icon" class="material-symbols-outlined">{{ diagPlugins.icon }}</span>{{ diagPlugins.txt }}</div><div class="stat-lbl">插件状态</div></div>
      <div class="stat-card"><div class="stat-val" :class="diagRules.cls"><span v-if="diagRules.icon" class="material-symbols-outlined">{{ diagRules.icon }}</span>{{ diagRules.txt }}</div><div class="stat-lbl">规则状态</div></div>
      <div class="stat-card"><div class="stat-val" :class="diagActions.cls"><span v-if="diagActions.icon" class="material-symbols-outlined">{{ diagActions.icon }}</span>{{ diagActions.txt }}</div><div class="stat-lbl">动作执行</div></div>
      <div class="stat-card"><div class="stat-val"><span class="material-symbols-outlined">{{ diagErrors.ec > 0 ? 'error' : 'check_circle' }}</span>{{ diagErrors.txt }}</div><div class="stat-lbl">错误 / 警告</div></div>
    </div>
    <div class="diag-detail">
      <div v-for="(l, i) in diagLines" :key="i" :class="l.cls"><span class="material-symbols-outlined">{{ l.icon }}</span>{{ l.text }}</div>
      <div v-if="!diagLines.length">无异常</div>
    </div>
  </section>
</template>
