<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { store } from '../../lib/store'
import { getEngineStatus, readDiagnostics, hasBridge } from '../../lib/api'
import { snackbar } from '../../lib/notify'

const starting = ref(false)
const stopping = ref(false)
const stats = ref({ rules: '-', triggers: '-', actions: '-', pid: '-' })
const diag = ref(null)
let diagTimer = null
const appVersion = __APP_VERSION__

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
    if (s.engine_running === true) {
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
      const s = await getEngineStatus()
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
// 引擎状态切换：关闭时清空数据，启动时立即刷新
watch(isRunning, (running) => {
  if (!running) {
    stats.value = { rules: '-', triggers: '-', actions: '-', pid: '-' }
    diag.value = null
  } else {
    loadStats()
  }
})
</script>

<template>
  <section class="page active">
    <div class="page-head"><h2>引擎状态</h2><div class="actions">
      <button class="btn btn-outlined" @click="refreshHome"><span class="material-symbols-outlined">refresh</span>刷新</button>
    </div></div>

    <!-- APatch 风格状态卡片 -->
    <div class="apatch-hero" :class="starting ? 'starting' : stopping ? 'stopping' : isRunning ? 'running' : 'stopped'">
      <div class="hero-left">
        <div class="hero-icon">
          <span v-if="starting" class="spinner"></span>
          <span v-else-if="stopping" class="spinner"></span>
          <span v-else class="material-symbols-outlined">{{ isRunning ? 'task_alt' : 'cancel' }}</span>
        </div>
        <div>
          <h3>{{ starting ? '启动中...' : stopping ? '关闭中...' : isRunning ? '运行中 😋' : '已停止' }}</h3>
          <p v-if="starting">正在启动引擎进程...</p>
          <p v-else-if="stopping">正在关闭引擎...</p>
          <p v-else-if="isRunning">PID {{ stats.pid }} · {{ modeLabel }} · 127.0.0.1:19198</p>
          <p v-else>引擎未运行</p>
        </div>
      </div>
      <!-- 填充按钮 + 假加载 spinner，4 态互斥 -->
      <div class="actions">
        <button v-if="!isRunning && !starting && !stopping" class="btn" @click="startEngine" style="min-width:148px">
          <span class="material-symbols-outlined">play_arrow</span>启动引擎</button>
        <button v-if="starting" class="btn" disabled style="min-width:148px">
          <span class="spinner"></span>启动中...</button>
        <button v-if="isRunning && !stopping" class="btn" @click="stopEngine" style="min-width:148px">
          <span class="material-symbols-outlined">power_settings_new</span>关闭引擎</button>
        <button v-if="stopping" class="btn" disabled style="min-width:148px">
          <span class="spinner"></span>关闭中...</button>
      </div>
    </div>

    <!-- 引擎运行中：显示概览 + 诊断 -->
    <template v-if="isRunning">
    <div class="card" style="margin-bottom:16px">
      <h4 class="card-section-title">引擎概览</h4>
      <div class="kv-list">
        <div class="kv-item"><span class="kv-key">规则数量</span><span class="kv-val">{{ stats.rules }}</span></div>
        <div class="kv-item"><span class="kv-key">活跃触发器</span><span class="kv-val">{{ stats.triggers }}</span></div>
        <div class="kv-item"><span class="kv-key">动作类型</span><span class="kv-val">{{ stats.actions }}</span></div>
        <div class="kv-item"><span class="kv-key">进程 PID</span><span class="kv-val">{{ stats.pid }}</span></div>
      </div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <h4 class="card-section-title">引擎诊断</h4>
      <div class="kv-list">
        <div class="kv-item"><span class="kv-key">插件状态</span><span class="kv-val" :class="diagPlugins.cls"><span v-if="diagPlugins.icon" class="material-symbols-outlined">{{ diagPlugins.icon }}</span>{{ diagPlugins.txt }}</span></div>
        <div class="kv-item"><span class="kv-key">规则状态</span><span class="kv-val" :class="diagRules.cls"><span v-if="diagRules.icon" class="material-symbols-outlined">{{ diagRules.icon }}</span>{{ diagRules.txt }}</span></div>
        <div class="kv-item"><span class="kv-key">动作执行</span><span class="kv-val" :class="diagActions.cls"><span v-if="diagActions.icon" class="material-symbols-outlined">{{ diagActions.icon }}</span>{{ diagActions.txt }}</span></div>
        <div class="kv-item"><span class="kv-key">错误 / 警告</span><span class="kv-val"><span class="material-symbols-outlined">{{ diagErrors.ec > 0 ? 'error' : 'check_circle' }}</span>{{ diagErrors.txt }}</span></div>
        <!-- 诊断详情整合到卡片内，kv-item 风格统一 -->
        <template v-if="diagLines.length">
          <div class="kv-item" v-for="(l, i) in diagLines" :key="i">
            <span class="kv-key" :class="l.cls"><span class="material-symbols-outlined">{{ l.icon }}</span>{{ l.text }}</span>
          </div>
        </template>
        <div v-else class="kv-item">
          <span class="kv-key">异常详情</span>
          <span class="kv-val diag-ok"><span class="material-symbols-outlined">check_circle</span>无异常</span>
        </div>
      </div>
    </div>
    </template>

    <!-- 引擎未运行：显示系统信息 -->
    <template v-else>
    <div class="card" style="margin-bottom:16px">
      <h4 class="card-section-title">系统信息</h4>
      <div class="kv-list">
        <div class="kv-item"><span class="kv-key">版本</span><span class="kv-val">NotmyFault v{{ appVersion }}</span></div>
        <div class="kv-item"><span class="kv-key">安全模式</span><span class="kv-val">{{ modeLabel }}</span></div>
        <div class="kv-item"><span class="kv-key">配置目录</span><span class="kv-val">%APPDATA%/NotmyFault/</span></div>
        <div class="kv-item"><span class="kv-key">已配置规则</span><span class="kv-val">{{ store.configData?.rules?.length || 0 }} 条</span></div>
      </div>
    </div>
    <div class="empty-state" style="padding:32px 20px">
      <div class="material-symbols-outlined">power_off</div>
      <h3>引擎未启动</h3>
      <p>点击上方"启动引擎"按钮开始使用</p>
    </div>
    </template>
  </section>
</template>
