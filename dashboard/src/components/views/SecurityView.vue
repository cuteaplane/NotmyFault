<script setup>
import { ref, computed, onMounted } from 'vue'
import { store } from '../../lib/store'
import { getEngineStatus } from '../../lib/api'

const mode = ref('unknown')
const modeMap = {
  strict: { l: '严格', d: '未声明危险能力的插件将被拒绝加载，签名无效也不加载', c: 'sec-mode-strict' },
  normal: { l: '标准', d: '签名无效的插件降级加载，未声明能力仅告警', c: 'sec-mode-normal' },
  permissive: { l: '宽松', d: '开发模式：所有插件均可加载，未声明能力仅告警', c: 'sec-mode-permissive' },
  unknown: { l: '未知', d: '无法获取安全模式（引擎可能未运行）', c: 'sec-mode-permissive' },
}
const pL = { admin: '管理员', native_api: '原生API', external_binary: '外部程序' }
const pC = { admin: 'chip-admin', native_api: 'chip-native', external_binary: 'chip-external' }
const pI = { admin: 'admin_panel_settings', native_api: 'code', external_binary: 'terminal' }
const oL = { builtin: '内置', user: '用户', third_party: '第三方' }

const all = computed(() => {
  const arr = []
  ;['triggers', 'actions'].forEach(pt => {
    const plugins = store.pluginsData[pt] || {}
    Object.entries(plugins).forEach(([pid, meta]) => {
      arr.push({ pid, name: meta.name || pid, type: pt === 'triggers' ? '触发器' : '动作',
        origin: meta.origin, enabled: meta.enabled !== false, perms: meta.permissions || [] })
    })
  })
  arr.sort((a, b) => a.type.localeCompare(b.type) || a.name.localeCompare(b.name))
  return arr
})

const counts = computed(() => {
  let a = 0, n = 0, e = 0, c = 0
  all.value.forEach(p => {
    if (p.perms.includes('admin')) a++
    if (p.perms.includes('native_api')) n++
    if (p.perms.includes('external_binary')) e++
    if (p.perms.length === 0) c++
  })
  return { admin: a, native: n, external: e, clean: c }
})

const modeInfo = computed(() => modeMap[mode.value] || modeMap.unknown)

async function load() {
  try {
    const s = await getEngineStatus()
    // 合并而非整体覆盖：与 App.vue.updateStatus / HomeView.syncStatus 对齐。
    // 整体替换会把 engine_running 等部分状态字段冲掉，导致引擎状态不一致。
    store.engineStatus = { ...store.engineStatus, ...s }
    store.engineOnline = s.engine_running === true
    document.body.classList.toggle('engine-online', store.engineOnline)
    mode.value = s.security_mode || 'unknown'
  } catch (e) { mode.value = 'unknown' }
}
onMounted(load)
</script>

<template>
  <section class="page active">
    <div class="page-head"><h2>安全与权限</h2><div class="actions">
      <button class="btn btn-outlined" @click="load"><span class="material-symbols-outlined">refresh</span>刷新</button>
    </div></div>
    <div class="sec-banner" :class="modeInfo.c"><span class="material-symbols-outlined sec-banner-ico">shield</span>
      <div><div class="sec-banner-title">安全模式：{{ modeInfo.l }}</div><p class="sec-banner-desc">{{ modeInfo.d }}</p></div></div>
    <div class="stat-grid">
      <div class="stat-card"><div class="material-symbols-outlined stat-ico" style="color:var(--md-error)">admin_panel_settings</div><div class="stat-val">{{ counts.admin }}</div><div class="stat-lbl">管理员权限</div></div>
      <div class="stat-card"><div class="material-symbols-outlined stat-ico" style="color:var(--md-warn)">code</div><div class="stat-val">{{ counts.native }}</div><div class="stat-lbl">原生 API</div></div>
      <div class="stat-card"><div class="material-symbols-outlined stat-ico" style="color:var(--md-tertiary)">terminal</div><div class="stat-val">{{ counts.external }}</div><div class="stat-lbl">外部程序</div></div>
      <div class="stat-card"><div class="material-symbols-outlined stat-ico" style="color:var(--md-success)">verified</div><div class="stat-val">{{ counts.clean }}</div><div class="stat-lbl">无特殊权限</div></div>
    </div>
    <h4 class="sec-h">权限说明</h4>
    <div class="sec-legend">
      <div class="sec-legend-item"><span class="chip chip-admin">管理员</span><span>导入 notmyfault.sudo 模块，可请求管理员提权执行</span></div>
      <div class="sec-legend-item"><span class="chip chip-native">原生 API</span><span>直接调用 ctypes / win32api 等系统底层接口，可绕过内置工具</span></div>
      <div class="sec-legend-item"><span class="chip chip-external">外部程序</span><span>通过 subprocess / os.system 执行外部命令或进程</span></div>
    </div>
    <h4 class="sec-h">插件权限清单</h4>
    <div v-if="!all.length" class="empty-state"><div class="material-symbols-outlined">extension_off</div><h3>暂无插件</h3><p>启动引擎或安装插件后查看</p></div>
    <div v-else class="perm-table">
      <div class="perm-row perm-row-head"><span class="perm-name">插件</span><span class="perm-type">类型</span><span class="perm-origin">来源</span><span class="perm-chips">声明权限</span></div>
      <div v-for="p in all" :key="p.pid" class="perm-row" :class="{ disabled: !p.enabled }">
        <span class="perm-name"><b>{{ p.name }}</b><small>{{ p.pid }}</small></span>
        <span class="perm-type">{{ p.type }}</span>
        <span class="perm-origin">{{ oL[p.origin] || p.origin || '未知' }}</span>
        <span class="perm-chips">
          <span v-if="!p.perms.length" class="chip chip-clean">无特殊权限</span>
          <span v-for="perm in p.perms" :key="perm" class="chip" :class="pC[perm]">
            <span class="material-symbols-outlined" style="font-size:14px">{{ pI[perm] }}</span>{{ pL[perm] }}
          </span>
        </span>
      </div>
    </div>
  </section>
</template>
