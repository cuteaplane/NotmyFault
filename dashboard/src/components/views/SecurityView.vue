<script setup>
import { ref, computed, onMounted } from 'vue'
import { store } from '../../lib/store'
import {
  getEngineStatus,
  getConfigSecurityStatus,
  approveConfigSecurity,
  getAdminAuthorizationSetting,
  updateAdminAuthorizationSetting,
} from '../../lib/api'
import { snackbar } from '../../lib/notify'
import { alertDialog, confirmDialog } from '../../lib/dialog'

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
const adminAuth = ref({
  mode: 'per_execution',
  effective_mode: null,
  supported_modes: ['per_execution'],
  restart_required: false,
})
const savingAdminAuth = ref(false)
const HIGH_RISK = ['run_powershell', 'shutdown_system', 'kill_process']

async function loadConfigSecurity() {
  const data = await getConfigSecurityStatus()
  configSec.value = data
}

async function approveConfig() {
  if (!await confirmDialog('重新签名配置？', '请再次确认上方摘要中的规则均为你本人配置。确认无误后，当前配置将被原样重新签名，引擎恢复运行。', '重新签名')) return
  approving.value = true
  try {
    const r = await approveConfigSecurity()
    if (r.ok) {
      snackbar(r.message || '配置已重新签名')
      await loadConfigSecurity()
      await load()
    } else {
      alertDialog('重新签名失败', r.error || '未知错误')
    }
  } catch (e) {
    alertDialog('重新签名失败', e.message)
  } finally {
    approving.value = false
  }
}

async function loadAdminAuthorization() {
  try {
    const result = await getAdminAuthorizationSetting()
    adminAuth.value = {
      ...adminAuth.value,
      ...result,
      supported_modes: Array.isArray(result.supported_modes)
        ? result.supported_modes
        : ['per_execution'],
    }
  } catch (e) {
    snackbar('无法读取管理员授权方式')
  }
}

async function selectAdminAuthorization(mode) {
  if (savingAdminAuth.value || mode === adminAuth.value.mode) return
  savingAdminAuth.value = true
  try {
    const result = await updateAdminAuthorizationSetting(mode)
    if (!result.ok) {
      alertDialog('保存失败', result.error || '无法保存管理员授权方式')
      return
    }
    adminAuth.value = { ...adminAuth.value, ...result }
    snackbar(result.restart_required ? '已保存，重启引擎后生效' : '管理员授权方式已保存')
  } catch (e) {
    alertDialog('保存失败', e.message)
  } finally {
    savingAdminAuth.value = false
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
onMounted(() => { load(); loadConfigSecurity(); loadAdminAuthorization() })
</script>

<template>
  <section class="page active">
    <div class="page-head"><h2>安全与权限</h2><div class="actions">
      <button class="btn btn-outlined" @click="load(); loadConfigSecurity()"><span class="material-symbols-outlined">refresh</span>刷新</button>
    </div></div>

    <Transition name="status-strip" mode="out-in">
    <div v-if="configSec.status === 'tampered'" key="tampered"
      class="config-security-status config-security-panel config-security-panel-danger">
      <header class="config-security-head">
        <span class="material-symbols-outlined config-security-icon">shield_person</span>
        <div>
          <h3>配置可能被篡改，引擎已暂停</h3>
          <p>{{ configSec.reason }}</p>
        </div>
      </header>
      <div v-if="configSec.summary" class="config-security-summary">
        <div class="config-security-summary-title">
          <span>当前规则摘要</span>
          <strong>{{ configSec.summary.rule_count }} 条</strong>
        </div>
        <div class="config-security-rule-list">
          <div v-for="(rule, i) in configSec.summary.rules" :key="i" class="config-security-rule">
            <span>{{ i + 1 }}. {{ rule.name }}</span>
            <span class="config-security-rule-actions">
              <span v-for="(a, j) in rule.actions" :key="j" class="chip"
                    :class="a.high_risk ? 'chip-admin' : 'chip-clean'">{{ a.type }}</span>
            </span>
          </div>
          <div v-if="!configSec.summary.rules.length" class="config-security-rule-empty">当前没有规则</div>
        </div>
        <p class="config-security-risk">
          <span class="material-symbols-outlined">warning</span>
          <span>红色标记为高风险动作（PowerShell 执行 / 系统控制 / 进程终止），请确认它们由你本人配置。</span>
        </p>
      </div>
      <footer class="config-security-actions">
        <p>
          <span class="material-symbols-outlined">backup</span>
          <span>重新签名前，设置与规则会分别备份到 config.json.bak 和 rules.json.bak。</span>
        </p>
        <button class="btn btn-filled" :disabled="approving" @click="approveConfig">
          <span class="material-symbols-outlined">verified</span>{{ approving ? '重新签名中…' : '我已确认，重新签名' }}
        </button>
      </footer>
    </div>
    <div v-else-if="configSec.status === 'ok'" key="ok"
      class="config-security-status config-security-panel config-security-panel-ok config-security-panel-compact">
      <span class="material-symbols-outlined config-security-icon">verified</span>
      <div>
        <strong>完整性正常</strong>
        <p>设置与规则的签名均有效。</p>
      </div>
    </div>
    <div v-else-if="configSec.status === 'unreadable'" key="unreadable"
      class="config-security-status config-security-panel config-security-panel-warn config-security-panel-compact">
      <span class="material-symbols-outlined config-security-icon">warning</span>
      <div>
        <strong>配置无法读取</strong>
        <p>{{ configSec.reason }}</p>
      </div>
    </div>
    </Transition>

    <div class="sec-banner" :class="modeInfo.c"><span class="material-symbols-outlined sec-banner-ico">shield</span>
      <div><div class="sec-banner-title">安全模式：{{ modeInfo.l }}</div><p class="sec-banner-desc">{{ modeInfo.d }}</p></div></div>

    <h4 class="sec-h">管理员授权方式</h4>
    <div class="admin-auth-settings">
      <button type="button" class="admin-auth-option"
        :class="{ selected: adminAuth.mode === 'engine_start' }"
        :disabled="savingAdminAuth || !adminAuth.supported_modes.includes('engine_start')"
        @click="selectAdminAuthorization('engine_start')">
        <span class="material-symbols-outlined">verified_user</span>
        <span><b>引擎启动时授权一次</b><small>存在使用管理员插件的启用规则时，启动阶段显示一次 UAC。本代引擎后续通过管理员代理执行这些命令。<template v-if="!adminAuth.supported_modes.includes('engine_start')">当前系统不支持此方式。</template></small></span>
        <span class="material-symbols-outlined auth-check">{{ adminAuth.mode === 'engine_start' ? 'radio_button_checked' : 'radio_button_unchecked' }}</span>
      </button>
      <button type="button" class="admin-auth-option"
        :class="{ selected: adminAuth.mode === 'per_execution' }"
        :disabled="savingAdminAuth"
        @click="selectAdminAuthorization('per_execution')">
        <span class="material-symbols-outlined">touch_app</span>
        <span><b>每次执行时确认</b><small>先显示保留两分钟的 NotmyFault 通知；点击“允许并继续”后，才为该次命令显示 UAC。关闭或超时未处理时不执行。</small></span>
        <span class="material-symbols-outlined auth-check">{{ adminAuth.mode === 'per_execution' ? 'radio_button_checked' : 'radio_button_unchecked' }}</span>
      </button>
    </div>
    <p v-if="adminAuth.restart_required" class="admin-auth-restart">
      <span class="material-symbols-outlined">restart_alt</span>当前引擎仍使用上一次设置，重启引擎后生效。
    </p>
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
