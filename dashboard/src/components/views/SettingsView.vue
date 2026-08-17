<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { store } from '../../lib/store'
import {
  getAdminAuthorizationSetting,
  getAdminRuleVerificationSetting,
  getAIDraftingSetting,
  getBluetoothSetting,
  installBluetoothPlugin,
  deleteAIApiKey,
  saveAIApiKey,
  updateAdminAuthorizationSetting,
  updateAdminRuleVerificationSetting,
  updateAIDraftingSetting,
  uninstallBluetoothPlugin,
} from '../../lib/api'
import { alertDialog } from '../../lib/dialog'
import { snackbar } from '../../lib/notify'
import { appLogoUrl } from '../../lib/branding'
import { aiProviderIdFor, aiProviderPresets } from '../../lib/providers'
import OriginDialog from '../OriginDialog.vue'

const adminAuth = ref({ mode: 'per_execution', supported_modes: ['per_execution'], restart_required: false })
const adminRuleVerification = ref(true)
const savingAdminAuth = ref(false)
const savingAI = ref(false)
const savingAIKey = ref(false)
const savingVerification = ref(false)
const bluetooth = ref({ available: false, installed: false, meta: null })
const savingBluetooth = ref(false)
const selectedAIProvider = computed({
  get: () => aiProviderIdFor(store.aiDrafting),
  set: providerId => {
    const preset = aiProviderPresets.find(item => item.id === providerId)
    if (!preset) return
    store.aiDrafting.endpoint_url = preset.endpointUrl
    store.aiDrafting.model = preset.model
    store.aiDrafting.api_format = preset.apiFormat
  },
})

const authOptions = [
  { mode: 'engine_start', icon: 'verified_user', title: '引擎启动时授权一次', sub: '启动阶段确认一次，本次引擎运行期间复用管理员代理。' },
  { mode: 'per_execution', icon: 'touch_app', title: '每次执行时确认', sub: '每次管理员命令都需单独确认，未确认不执行。' },
]

const aiApiKeyStatusInfo = computed(() => ({
  none: { label: '未保存', description: '输入密钥后可仅用于本次会话，也可以安全保存到系统密钥库。' },
  saved: { label: '已保存', description: '已保存到系统密钥库，生成草稿时会自动使用。' },
  unsupported: { label: '不支持保存', description: '当前平台不支持安全保存密钥，仍可仅用于本次 Dashboard 会话。' },
  corrupt: { label: '需要处理', description: '保存的密钥无法读取，请替换或删除该保存项。' },
}[store.aiApiKeyStatus] || {
  label: '未保存', description: '输入密钥后可仅用于本次会话，也可以安全保存到系统密钥库。',
}))

const aiApiKeyPersistenceUnsupported = computed(() => store.aiApiKeyStatus === 'unsupported')
const canDeleteSavedAIKey = computed(() => ['saved', 'corrupt'].includes(store.aiApiKeyStatus))
const aiApiKeySaveLabel = computed(() => (
  store.aiApiKeyStatus === 'saved' || store.aiApiKeyStatus === 'corrupt'
    ? '替换 API 密钥'
    : '保存 API 密钥'
))

async function load() {
  try {
    adminAuth.value = { ...adminAuth.value, ...await getAdminAuthorizationSetting() }
    adminRuleVerification.value = (await getAdminRuleVerificationSetting()).key_verification === true
    const { api_key_status: aiApiKeyStatus, ...aiDrafting } = await getAIDraftingSetting()
    store.aiDrafting = { ...store.aiDrafting, ...aiDrafting }
    store.aiApiKeyStatus = aiApiKeyStatus || 'none'
    bluetooth.value = await getBluetoothSetting()
  } catch { snackbar('无法读取设置') }
}
async function selectAdminAuthorization(mode) {
  if (savingAdminAuth.value || mode === adminAuth.value.mode) return
  savingAdminAuth.value = true
  try {
    const result = await updateAdminAuthorizationSetting(mode)
    if (!result.ok) return alertDialog('保存失败', result.error || '无法保存管理员授权方式')
    adminAuth.value = { ...adminAuth.value, ...result }
    snackbar(result.restart_required ? '已保存，重启引擎后生效' : '管理员授权方式已保存')
  } catch (error) { alertDialog('保存失败', error.message) } finally { savingAdminAuth.value = false }
}
async function toggleAdminRuleVerification() {
  if (savingVerification.value) return
  savingVerification.value = true
  try {
    const result = await updateAdminRuleVerificationSetting(!adminRuleVerification.value)
    if (!result.ok) return alertDialog('保存失败', result.error || '无法保存验证设置')
    adminRuleVerification.value = result.key_verification === true
    snackbar(adminRuleVerification.value ? '已开启：创建管理员规则时要求验证签名私钥' : '已关闭：创建管理员规则不再要求验证签名私钥')
  } catch (error) { alertDialog('保存失败', error.message) } finally { savingVerification.value = false }
}
async function saveAI() {
  savingAI.value = true
  try {
    const result = await updateAIDraftingSetting(store.aiDrafting)
    if (!result.ok) return alertDialog('保存失败', result.error || '无法保存 AI 设置')
    store.aiDrafting = { ...store.aiDrafting, ...result.settings }
    snackbar('AI 设置已保存')
  } catch (error) { alertDialog('保存失败', error.message) } finally { savingAI.value = false }
}
async function saveAIKey() {
  if (savingAIKey.value || aiApiKeyPersistenceUnsupported.value || !store.aiApiKey) return
  savingAIKey.value = true
  try {
    const result = await saveAIApiKey(store.aiApiKey)
    if (!result.ok) return alertDialog('保存失败', result.error || '无法保存 AI API 密钥')
    store.aiApiKeyStatus = result.api_key_status || 'saved'
    store.aiApiKey = ''
    snackbar('AI API 密钥已安全保存')
  } catch (error) { alertDialog('保存失败', error.message) } finally { savingAIKey.value = false }
}
async function deleteAIKey() {
  if (savingAIKey.value || !canDeleteSavedAIKey.value) return
  savingAIKey.value = true
  try {
    const result = await deleteAIApiKey()
    if (!result.ok) return alertDialog('删除失败', result.error || '无法删除 AI API 密钥')
    store.aiApiKeyStatus = result.api_key_status || 'none'
    snackbar('已删除保存的 AI API 密钥')
  } catch (error) { alertDialog('删除失败', error.message) } finally { savingAIKey.value = false }
}
async function changeBluetoothInstallation() {
  if (savingBluetooth.value || !bluetooth.value.available) return
  savingBluetooth.value = true
  try {
    const result = bluetooth.value.installed
      ? await uninstallBluetoothPlugin()
      : await installBluetoothPlugin()
    if (!result.ok) return alertDialog('操作失败', result.error || '无法更改蓝牙插件')
    bluetooth.value = await getBluetoothSetting()
    snackbar(bluetooth.value.installed ? '蓝牙开关已安装，重启引擎后生效' : '蓝牙开关已移除，重启引擎后生效')
  } catch (error) { alertDialog('操作失败', error.message) } finally { savingBluetooth.value = false }
}
onMounted(load)

const appVersion = __APP_VERSION__

// 科乐美秘技：在设置页依次输入序列解锁起源彩蛋，按错即重置。
const KONAMI_SEQUENCE = [
  'ArrowUp', 'ArrowUp', 'ArrowDown', 'ArrowDown',
  'ArrowLeft', 'ArrowRight', 'ArrowLeft', 'ArrowRight', 'b', 'a',
]
let konamiProgress = 0
const showOrigin = ref(false)
function onKonamiKey(event) {
  const key = event.key.length === 1 ? event.key.toLowerCase() : event.key
  if (key === KONAMI_SEQUENCE[konamiProgress]) {
    konamiProgress++
    if (konamiProgress === KONAMI_SEQUENCE.length) {
      konamiProgress = 0
      showOrigin.value = true
    }
  } else {
    konamiProgress = key === KONAMI_SEQUENCE[0] ? 1 : 0
  }
}
onMounted(() => window.addEventListener('keydown', onKonamiKey))
onUnmounted(() => window.removeEventListener('keydown', onKonamiKey))

const ruleCount = computed(() => store.configData?.rules?.length || 0)
const triggerCount = computed(() => Object.keys(store.pluginsData?.triggers || {}).length)
const actionCount = computed(() => Object.keys(store.pluginsData?.actions || {}).length)
const modeLabel = computed(() => {
  const m = store.engineStatus?.security_mode
  return ({ strict: '严格', normal: '标准', permissive: '宽松' })[m] || '未知'
})

const runtimeInfo = computed(() => [
  { icon: 'info', key: '版本', val: 'NotmyFault ' + appVersion },
  { icon: 'shield', key: '安全模式', val: modeLabel.value },
  { icon: 'rule', key: '已配置规则', val: ruleCount.value + ' 条' },
  { icon: 'memory', key: '触发器插件', val: triggerCount.value ? triggerCount.value + ' 个' : '引擎离线时不可用' },
  { icon: 'bolt', key: '动作插件', val: actionCount.value ? actionCount.value + ' 个' : '引擎离线时不可用' },
  { icon: 'lan', key: '本地 API', val: '127.0.0.1:19198 · 本机令牌认证' },
])

const platforms = [
  { icon: 'desktop_windows', name: 'Windows', state: '主要开发平台', cls: 'text-success' },
  { icon: 'terminal', name: 'Linux', state: '实验性支持', cls: 'text-warn' },
  { icon: 'laptop_mac', name: 'macOS', state: '暂不支持', cls: 'text-outline' },
]

const techStack = ['Python 引擎', 'Vue 3', 'FastAPI 本地服务', 'SSE 事件推送']

// 搜索命中才算可见：标题、副标题和右侧值都参与匹配。
const q = ref('')
const query = computed(() => q.value.trim().toLowerCase())
const hit = (...parts) => !query.value || parts.join(' ').toLowerCase().includes(query.value)
const showSuggest = computed(() => !query.value)
const showAuth = computed(() => hit(
  '授权与安全', '管理员授权方式',
  ...authOptions.flatMap(o => [o.title, o.sub]),
  '管理员规则验证', '创建管理员规则时要求验证签名私钥',
))
const showAi = computed(() => hit(
  '实验功能', '实验性 AI 规则草稿', 'AI 只生成候选草稿，由你检查后手动保存',
  '服务商', ...aiProviderPresets.map(provider => provider.label),
  'OpenAI 兼容地址', '模型', '接口格式', 'AI API 密钥', '保存 API 密钥', '删除已保存的密钥', '保存 AI 设置',
))
const showBluetooth = computed(() => hit(
  '可选插件', '蓝牙开关', '安装蓝牙开关', '用户插件',
))
const showAbout = computed(() => hit(
  '关于 NotmyFault', 'NotmyFault', '拓展万千', '平台支持', '技术栈', '配置与日志',
  ...runtimeInfo.value.flatMap(r => [r.key, r.val]),
  ...platforms.flatMap(p => [p.name, p.state]),
))
const noResults = computed(() => query.value !== '' && !showAuth.value && !showAi.value && !showBluetooth.value && !showAbout.value)
// 列表行直接按命中过滤后再渲染，v-show 挂在 v-for 行上会被编译成稳定片段，查询变化时不重算。
const visibleRuntimeInfo = computed(() => runtimeInfo.value.filter(r => hit(r.key, r.val)))
const visiblePlatforms = computed(() => platforms.filter(p => hit('平台支持', p.name, p.state)))

const suggestions = computed(() => [
  { icon: 'shield', cls: 'a16-ico-primary', title: '安全模式', val: modeLabel.value, page: 'security' },
  { icon: 'rule', cls: 'a16-ico-tertiary', title: '自动化规则', val: ruleCount.value + ' 条', page: 'rules' },
  { icon: 'extension', cls: 'a16-ico-success', title: '插件', val: actionCount.value || triggerCount.value ? triggerCount.value + ' 触发器 · ' + actionCount.value + ' 动作' : '引擎离线时不可用', page: 'plugins' },
])

function openPage(page) {
  if (window.__nmf) window.__nmf.switchPage(page)
}
function scrollToAbout() {
  document.querySelector('.settings-about-card')?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
}
</script>

<template>
  <section class="page active settings-page">
    <div class="a16-canvas">
      <header class="a16-head">
        <h2>设置</h2>
        <button type="button" class="a16-profile" title="查看关于信息" @click="scrollToAbout">
          <img :src="appLogoUrl" alt="">
          <span class="a16-profile-body"><b>NotmyFault</b><small>{{ appVersion }} · Alpha</small></span>
          <span class="material-symbols-outlined">expand_more</span>
        </button>
      </header>

      <label class="a16-search">
        <span class="material-symbols-outlined">search</span>
        <input v-model="q" type="search" aria-label="搜索设置" autocomplete="off" spellcheck="false" placeholder="搜索设置">
        <button v-if="q" type="button" class="a16-search-clear" aria-label="清空搜索" @click="q = ''"><span class="material-symbols-outlined">close</span></button>
      </label>

      <div v-show="showSuggest" class="a16-suggest">
        <button v-for="s in suggestions" :key="s.title" type="button" class="a16-suggest-card" @click="openPage(s.page)">
          <span class="a16-suggest-ico" :class="s.cls"><span class="material-symbols-outlined">{{ s.icon }}</span></span>
          <span class="a16-suggest-body"><b>{{ s.title }}</b><small>{{ s.val }}</small></span>
        </button>
      </div>

      <div v-show="showAuth">
        <h3 class="a16-section-label">授权与安全</h3>
        <section class="a16-card">
          <button v-for="o in authOptions" :key="o.mode" type="button"
            class="a16-row admin-auth-option" :class="{ selected: adminAuth.mode === o.mode }"
            :disabled="savingAdminAuth || (o.mode === 'engine_start' && !adminAuth.supported_modes.includes('engine_start'))"
            @click="selectAdminAuthorization(o.mode)">
            <span class="material-symbols-outlined a16-row-ico">{{ o.icon }}</span>
            <span class="a16-row-body"><span class="a16-row-title">{{ o.title }}</span><span class="a16-row-sub">{{ o.sub }}</span></span>
            <span class="material-symbols-outlined a16-radio">{{ adminAuth.mode === o.mode ? 'radio_button_checked' : 'radio_button_unchecked' }}</span>
          </button>
          <div class="a16-row admin-rule-verification-settings">
            <span class="material-symbols-outlined a16-row-ico">key</span>
            <span class="a16-row-body">
              <span class="a16-row-title">管理员规则验证</span>
              <span class="a16-row-sub">创建管理员规则时要求验证签名私钥</span>
            </span>
            <label class="switch">
              <input type="checkbox" :checked="adminRuleVerification" :disabled="savingVerification" @change="toggleAdminRuleVerification">
              <span class="switch-track"><span class="switch-thumb"></span></span>
            </label>
          </div>
          <p v-if="adminAuth.restart_required" class="a16-note"><span class="material-symbols-outlined">restart_alt</span>重启引擎后生效。</p>
        </section>
      </div>

      <div v-show="showAi">
        <h3 class="a16-section-label">实验功能</h3>
        <section class="a16-card ai-drafting-settings">
          <div class="a16-row">
            <span class="material-symbols-outlined a16-row-ico">auto_awesome</span>
            <span class="a16-row-body">
              <span class="a16-row-title">实验性 AI 规则草稿</span>
              <span class="a16-row-sub">AI 只生成候选草稿，由你检查后手动保存</span>
            </span>
            <label class="switch">
              <input v-model="store.aiDrafting.enabled" type="checkbox">
              <span class="switch-track"><span class="switch-thumb"></span></span>
            </label>
          </div>
          <div class="a16-panel">
            <div class="a16-fields">
               <label class="a16-field">服务商<select v-model="selectedAIProvider" name="ai-provider" class="text-field"><option v-for="provider in aiProviderPresets" :key="provider.id" :value="provider.id">{{ provider.label }}</option><option value="custom">自定义</option></select></label>
               <label class="a16-field">OpenAI 兼容地址<input v-model="store.aiDrafting.endpoint_url" name="ai-endpoint" class="text-field" type="url" placeholder="https://example.com/v1"></label>
               <label class="a16-field">模型<input v-model="store.aiDrafting.model" name="ai-model" class="text-field" placeholder="model-name"></label>
               <label class="a16-field">接口格式<select v-model="store.aiDrafting.api_format" name="ai-api-format" class="text-field"><option value="chat_completions">Chat Completions</option><option value="responses">Responses API</option></select></label>
             </div>
             <div class="a16-row">
               <span class="material-symbols-outlined a16-row-ico">key</span>
               <span class="a16-row-body">
                 <span class="a16-row-title">AI API 密钥</span>
                 <span class="a16-row-sub">{{ aiApiKeyStatusInfo.description }}</span>
               </span>
               <span class="a16-row-val">{{ aiApiKeyStatusInfo.label }}</span>
             </div>
             <label class="a16-field">本次会话 API 密钥<input v-model="store.aiApiKey" class="text-field" type="password" autocomplete="off" placeholder="可仅用于本次会话，或安全保存"></label>
             <p v-if="store.aiApiKeyStatus === 'corrupt'" class="a16-note"><span class="material-symbols-outlined">warning</span>请输入新的密钥后替换，或删除损坏的保存项后重新保存。</p>
             <p v-else class="a16-note">发送草稿请求时会将你的描述和可用插件名称发往配置的远程服务。保存的密钥不会显示在 Dashboard 中。</p>
             <div class="a16-actions">
               <button class="btn btn-filled" :disabled="savingAI" @click="saveAI">{{ savingAI ? '保存中…' : '保存 AI 设置' }}</button>
               <button class="btn btn-tonal" :disabled="savingAIKey || aiApiKeyPersistenceUnsupported || !store.aiApiKey" @click="saveAIKey">{{ savingAIKey ? '保存中…' : aiApiKeySaveLabel }}</button>
               <button v-if="canDeleteSavedAIKey" class="btn btn-outlined" :disabled="savingAIKey" @click="deleteAIKey">删除已保存的密钥</button>
             </div>
           </div>
        </section>
      </div>

      <div v-show="showBluetooth">
        <h3 class="a16-section-label">可选插件</h3>
        <section class="a16-card bluetooth-settings">
          <div class="a16-row">
            <span class="material-symbols-outlined a16-row-ico">bluetooth</span>
            <span class="a16-row-body">
              <span class="a16-row-title">蓝牙开关</span>
              <span class="a16-row-sub">{{ bluetooth.installed ? '已安装到用户插件目录' : bluetooth.available ? '需要时安装，不随引擎默认加载' : '当前安装包未包含此插件' }}</span>
            </span>
            <span class="a16-row-val">{{ bluetooth.installed ? '已安装' : '未安装' }}</span>
          </div>
          <div class="a16-panel">
            <p class="a16-note">安装后可在规则中开启、关闭或查询蓝牙状态。更改安装状态后需要重启引擎。</p>
            <div class="a16-actions"><button class="btn btn-filled" :disabled="savingBluetooth || !bluetooth.available" @click="changeBluetoothInstallation">{{ savingBluetooth ? '处理中…' : bluetooth.installed ? '移除蓝牙开关' : '安装蓝牙开关' }}</button></div>
          </div>
        </section>
      </div>

      <div v-show="showAbout">
        <h3 class="a16-section-label">关于 NotmyFault</h3>
        <section class="a16-card settings-about-card">
          <header class="a16-brand">
            <img class="a16-brand-logo" :src="appLogoUrl" alt="">
            <span class="a16-brand-body"><b>NotmyFault</b><small>拓展万千</small></span>
            <span class="a16-badges"><span class="chip chip-clean">{{ appVersion }}</span><span class="chip">Alpha</span><span class="chip">GPL-3.0</span></span>
          </header>
          <div v-for="r in visibleRuntimeInfo" :key="r.key" class="a16-row">
            <span class="material-symbols-outlined a16-row-ico">{{ r.icon }}</span>
            <span class="a16-row-body"><span class="a16-row-title">{{ r.key }}</span></span>
            <span class="a16-row-val">{{ r.val }}</span>
          </div>
          <p class="a16-sub-label">平台支持</p>
          <div v-for="p in visiblePlatforms" :key="p.name" class="a16-row a16-row-sm">
            <span class="material-symbols-outlined a16-row-ico">{{ p.icon }}</span>
            <span class="a16-row-body"><span class="a16-row-title">{{ p.name }}</span></span>
            <span class="a16-row-val" :class="p.cls">{{ p.state }}</span>
          </div>
          <p class="a16-note">配置与日志位于 <code>%APPDATA%\NotmyFault\</code>，Linux 为 <code>~/.config/notmyfault/</code>。</p>
          <p class="a16-sub-label">技术栈</p>
          <div class="a16-stack"><span v-for="t in techStack" :key="t">{{ t }}</span></div>
          <footer class="a16-foot">
            <span>&copy; 2026 NotmyFault Project</span>
            <span>问题反馈请附系统版本、复现步骤与最新日志</span>
          </footer>
        </section>
      </div>

      <p v-if="noResults" class="a16-empty">没有找到与“{{ q.trim() }}”相关的设置</p>
    </div>

    <OriginDialog :open="showOrigin" @close="showOrigin = false" />
  </section>
</template>
