<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { store } from '../../lib/store'
import {
  getAIDraftingSetting,
  deleteAIApiKey,
  saveAIApiKey,
  updateAIDraftingSetting,
} from '../../lib/api'
import { alertDialog } from '../../lib/dialog'
import { snackbar } from '../../lib/notify'
import { appLogoUrl } from '../../lib/branding'
import { aiProviderIdFor, aiProviderPresets } from '../../lib/providers'
import OriginDialog from '../OriginDialog.vue'

const savingAIFeature = ref(false)
const savingAIService = ref(false)
const savingAIKey = ref(false)
const aiSavedSnapshot = ref(null)
const aiServiceDraft = ref({ endpoint_url: '', model: '', api_format: 'chat_completions' })
const aiKeyEditorOpen = ref(false)
const aiApiKeyDraft = ref('')
const currentPage = ref('root')
const settingsTransition = ref('settings-forward')
const q = ref('')

const pageTitles = {
  root: '设置',
  auth: '安全与授权',
  ai: 'AI 功能',
  'ai-service': '服务配置',
  'ai-key': 'API 密钥',
  about: '关于 NotmyFault',
}

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

const rootItems = computed(() => [
  { page: 'auth', icon: 'shield_lock', title: '安全与授权', value: '每次执行时确认', terms: '管理员 授权 签名 验证' },
  { page: 'ai', icon: 'auto_awesome', title: 'AI 功能', value: store.aiDrafting.enabled ? '已开启' : '已关闭', terms: '规则 草稿 服务商 api 密钥 模型' },
  { page: 'about', icon: 'info', title: '关于 NotmyFault', value: appVersion, terms: '版本 环境 平台 技术栈' },
])
const filteredRootItems = computed(() => {
  const query = q.value.trim().toLowerCase()
  if (!query) return rootItems.value
  return rootItems.value.filter(item => `${item.title} ${item.value} ${item.terms}`.toLowerCase().includes(query))
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
    aiApiKeyDraft.value = ''
    aiKeyEditorOpen.value = false
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
    aiApiKeyDraft.value = ''
    aiKeyEditorOpen.value = false
    snackbar('API 密钥已删除')
  } catch (error) { alertDialog('删除失败', error.message) } finally { savingAIKey.value = false }
}

function openSettingsPage(page) {
  settingsTransition.value = 'settings-forward'
  currentPage.value = page
  q.value = ''
}

function goBack() {
  settingsTransition.value = 'settings-back'
  currentPage.value = currentPage.value.startsWith('ai-') ? 'ai' : 'root'
}

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

onMounted(() => {
  load()
  window.addEventListener('keydown', onKonamiKey)
})
onUnmounted(() => {
  aiApiKeyDraft.value = ''
  window.removeEventListener('keydown', onKonamiKey)
})
</script>

<template>
  <section class="page active settings-page">
    <div class="a16-canvas settings-shell">
      <Transition :name="settingsTransition" mode="out-in">
        <div :key="currentPage" class="settings-stage">
          <header class="settings-toolbar">
        <button v-if="currentPage !== 'root'" type="button" class="settings-back" aria-label="返回" @click="goBack">
          <span class="material-symbols-outlined">arrow_back</span>
        </button>
        <h2>{{ pageTitles[currentPage] }}</h2>
      </header>

      <template v-if="currentPage === 'root'">
        <label class="a16-search settings-search">
          <span class="material-symbols-outlined">search</span>
          <input v-model="q" type="search" aria-label="搜索设置" autocomplete="off" spellcheck="false" placeholder="搜索设置">
          <button v-if="q" type="button" class="a16-search-clear" aria-label="清空搜索" @click="q = ''"><span class="material-symbols-outlined">close</span></button>
        </label>
        <section v-if="filteredRootItems.length" class="a16-card settings-root-list">
          <button v-for="item in filteredRootItems" :key="item.page" type="button" class="a16-row" @click="openSettingsPage(item.page)">
            <span class="material-symbols-outlined a16-row-ico">{{ item.icon }}</span>
            <span class="a16-row-body"><span class="a16-row-title">{{ item.title }}</span></span>
            <span class="a16-row-val">{{ item.value }}</span>
            <span class="material-symbols-outlined settings-chevron">chevron_right</span>
          </button>
        </section>
        <p v-else class="a16-empty">没有找到设置</p>
      </template>

      <section v-else-if="currentPage === 'auth'" class="settings-subpage">
        <div class="a16-card">
          <div class="a16-row admin-auth-option selected">
            <span class="material-symbols-outlined a16-row-ico">touch_app</span>
            <span class="a16-row-body"><span class="a16-row-title">每次执行时确认</span><span class="a16-row-sub">每次管理员命令都需要单独确认</span></span>
            <span class="material-symbols-outlined a16-radio" aria-label="已启用">check_circle</span>
          </div>
          <div class="a16-row admin-rule-verification-settings">
            <span class="material-symbols-outlined a16-row-ico">key</span>
            <span class="a16-row-body"><span class="a16-row-title">管理员规则验证</span><span class="a16-row-sub">需要审批的规则始终验证签名私钥</span></span>
            <span class="material-symbols-outlined a16-radio" aria-label="已启用">check_circle</span>
          </div>
        </div>
      </section>

      <section v-else-if="currentPage === 'ai'" class="settings-subpage ai-drafting-settings">
        <div class="a16-card">
          <div class="a16-row">
            <span class="material-symbols-outlined a16-row-ico">auto_awesome</span>
            <span class="a16-row-body"><span class="a16-row-title">AI 规则草稿</span></span>
            <label class="switch"><input type="checkbox" :checked="store.aiDrafting.enabled" :disabled="savingAIFeature" @change="toggleAIDrafting"><span class="switch-track"><span class="switch-thumb"></span></span></label>
          </div>
          <button type="button" class="a16-row" @click="openSettingsPage('ai-service')">
            <span class="material-symbols-outlined a16-row-ico">dns</span>
            <span class="a16-row-body"><span class="a16-row-title">服务配置</span></span>
            <span class="a16-row-val">{{ aiServiceDirty ? '未保存' : aiProviderLabel }}</span>
            <span class="material-symbols-outlined settings-chevron">chevron_right</span>
          </button>
          <button type="button" class="a16-row" @click="openSettingsPage('ai-key')">
            <span class="material-symbols-outlined a16-row-ico">key</span>
            <span class="a16-row-body"><span class="a16-row-title">API 密钥</span></span>
            <span class="a16-row-val" :class="{ 'settings-state-success': store.aiApiKeyStatus === 'saved' }">{{ aiApiKeyStatusInfo.label }}</span>
            <span class="material-symbols-outlined settings-chevron">chevron_right</span>
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
          <div v-if="aiServiceDirty" class="settings-save-bar">
            <span>有未保存的更改</span>
            <button class="btn btn-filled" :disabled="savingAIService" @click="saveAIService">{{ savingAIService ? '保存中…' : '保存' }}</button>
          </div>
        </div>
      </section>

      <section v-else-if="currentPage === 'ai-key'" class="settings-subpage ai-drafting-settings">
        <div class="a16-card settings-key-card">
          <div class="settings-key-status" :class="aiApiKeyStatusInfo.cls">
            <span class="material-symbols-outlined">{{ aiApiKeyStatusInfo.icon }}</span>
            <strong>{{ aiApiKeyStatusInfo.label }}</strong>
          </div>
          <template v-if="aiKeyEditorOpen">
            <label class="a16-field settings-key-input">API 密钥<input v-model="aiApiKeyDraft" class="text-field" type="password" autocomplete="off"></label>
            <div class="settings-key-actions">
              <button class="btn btn-text" :disabled="savingAIKey" @click="cancelAIKeyEdit">取消</button>
              <button v-if="!aiApiKeyPersistenceUnsupported" class="btn btn-filled" :disabled="savingAIKey || !aiApiKeyDraft" @click="saveAIKey">{{ savingAIKey ? '保存中…' : '保存 API 密钥' }}</button>
            </div>
          </template>
          <div v-else class="settings-key-actions">
            <button class="btn btn-filled" :disabled="aiApiKeyPersistenceUnsupported" @click="editAIKey">{{ canDeleteSavedAIKey ? '更改 API 密钥' : '添加 API 密钥' }}</button>
            <button v-if="canDeleteSavedAIKey" class="btn btn-outlined" :disabled="savingAIKey" @click="deleteAIKey">删除 API 密钥</button>
          </div>
        </div>
      </section>

      <section v-else-if="currentPage === 'about'" class="settings-subpage">
        <div class="a16-card settings-about-card">
          <header class="a16-brand"><img class="a16-brand-logo" :src="appLogoUrl" alt=""><span class="a16-brand-body"><b>NotmyFault</b><small>{{ appVersion }}</small></span><span class="a16-badges"><span class="chip">Alpha</span><span class="chip">GPL-3.0</span></span></header>
          <div v-for="item in runtimeInfo" :key="item.key" class="a16-row"><span class="material-symbols-outlined a16-row-ico">{{ item.icon }}</span><span class="a16-row-body"><span class="a16-row-title">{{ item.key }}</span></span><span class="a16-row-val">{{ item.val }}</span></div>
          <p class="a16-sub-label">平台支持</p>
          <div v-for="platform in platforms" :key="platform.name" class="a16-row a16-row-sm"><span class="material-symbols-outlined a16-row-ico">{{ platform.icon }}</span><span class="a16-row-body"><span class="a16-row-title">{{ platform.name }}</span></span><span class="a16-row-val" :class="platform.cls">{{ platform.state }}</span></div>
          <p class="a16-sub-label">配置目录</p>
          <div class="settings-paths"><code>%APPDATA%\NotmyFault\</code><code>~/.config/notmyfault/</code></div>
          <p class="a16-sub-label">技术栈</p>
          <div class="a16-stack"><span v-for="item in techStack" :key="item">{{ item }}</span></div>
          <footer class="a16-foot"><span>&copy; 2026 NotmyFault Project</span></footer>
        </div>
      </section>
        </div>
      </Transition>
    </div>
    <OriginDialog :open="showOrigin" @close="showOrigin = false" />
  </section>
</template>
