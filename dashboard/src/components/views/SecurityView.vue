<script setup>
import { ref, computed, onMounted } from 'vue'
import { store } from '../../lib/store'
import {
  getEngineStatus,
  getConfigSecurityStatus,
  approveConfigSecurity,
} from '../../lib/api'
import { snack } from '../../lib/notify'

const mode = ref('unknown')
const modeMap = {
  strict: { l: '严格', d: '未声明危险能力的插件将被拒绝加载，签名无效也不加载', c: 'sec-mode-strict' },
  normal: { l: '标准', d: '签名无效的插件降级加载，未声明能力仅告警', c: 'sec-mode-normal' },
  permissive: { l: '宽松', d: '开发模式：所有插件均可加载，未声明能力仅告警', c: 'sec-mode-permissive' },
  unknown: { l: '未知', d: '无法获取安全模式（引擎可能未运行）', c: 'sec-mode-permissive' },
}
// pL 按后端 PERMISSION_REGISTRY 列出全部权限，clipboard 等新增项也会显示图标和文字。
const pL = {
  notification: '发送通知', audio: '音频', clipboard: '剪贴板', network: '网络访问',
  external_binary: '外部程序', native_api: '原生 API', filesystem: '文件系统',
  process: '进程管理', registry: '注册表', screen_reader: '屏幕读取', admin: '管理员权限',
}
const pC = {
  notification: 'chip-clean', audio: 'chip-permission-low', clipboard: 'chip-permission-medium',
  network: 'chip-permission-medium', external_binary: 'chip-external', native_api: 'chip-native',
  filesystem: 'chip-permission-high', process: 'chip-permission-high', registry: 'chip-permission-high',
  screen_reader: 'chip-permission-high', admin: 'chip-admin',
}
const pI = {
  notification: 'notifications', audio: 'volume_up', clipboard: 'content_paste', network: 'language',
  external_binary: 'terminal', native_api: 'code', filesystem: 'folder_open', process: 'memory',
  registry: 'account_tree', screen_reader: 'screenshot_monitor', admin: 'admin_panel_settings',
}
const oL = { builtin: '内置', user: '用户', third_party: '第三方' }

const configSec = ref({ status: 'loading', reason: '', summary: null })
const approving = ref(false)
const HIGH_RISK = ['run_powershell', 'shutdown_system', 'kill_process']

async function loadConfigSecurity() {
  const data = await getConfigSecurityStatus()
  configSec.value = data
}

async function approveConfig() {
  if (!confirm('请再次确认上方摘要中的规则均为你本人配置。确认无误后，当前配置将被原样重新签名，引擎恢复运行。')) return
  approving.value = true
  try {
    const r = await approveConfigSecurity()
    if (r.ok) {
      snack(r.message || '配置已重新签名')
      await loadConfigSecurity()
      await load()
    } else {
      alert('重新签名失败: ' + (r.error || '未知错误'))
    }
  } catch (e) {
    alert('重新签名失败: ' + e.message)
  } finally {
    approving.value = false
  }
}

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
    // 状态更新采用合并，保留已有的 engine_running 等字段。
    store.engineStatus = { ...store.engineStatus, ...s }
    store.engineOnline = s.engine_running === true
    document.body.classList.toggle('engine-online', store.engineOnline)
    mode.value = s.security_mode || 'unknown'
  } catch (e) { mode.value = 'unknown' }
}
onMounted(() => { load(); loadConfigSecurity() })
</script>

<template>
  <section class="page active">
    <div class="page-head"><h2>安全与权限</h2><div class="actions">
      <button class="btn btn-outlined" @click="load(); loadConfigSecurity()"><span class="material-symbols-outlined">refresh</span>刷新</button>
    </div></div>

    <Transition name="status-strip" mode="out-in">
    <div v-if="configSec.status === 'tampered'" key="tampered" class="config-security-status rounded-lg border border-error/40 bg-error/10 p-4">
      <div class="flex items-center gap-2">
        <span class="material-symbols-outlined text-error">shield_person</span>
        <h3 class="font-bold text-error">配置可能被篡改，引擎已暂停</h3>
      </div>
      <p class="mt-1 text-body-s text-on-surface-variant">{{ configSec.reason }}</p>
      <div v-if="configSec.summary" class="mt-3">
        <p class="text-body-s font-semibold">当前配置摘要（共 {{ configSec.summary.rule_count }} 条规则）：</p>
        <div class="mt-2 max-h-48 overflow-y-auto rounded-md bg-surface-c-low p-3">
          <div v-for="(rule, i) in configSec.summary.rules" :key="i" class="border-b border-on-surface/10 py-1 last:border-0">
            <span class="text-body-s font-medium">{{ i + 1 }}. {{ rule.name }}</span>
            <span class="ml-2 flex flex-wrap gap-1">
              <span v-for="(a, j) in rule.actions" :key="j" class="chip"
                    :class="a.high_risk ? 'chip-admin' : 'chip-clean'">{{ a.type }}</span>
            </span>
          </div>
        </div>
        <p class="mt-2 text-body-s text-warn">
          红色标记为高风险动作（PowerShell 执行 / 系统控制 / 进程终止），请仔细核对是否为你本人配置。
        </p>
      </div>
      <div class="mt-3 flex flex-wrap gap-2">
        <button class="btn btn-primary" :disabled="approving" @click="approveConfig">
          <span class="material-symbols-outlined">verified</span>{{ approving ? '重新签名中…' : '我已确认无误，重新签名' }}
        </button>
        <button class="btn btn-outlined btn-danger" :disabled="resetting" @click="resetConfig">
          <span class="material-symbols-outlined">restart_alt</span>{{ resetting ? '恢复中…' : '恢复默认配置' }}
        </button>
      </div>
      <p class="mt-2 text-body-s text-on-surface-variant">当前内容已自动备份到 config.json.bak，可随时手动恢复。</p>
    </div>
    <div v-else-if="configSec.status === 'ok'" key="ok" class="config-security-status flex items-center gap-2 rounded-lg border border-success/40 bg-success/10 p-3">
      <span class="material-symbols-outlined text-success">verified</span>
      <span class="text-body-s">配置签名有效，完整性正常。</span>
    </div>
    <div v-else-if="configSec.status === 'unreadable'" key="unreadable" class="config-security-status rounded-lg border border-warn/40 bg-warn/10 p-3">
      <span class="text-body-s text-warn">配置无法读取：{{ configSec.reason }}</span>
    </div>
    </Transition>

    <div class="sec-banner" :class="modeInfo.c"><span class="material-symbols-outlined sec-banner-ico">shield</span>
      <div><div class="sec-banner-title">安全模式：{{ modeInfo.l }}</div><p class="sec-banner-desc">{{ modeInfo.d }}</p></div></div>
    <div class="stat-grid">
      <div class="stat-card"><div class="material-symbols-outlined stat-ico text-error">admin_panel_settings</div><div class="stat-val">{{ counts.admin }}</div><div class="stat-lbl">管理员权限</div></div>
      <div class="stat-card"><div class="material-symbols-outlined stat-ico text-warn">code</div><div class="stat-val">{{ counts.native }}</div><div class="stat-lbl">原生 API</div></div>
      <div class="stat-card"><div class="material-symbols-outlined stat-ico text-tertiary">terminal</div><div class="stat-val">{{ counts.external }}</div><div class="stat-lbl">外部程序</div></div>
      <div class="stat-card"><div class="material-symbols-outlined stat-ico text-success">verified</div><div class="stat-val">{{ counts.clean }}</div><div class="stat-lbl">无特殊权限</div></div>
    </div>
    <h4 class="sec-h">权限说明</h4>
    <div class="mb-5 grid grid-cols-1 gap-3 md:grid-cols-3">
      <div v-for="leg in [
          { chip: 'chip-admin', label: '管理员', icon: 'admin_panel_settings', desc: '导入 notmyfault.sudo 模块，可请求管理员提权执行' },
          { chip: 'chip-native', label: '原生 API', icon: 'code', desc: '直接调用 ctypes / win32api 等系统底层接口，可绕过内置工具' },
          { chip: 'chip-external', label: '外部程序', icon: 'terminal', desc: '通过 subprocess / os.system 执行外部命令或进程' },
        ]" :key="leg.label"
        class="flex items-start gap-3 rounded-md bg-surface-c-low p-4 shadow-elev1">
        <span class="chip shrink-0" :class="leg.chip">{{ leg.label }}</span>
        <p class="text-body-s text-on-surface-variant">{{ leg.desc }}</p>
      </div>
    </div>
    <h4 class="sec-h">插件权限清单</h4>
    <div v-if="!all.length" class="empty-state"><div class="material-symbols-outlined">extension_off</div><h3>暂无插件</h3><p>启动引擎或安装插件后查看</p></div>
    <div v-else class="overflow-x-auto"><div class="perm-table min-w-[680px]">
      <div class="perm-row perm-row-head"><span class="perm-name">插件</span><span class="perm-type">类型</span><span class="perm-origin">来源</span><span class="perm-chips">声明权限</span></div>
      <div v-for="p in all" :key="p.pid" class="perm-row" :class="{ disabled: !p.enabled }">
        <span class="perm-name"><b>{{ p.name }}</b><small>{{ p.pid }}</small></span>
        <span class="perm-type">{{ p.type }}</span>
        <span class="perm-origin">{{ oL[p.origin] || p.origin || '未知' }}</span>
        <span class="perm-chips">
          <span v-if="!p.perms.length" class="chip chip-clean">无特殊权限</span>
          <span v-for="perm in p.perms" :key="perm" class="chip" :class="pC[perm] || 'chip-unknown'"
            :title="pL[perm] ? perm : `未识别的权限：${perm || '空值'}`">
            <span class="material-symbols-outlined" style="font-size:14px">{{ pI[perm] || 'help' }}</span>{{ pL[perm] || perm || '未命名权限' }}
          </span>
        </span>
      </div>
    </div></div>
  </section>
</template>
