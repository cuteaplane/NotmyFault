<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { store } from '../../lib/store'
import {
  deleteAIApiKey,
  getAIDraftingSetting,
  readPlatformCapabilities,
  saveAIApiKey,
  updateAIDraftingSetting,
} from '../../lib/api'
import { alertDialog } from '../../lib/dialog'
import { snackbar } from '../../lib/notify'
import { appLogoUrl } from '../../lib/branding'
import { aiProviderIdFor, aiProviderPresets } from '../../lib/providers'
import { useTheme } from '../../composables/useTheme'
import LogsView from './LogsView.vue'
import SecurityView from './SecurityView.vue'
import OriginDialog from '../OriginDialog.vue'

const validPages = new Set(['general', 'ai', 'ai-service', 'ai-key', 'security', 'diagnostics', 'about'])
const currentPage = ref(validPages.has(store.pendingSettingsSection) ? store.pendingSettingsSection : 'general')
store.pendingSettingsSection = ''
const q = ref('')
const platformReport = ref(null)
const savingAIFeature = ref(false)
const savingAIService = ref(false)
const savingAIKey = ref(false)
const aiSavedSnapshot = ref(null)
const aiServiceDraft = ref({ endpoint_url: '', model: '', api_format: 'chat_completions' })
const aiKeyEditorOpen = ref(false)
const aiApiKeyDraft = ref('')
const { isDark, toggle: toggleTheme } = useTheme()

const pageTitles = {
  general: '常规',
  ai: 'AI 功能',
  'ai-service': '服务配置',
  'ai-key': 'API 密钥',
  security: '安全与权限',
  diagnostics: '系统与诊断',
  about: '关于 NotmyFault',
}
const currentCategory = computed(() => currentPage.value.startsWith('ai-') ? 'ai' : currentPage.value)

const selectedAIProvider = computed({
  get: () => aiProviderIdFor(aiServiceDraft.value),
  set: providerId => {
    const preset = aiProviderPresets.find(item => item.id === providerId)
    if (!preset) return
    aiServiceDraft.value = {
      endpoint_url: preset.endpointUrl,
      model: preset.model,
      api_format: preset.apiFormat,
    }
  },
})
const aiServiceDirty = computed(() => {
  if (!aiSavedSnapshot.value) return false
  return ['endpoint_url', 'model', 'api_format'].some(key => (
    aiServiceDraft.value[key] !== aiSavedSnapshot.value[key]
  ))
})
const aiApiKeyPersistenceUnsupported = computed(() => store.aiApiKeyStatus === 'unsupported')
const canDeleteSavedAIKey = computed(() => ['saved', 'corrupt'].includes(store.aiApiKeyStatus))
const aiApiKeyStatusInfo = computed(() => ({
  none: { label: '未设置', icon: 'key_off', cls: '' },
  saved: { label: '已保存', icon: 'check_circle', cls: 'is-saved' },
  unsupported: { label: '不支持安全保存', icon: 'block', cls: 'is-warning' },
  corrupt: { label: '需要更改', icon: 'error', cls: 'is-error' },
}[store.aiApiKeyStatus] || { label: '未设置', icon: 'key_off', cls: '' }))

const appVersion = __APP_VERSION__
const ruleCount = computed(() => store.configData?.rules?.length || 0)
const triggerCount = computed(() => Object.keys(store.pluginsData?.triggers || {}).length)
const actionCount = computed(() => Object.keys(store.pluginsData?.actions || {}).length)
const modeLabel = computed(() => ({ strict: '严格', normal: '标准', permissive: '宽松' })[store.engineStatus?.security_mode] || '未知')
const aiProviderLabel = computed(() => {
  const provider = aiProviderPresets.find(item => item.id === aiProviderIdFor(aiSavedSnapshot.value || store.aiDrafting))
  return provider?.label || (store.aiDrafting.endpoint_url ? '自定义' : '未设置')
})
const capabilityRows = computed(() => Object.entries(platformReport.value?.capabilities || {}).map(([id, entry]) => ({
  id,
  available: entry.available === true,
  backend: entry.backend || '',
  state: entry.available ? (entry.degraded ? '部分可用' : '可用') : '不可用',
  reason: entry.reason || '',
  cls: entry.available ? (entry.degraded ? 'text-warn' : 'text-success') : 'text-error',
})))
const availableCapabilityCount = computed(() => capabilityRows.value.filter(row => row.available).length)

const settingsItems = computed(() => [
  { page: 'general', icon: 'tune', title: '常规', value: isDark.value ? '深色模式' : '浅色模式', tone: 'blue', terms: '主题 外观 运行' },
  { page: 'ai', icon: 'auto_awesome', title: 'AI 功能', value: store.aiDrafting.enabled ? '已开启' : '已关闭', tone: 'violet', terms: '规则 草稿 服务商 api 密钥 模型' },
  { page: 'security', icon: 'shield_lock', title: '安全与权限', value: modeLabel.value, tone: 'cyan', terms: '管理员 授权 签名 验证 插件 权限' },
  { page: 'diagnostics', icon: 'monitor_heart', title: '系统与诊断', value: store.engineStatus.engine_running ? '引擎运行中' : '查看日志', tone: 'amber', terms: '日志 错误 崩溃 启动 引擎 诊断' },
  { page: 'about', icon: 'info', title: '关于 NotmyFault', value: appVersion, tone: 'slate', terms: '版本 环境 平台 技术栈' },
])
const filteredSettingsItems = computed(() => {
  const query = q.value.trim().toLowerCase()
  if (!query) return settingsItems.value
  return settingsItems.value.filter(item => `${item.title} ${item.value} ${item.terms}`.toLowerCase().includes(query))
})
const runtimeInfo = computed(() => [
  { icon: 'info', key: '版本', val: 'NotmyFault ' + appVersion },
  { icon: 'shield', key: '安全模式', val: modeLabel.value },
  { icon: 'rule', key: '已配置规则', val: ruleCount.value + ' 条' },
  { icon: 'memory', key: '触发器插件', val: triggerCount.value ? triggerCount.value + ' 个' : '引擎离线时不可用' },
  { icon: 'bolt', key: '动作插件', val: actionCount.value ? actionCount.value + ' 个' : '引擎离线时不可用' },
  { icon: 'lan', key: 'NotmyFault API', val: '127.0.0.1:19198' },
])

function applyAISavedSettings(settings) {
  store.aiDrafting = { ...store.aiDrafting, ...settings }
  aiSavedSnapshot.value = { ...store.aiDrafting }
  aiServiceDraft.value = {
    endpoint_url: store.aiDrafting.endpoint_url,
    model: store.aiDrafting.model,
    api_format: store.aiDrafting.api_format,
  }
}

async function load() {
  try {
    const { api_key_status: aiApiKeyStatus, ...aiDrafting } = await getAIDraftingSetting()
    applyAISavedSettings(aiDrafting)
    store.aiApiKeyStatus = aiApiKeyStatus || 'none'
  } catch { snackbar('无法读取设置') }
}

async function toggleAIDrafting() {
  if (savingAIFeature.value) return
  savingAIFeature.value = true
  try {
    const result = await updateAIDraftingSetting({
      ...(aiSavedSnapshot.value || store.aiDrafting),
      enabled: !store.aiDrafting.enabled,
    })
    if (!result.ok) return alertDialog('保存失败', result.error || '无法保存 AI 设置')
    applyAISavedSettings(result.settings)
    snackbar(store.aiDrafting.enabled ? 'AI 规则草稿已开启' : 'AI 规则草稿已关闭')
  } catch (error) { alertDialog('保存失败', error.message) } finally { savingAIFeature.value = false }
}

async function saveAIService() {
  if (savingAIService.value || !aiServiceDirty.value) return
  savingAIService.value = true
  try {
    const result = await updateAIDraftingSetting({
      ...(aiSavedSnapshot.value || store.aiDrafting),
      ...aiServiceDraft.value,
    })
    if (!result.ok) return alertDialog('保存失败', result.error || '无法保存 AI 设置')
    applyAISavedSettings(result.settings)
    snackbar('服务配置已保存')
  } catch (error) { alertDialog('保存失败', error.message) } finally { savingAIService.value = false }
}

function editAIKey() {
  aiApiKeyDraft.value = ''
  aiKeyEditorOpen.value = true
}

function cancelAIKeyEdit() {
  aiApiKeyDraft.value = ''
  aiKeyEditorOpen.value = false
}

async function saveAIKey() {
  if (savingAIKey.value || aiApiKeyPersistenceUnsupported.value || !aiApiKeyDraft.value) return
  savingAIKey.value = true
  try {
    const result = await saveAIApiKey(aiApiKeyDraft.value)
    if (!result.ok) return alertDialog('保存失败', result.error || '无法保存 AI API 密钥')
    store.aiApiKeyStatus = result.api_key_status || 'saved'
    cancelAIKeyEdit()
    snackbar('API 密钥已保存')
  } catch (error) { alertDialog('保存失败', error.message) } finally { savingAIKey.value = false }
}

async function deleteAIKey() {
  if (savingAIKey.value || !canDeleteSavedAIKey.value) return
  savingAIKey.value = true
  try {
    const result = await deleteAIApiKey()
    if (!result.ok) return alertDialog('删除失败', result.error || '无法删除 AI API 密钥')
    store.aiApiKeyStatus = result.api_key_status || 'none'
    cancelAIKeyEdit()
    snackbar('API 密钥已删除')
  } catch (error) { alertDialog('删除失败', error.message) } finally { savingAIKey.value = false }
}

function openSettingsPage(page) {
  if (!validPages.has(page)) return
  currentPage.value = page
}

function goBackFromNestedPage() {
  currentPage.value = 'ai'
}

watch(() => store.pendingSettingsSection, section => {
  if (!validPages.has(section)) return
  currentPage.value = section
  store.pendingSettingsSection = ''
})

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

onMounted(async () => {
  load()
  platformReport.value = await readPlatformCapabilities()
  window.addEventListener('keydown', onKonamiKey)
})
onUnmounted(() => {
  aiApiKeyDraft.value = ''
  window.removeEventListener('keydown', onKonamiKey)
})
</script>

<template>
  <section class="page active settings-page settings-tablet-page">
    <div class="page-head settings-page-head"><div><h2>设置</h2><p class="page-subtitle">管理 NotmyFault 的功能、安全与运行信息</p></div></div>
    <div class="settings-tablet-shell">
      <aside class="settings-category-pane">
        <label class="a16-search settings-search">
          <span class="material-symbols-outlined">search</span>
          <input v-model="q" type="search" aria-label="搜索设置" autocomplete="off" spellcheck="false" placeholder="搜索设置">
          <button v-if="q" type="button" class="a16-search-clear" aria-label="清空搜索" @click="q = ''"><span class="material-symbols-outlined">close</span></button>
        </label>
        <nav v-if="filteredSettingsItems.length" class="settings-category-list settings-root-list" aria-label="设置分类">
          <button v-for="item in filteredSettingsItems" :key="item.page" type="button" class="settings-category-row" :class="{ active: currentCategory === item.page }" :aria-current="currentCategory === item.page ? 'page' : undefined" @click="openSettingsPage(item.page)">
            <span class="settings-category-icon material-symbols-outlined" :class="`tone-${item.tone}`">{{ item.icon }}</span>
            <span class="settings-category-copy"><b>{{ item.title }}</b><small>{{ item.value }}</small></span>
            <span class="material-symbols-outlined settings-chevron">chevron_right</span>
          </button>
        </nav>
        <p v-else class="a16-empty">没有找到设置</p>
      </aside>

      <div class="settings-detail-pane">
        <Transition name="settings-detail" mode="out-in">
        <div :key="currentPage" class="settings-detail-stage" tabindex="0" role="region" :aria-label="pageTitles[currentPage]">
        <header class="settings-detail-head">
          <button v-if="currentPage.startsWith('ai-')" type="button" class="icon-btn settings-back" aria-label="返回 AI 功能" @click="goBackFromNestedPage"><span class="material-symbols-outlined">arrow_back</span></button>
          <div><h3>{{ pageTitles[currentPage] }}</h3><p v-if="currentPage === 'diagnostics'">查看引擎状态、启动失败、插件崩溃和会话日志。</p></div>
        </header>

        <section v-if="currentPage === 'general'" class="settings-subpage">
          <div class="a16-card">
            <button type="button" class="a16-row" @click="toggleTheme">
              <span class="material-symbols-outlined a16-row-ico">{{ isDark ? 'dark_mode' : 'light_mode' }}</span>
              <span class="a16-row-body"><span class="a16-row-title">外观</span><span class="a16-row-sub">切换整个 Dashboard 的明暗主题</span></span>
              <span class="a16-row-val">{{ isDark ? '深色' : '浅色' }}</span>
              <span class="material-symbols-outlined settings-chevron">swap_horiz</span>
            </button>
            <div class="a16-row">
              <span class="material-symbols-outlined a16-row-ico">memory</span>
              <span class="a16-row-body"><span class="a16-row-title">引擎</span><span class="a16-row-sub">自动化运行状态</span></span>
              <span class="a16-row-val">{{ store.engineStatus.engine_running ? '运行中' : store.engineStatus.api_alive ? '已暂停' : '离线' }}</span>
            </div>
          </div>
        </section>

        <section v-else-if="currentPage === 'ai'" class="settings-subpage ai-drafting-settings">
          <div class="a16-card">
            <div class="a16-row">
              <span class="material-symbols-outlined a16-row-ico">auto_awesome</span>
              <span class="a16-row-body"><span class="a16-row-title">AI 规则草稿</span><span class="a16-row-sub">在自动化编辑器中根据描述生成草稿</span></span>
              <label class="switch"><input type="checkbox" :checked="store.aiDrafting.enabled" :disabled="savingAIFeature" @change="toggleAIDrafting"><span class="switch-track"><span class="switch-thumb"></span></span></label>
            </div>
            <button type="button" class="a16-row" @click="openSettingsPage('ai-service')">
              <span class="material-symbols-outlined a16-row-ico">dns</span><span class="a16-row-body"><span class="a16-row-title">服务配置</span></span>
              <span class="a16-row-val">{{ aiServiceDirty ? '未保存' : aiProviderLabel }}</span><span class="material-symbols-outlined settings-chevron">chevron_right</span>
            </button>
            <button type="button" class="a16-row" @click="openSettingsPage('ai-key')">
              <span class="material-symbols-outlined a16-row-ico">key</span><span class="a16-row-body"><span class="a16-row-title">API 密钥</span></span>
              <span class="a16-row-val" :class="{ 'settings-state-success': store.aiApiKeyStatus === 'saved' }">{{ aiApiKeyStatusInfo.label }}</span><span class="material-symbols-outlined settings-chevron">chevron_right</span>
            </button>
          </div>
        </section>

        <section v-else-if="currentPage === 'ai-service'" class="settings-subpage ai-drafting-settings">
          <div class="a16-card settings-form-card">
            <div class="a16-fields">
              <label class="a16-field">服务商<select v-model="selectedAIProvider" name="ai-provider" class="text-field"><option v-for="provider in aiProviderPresets" :key="provider.id" :value="provider.id">{{ provider.label }}</option><option value="custom">自定义</option></select></label>
              <label class="a16-field">服务商 API 兼容地址<input v-model="aiServiceDraft.endpoint_url" name="ai-endpoint" class="text-field" type="url" placeholder="https://example.com/v1"></label>
              <label class="a16-field">模型<input v-model="aiServiceDraft.model" name="ai-model" class="text-field" placeholder="model-name"></label>
              <label class="a16-field">接口格式<select v-model="aiServiceDraft.api_format" name="ai-api-format" class="text-field"><option value="chat_completions">Chat Completions</option><option value="responses">Responses API</option></select></label>
            </div>
            <div v-if="aiServiceDirty" class="settings-save-bar"><span>有未保存的更改</span><button class="btn btn-filled" :disabled="savingAIService" @click="saveAIService">{{ savingAIService ? '保存中…' : '保存' }}</button></div>
          </div>
        </section>

        <section v-else-if="currentPage === 'ai-key'" class="settings-subpage ai-drafting-settings">
          <div class="a16-card settings-key-card">
            <div class="settings-key-status" :class="aiApiKeyStatusInfo.cls"><span class="material-symbols-outlined">{{ aiApiKeyStatusInfo.icon }}</span><strong>{{ aiApiKeyStatusInfo.label }}</strong></div>
            <template v-if="aiKeyEditorOpen">
              <label class="a16-field settings-key-input">API 密钥<input v-model="aiApiKeyDraft" class="text-field" type="password" autocomplete="off"></label>
              <div class="settings-key-actions"><button class="btn btn-text" :disabled="savingAIKey" @click="cancelAIKeyEdit">取消</button><button v-if="!aiApiKeyPersistenceUnsupported" class="btn btn-filled" :disabled="savingAIKey || !aiApiKeyDraft" @click="saveAIKey">{{ savingAIKey ? '保存中…' : '保存 API 密钥' }}</button></div>
            </template>
            <div v-else class="settings-key-actions"><button class="btn btn-filled" :disabled="aiApiKeyPersistenceUnsupported" @click="editAIKey">{{ canDeleteSavedAIKey ? '更改 API 密钥' : '添加 API 密钥' }}</button><button v-if="canDeleteSavedAIKey" class="btn btn-outlined" :disabled="savingAIKey" @click="deleteAIKey">删除 API 密钥</button></div>
          </div>
        </section>

        <section v-else-if="currentPage === 'security'" class="settings-subpage settings-security-section">
          <SecurityView embedded />
        </section>

        <section v-else-if="currentPage === 'diagnostics'" class="settings-subpage settings-diagnostics-section">
          <LogsView embedded initial-tab="logs" :show-tabs="false" />
        </section>

        <section v-else-if="currentPage === 'about'" class="settings-subpage">
          <div class="a16-card settings-about-card">
            <header class="a16-brand"><img class="a16-brand-logo" :src="appLogoUrl" alt=""><span class="a16-brand-body"><b>NotmyFault</b><small>{{ appVersion }}</small></span><span class="a16-badges"><span class="chip">Alpha</span><span class="chip">GPL-3.0</span></span></header>
            <div v-for="item in runtimeInfo" :key="item.key" class="a16-row"><span class="material-symbols-outlined a16-row-ico">{{ item.icon }}</span><span class="a16-row-body"><span class="a16-row-title">{{ item.key }}</span></span><span class="a16-row-val">{{ item.val }}</span></div>
            <details v-if="capabilityRows.length" class="settings-capability-report">
              <summary><span class="material-symbols-outlined a16-row-ico">memory</span><span class="a16-row-body"><span class="a16-row-title">NotmyFault Platform</span><span class="a16-row-sub">{{ platformReport.platform }}{{ platformReport.session_type ? ' · ' + platformReport.session_type : '' }}</span></span><span class="a16-row-val">{{ availableCapabilityCount }} / {{ capabilityRows.length }} 可用</span><span class="material-symbols-outlined settings-chevron">expand_more</span></summary>
              <div class="settings-capability-list"><div v-for="row in capabilityRows" :key="row.id" class="a16-row a16-row-sm" :title="row.reason || row.backend"><span class="a16-row-body"><span class="a16-row-title">{{ row.id }}</span></span><span class="a16-row-val" :class="row.cls">{{ row.state }}{{ row.backend ? ' · ' + row.backend : '' }}</span></div></div>
            </details>
            <footer class="a16-foot"><span>&copy; 2026 NotmyFault Project</span></footer>
          </div>
        </section>
        </div>
        </Transition>
      </div>
    </div>
    <OriginDialog :open="showOrigin" @close="showOrigin = false" />
  </section>
</template>
