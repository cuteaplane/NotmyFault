<script setup>
import { ref, computed, onMounted } from 'vue'
import { store } from '../../lib/store'
import { getEngineStatus, getConfigSecurityStatus, approveConfigSecurity, loadPlugins } from '../../lib/api'
import { snackbar } from '../../lib/notify'
import { alertDialog, confirmDialog, passwordDialog } from '../../lib/dialog'
import BaseDialog from '../BaseDialog.vue'

const props = defineProps({ embedded: { type: Boolean, default: false } })
const configSec = ref({ status: 'loading', reason: '', summary: null })
const loading = ref(false)
const approving = ref(false)
const showReinstall = ref(false)
const permissionFilter = ref('all')
const mode = computed(() => configSec.value.security_mode || store.engineStatus.security_mode || 'unknown')
const modeMap = {
  strict: { label: '严格', description: '插件签名无效或使用未声明的危险能力时，拒绝加载。' },
  normal: { label: '标准', description: '允许签名无效的插件降级加载，未声明能力会产生告警。' },
  permissive: { label: '宽松', description: '允许开发中的未签名插件加载，未声明能力会产生告警。' },
  unknown: { label: '未知', description: '连接后台服务后可读取当前模式。' },
}
const modeInfo = computed(() => modeMap[mode.value] || modeMap.unknown)
const installation = computed(() => configSec.value.installation)
const installationInvalid = computed(() => installation.value?.status === 'invalid')
const configProblem = computed(() => ['tampered', 'unreadable'].includes(configSec.value.status))
const all = computed(() => ['triggers', 'actions'].flatMap(kind =>
  Object.entries(store.pluginsData[kind] || {}).map(([id, meta]) => ({
    id, kind, name: meta.name || id, origin: meta.origin,
    enabled: meta.enabled !== false, perms: meta.permissions || [], error: meta._error,
  })),
))
const userProblems = computed(() => all.value.filter(p => p.origin !== 'builtin' && p.error))
const permissionRows = computed(() => all.value.filter(p => permissionFilter.value === 'all' || p.perms.includes(permissionFilter.value)))
const heading = computed(() => {
  if (loading.value) return { icon: 'progress_activity', title: '正在检查安全状态', text: '检查安装文件、设置与规则。' }
  if (installationInvalid.value) return { icon: 'deployed_code_alert', title: '需要修复安装文件', text: '内置组件属于 NotmyFault 安装内容，请重新安装完整应用。' }
  if (configProblem.value) return { icon: 'policy', title: '需要核对设置与规则', text: '处理下方配置问题后，再启动引擎。' }
  if (!installation.value || configSec.value.status !== 'ok') return { icon: 'shield_question', title: '安全检查尚未完成', text: '暂时无法确认全部状态，请检查后台连接后重试。' }
  if (userProblems.value.length) return { icon: 'extension_off', title: '有用户插件需要处理', text: '查看下方问题插件，更新或重新安装对应扩展。' }
  return { icon: 'verified_user', title: '当前检查未发现异常', text: '安装签名、设置与规则的检查已完成。' }
})
const pL = {
  notification: '发送通知', audio: '音频', clipboard: '剪贴板', network: '网络访问',
  external_binary: '外部程序', native_api: '原生 API', filesystem: '文件系统',
  process: '进程管理', registry: '注册表', screen_reader: '屏幕读取',
  input_monitor: '监听键盘和鼠标', admin: '管理员权限',
}
const pC = {
  notification: 'chip-clean', audio: 'chip-permission-low', clipboard: 'chip-permission-medium',
  network: 'chip-permission-medium', external_binary: 'chip-external', native_api: 'chip-native',
  filesystem: 'chip-permission-high', process: 'chip-permission-high', registry: 'chip-permission-high',
  screen_reader: 'chip-permission-high', input_monitor: 'chip-permission-high', admin: 'chip-admin',
}
const pI = {
  notification: 'notifications', audio: 'volume_up', clipboard: 'content_paste', network: 'language',
  external_binary: 'terminal', native_api: 'code', filesystem: 'folder_open', process: 'memory',
  registry: 'account_tree', screen_reader: 'screenshot_monitor', input_monitor: 'keyboard', admin: 'admin_panel_settings',
}
function summaryActions(actions, prefix = '动作') {
  return (actions || []).flatMap((action, index) => {
    const path = `${prefix} ${index + 1}`
    return [
      { action, path },
      ...summaryActions(action.then, `${path} · 成立时`),
      ...summaryActions(action.else, `${path} · 否则`),
      ...summaryActions(action.failure_actions, `${path} · 失败补救`),
    ]
  })
}
async function approveConfig() {
  if (!await confirmDialog('重新签名配置？', '请核对规则、参数和失败动作。确认后为当前设置与规则重新签名；完成后可回到首页启动引擎。', '重新签名')) return
  approving.value = true
  try {
    let r = await approveConfigSecurity()
    if (!r.ok && r.code === 'admin_key_required') {
      const password = await passwordDialog(
        '需要签名私钥密码',
        '当前文件包含需要审批的动作。验证通过后才会重新签名。',
      )
      if (typeof password !== 'string') return
      r = await approveConfigSecurity(password)
    }
    if (r.ok) {
      snackbar(r.message || '配置已重新签名')
      await refresh()
    } else {
      alertDialog('重新签名失败', r.error || '未知错误')
    }
  } catch (e) {
    alertDialog('重新签名失败', e.message)
  } finally {
    approving.value = false
  }
}


function openPlugin(plugin) {
  store.pendingPluginFocus = { id: plugin.id, kind: plugin.kind === 'triggers' ? 'trigger' : 'action', reason: plugin.error || '' }
  window.__nmf?.switchPage?.('plugins')
}
async function refresh() {
  if (loading.value) return
  loading.value = true
  const [security, engine, plugins] = await Promise.allSettled([
    getConfigSecurityStatus(), getEngineStatus(), loadPlugins(),
  ])
  configSec.value = security.status === 'fulfilled'
    ? security.value : { status: 'unavailable', reason: '无法连接后台服务' }
  if (engine.status === 'fulfilled') {
    store.engineStatus = { ...store.engineStatus, ...engine.value }
    store.engineOnline = engine.value.engine_running === true
  }
  if (plugins.status === 'fulfilled') store.pluginsData = plugins.value
  loading.value = false
}
onMounted(refresh)
</script>

<template>
  <section class="page active security-center" :class="{ 'security-view-embedded': props.embedded }" :aria-busy="loading">
    <div v-if="!props.embedded" class="page-head"><h2>安全与权限</h2></div>
    <header class="security-overview" :class="{ 'needs-attention': installationInvalid || configProblem }" aria-live="polite">
      <span class="material-symbols-outlined security-emblem">{{ heading.icon }}</span>
      <div class="security-overview-copy"><h3>{{ heading.title }}</h3><p>{{ heading.text }}</p></div>
      <button class="btn btn-outlined" :disabled="loading" @click="refresh"><span class="material-symbols-outlined">refresh</span>{{ loading ? '检查中…' : '重新检查' }}</button>
    </header>

    <article class="security-block" :class="{ 'security-block-danger': installationInvalid }">
      <header class="security-block-head"><span class="material-symbols-outlined">deployed_code</span><h3>应用安装</h3><span class="chip" :class="installationInvalid ? 'chip-admin' : 'chip-clean'">{{ installationInvalid ? '需要重新安装' : installation?.status === 'ok' ? '签名有效' : '尚未确认' }}</span></header>
      <template v-if="installationInvalid">
        <p>安装文件缺失、损坏或被修改，内置插件也属于安装文件。请从可信来源重新安装 NotmyFault。</p>
        <p v-if="mode !== 'strict'" class="security-muted">当前{{ modeInfo.label }}模式允许未签名插件加载；此提示本身不会阻止引擎启动。</p>
        <button class="btn btn-filled" @click="showReinstall = true"><span class="material-symbols-outlined">install_desktop</span>重新安装 NotmyFault</button>
        <details class="security-details"><summary>查看异常文件 · {{ installation.issues.length }}</summary><ul class="security-file-list"><li v-for="(issue, i) in installation.issues" :key="i"><code>{{ issue.path }}</code><span>{{ issue.reason }}</span></li></ul></details>
      </template>
      <p v-else-if="installation?.status === 'ok'">构建信息与 {{ installation.checked_plugins }} 个内置插件的签名有效。{{ installation.core_checked ? '核心文件完整性检查通过。' : '当前运行方式不检查核心源码完整性。' }}</p>
      <p v-else>尚未取得安装文件检查结果。连接后台服务后重新检查。</p>
    </article>

    <article class="security-block" :class="{ 'security-block-danger': configProblem }">
      <header class="security-block-head"><span class="material-symbols-outlined">fact_check</span><h3>设置与规则</h3><span class="chip" :class="configProblem ? 'chip-admin' : 'chip-clean'">{{ configSec.status === 'ok' ? '签名有效' : configProblem ? '需要处理' : '尚未确认' }}</span></header>
      <p v-if="configSec.status === 'ok'">设置与规则的签名均有效。通过 Dashboard 保存的修改会自动签名。</p>
      <template v-else-if="configSec.status === 'tampered'">
        <p>{{ configSec.reason }}</p><p>先核对下方规则。只有确认这些内容是你要执行的操作后，才重新签名。</p>
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
            <div class="config-security-rule-details">
              <div v-for="entry in summaryActions([...(rule.preconditions || []), ...(rule.actions || [])])"
                   :key="entry.path" class="config-security-rule-params">
                <small>{{ entry.path }}</small>
                <span class="chip" :class="entry.action.high_risk ? 'chip-admin' : 'chip-clean'">{{ entry.action.type }}</span>
                <code>{{ JSON.stringify(entry.action.params || {}) }}</code>
              </div>
            </div>
          </div>
          <div v-if="!configSec.summary.rules.length" class="config-security-rule-empty">当前没有规则</div>
        </div>
        <p class="config-security-risk">
          <span class="material-symbols-outlined">warning</span>
          <span>红色标记为需要签名私钥审批的动作。请逐项核对动作参数、前置检查和失败动作。</span>
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

      </template>
      <p v-else-if="configSec.status === 'unreadable'">{{ configSec.reason }}。请恢复有效的配置文件，再重新检查。</p>
      <p v-else>{{ configSec.reason || '正在读取设置与规则的签名状态。' }}</p>
    </article>

    <article v-if="userProblems.length" class="security-block">
      <header class="security-block-head"><span class="material-symbols-outlined">extension_off</span><h3>用户插件</h3><span class="chip chip-admin">{{ userProblems.length }} 个问题</span></header>
      <p>用户插件可单独更新或重新安装。选择插件查看原因和处理选项。</p>
      <div class="security-plugin-list"><button v-for="plugin in userProblems" :key="`${plugin.kind}:${plugin.id}`" @click="openPlugin(plugin)"><span><b>{{ plugin.name }}</b><small>{{ plugin.error }}</small></span><span class="material-symbols-outlined">chevron_right</span></button></div>
    </article>

    <article class="security-block security-mode-block">
      <header class="security-block-head"><span class="material-symbols-outlined">shield_lock</span><h3>保护方式</h3><span class="chip">{{ modeInfo.label }}模式</span></header>
      <p>{{ modeInfo.description }}</p>
      <p class="security-muted">安全模式由签名的构建信息决定。环境变量只能提高等级；缺少有效构建信息时使用严格模式。</p>
      <div class="security-protection-list">
        <div><span class="material-symbols-outlined">touch_app</span><span><b>管理员命令逐次确认</b><small>每次执行管理员命令都需要你单独确认。</small></span></div>
        <div><span class="material-symbols-outlined">key</span><span><b>高权限规则审批</b><small>{{ mode === 'strict' ? '保存需要审批的规则时，验证签名私钥密码。' : mode === 'unknown' ? '读取模式后可确认当前审批要求。' : '当前模式保存规则时不要求签名私钥密码；执行管理员命令仍需确认。' }}</small></span></div>
      </div>
      <details class="security-details"><summary>了解三种模式</summary><div class="security-mode-list"><div v-for="(info, key) in { strict: modeMap.strict, normal: modeMap.normal, permissive: modeMap.permissive }" :key="key"><b>{{ info.label }}</b><p>{{ info.description }}</p></div></div><p class="security-muted">所有模式都会检查插件结构、平台要求和配置签名。宽松模式不会让结构错误或运行报错的插件变得可用。</p></details>
    </article>

    <details class="security-block security-permissions">
      <summary><span class="material-symbols-outlined">assignment_ind</span><span><b>插件声明的权限</b><small>查看 {{ all.length }} 个插件可访问的系统能力</small></span><span class="material-symbols-outlined">expand_more</span></summary>
      <div class="security-permission-tools"><p>权限来自插件声明；停用状态不代表已移除声明。</p><select v-model="permissionFilter" class="select" aria-label="按插件权限筛选"><option value="all">全部权限</option><option v-for="(label, key) in pL" :key="key" :value="key">{{ label }}</option></select></div>
      <div class="security-permission-list"><button v-for="plugin in permissionRows" :key="`${plugin.kind}:${plugin.id}`" @click="openPlugin(plugin)"><span class="security-permission-name"><b>{{ plugin.name }}</b><small>{{ plugin.origin === 'builtin' ? '内置' : '用户' }} · {{ plugin.kind === 'triggers' ? '触发器' : '动作' }}{{ plugin.enabled ? '' : ' · 已停用' }}</small></span><span class="security-permission-chips"><span v-for="perm in plugin.perms" :key="perm" class="chip" :class="pC[perm] || 'chip-unknown'"><span class="material-symbols-outlined">{{ pI[perm] || 'help' }}</span>{{ pL[perm] || perm }}</span><span v-if="!plugin.perms.length" class="chip chip-clean">无特殊权限</span></span><span class="material-symbols-outlined">chevron_right</span></button></div>
      <p v-if="!permissionRows.length" class="security-muted">没有符合筛选条件的插件。</p>
    </details>

    <BaseDialog :open="showReinstall" @close="showReinstall = false">
      <section class="security-reinstall-dialog" role="dialog" aria-modal="true" aria-labelledby="reinstall-title">
        <span class="material-symbols-outlined dialog-ico">install_desktop</span><h3 id="reinstall-title">重新安装 NotmyFault</h3>
        <p>从可信来源获取完整的 NotmyFault，替换损坏的应用安装文件。</p>
        <ol><li>退出 NotmyFault。</li><li>保留用户数据目录，里面包含设置、规则、插件和验证密钥。<code v-if="configSec.config_dir">{{ configSec.config_dir }}</code></li><li>按项目安装说明重新安装，启动后回到这里重新检查。</li></ol>
        <details class="security-details"><summary>正在开发源码？</summary><p>确认改动来自你本人后，重新构建以更新签名。开发构建可使用：</p><code>python build.py build --security-mode=permissive</code></details>
        <footer><button class="btn btn-text" @click="showReinstall = false">关闭</button><a class="btn btn-filled" href="https://github.com/cuteaplane/notmyfault#readme" target="_blank" rel="noopener noreferrer">打开安装说明<span class="material-symbols-outlined">open_in_new</span></a></footer>
      </section>
    </BaseDialog>
  </section>
</template>

<style scoped>
.config-security-rule { flex-wrap: wrap; }
.config-security-rule-details { flex-basis: 100%; display: grid; gap: 12px; min-width: 0; }
.config-security-rule-params { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
.config-security-rule-params code { flex-basis: 100%; white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
