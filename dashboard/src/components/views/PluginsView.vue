<script setup>
import { ref, computed, nextTick, onMounted } from 'vue'
import { store } from '../../lib/store'
import { apiDownload, apiRead, apiWrite, loadPlugins, getSchema } from '../../lib/api'
import { snackbar } from '../../lib/notify'
import { alertDialog, confirmDialog } from '../../lib/dialog'
import { useEngineControl } from '../../composables/useEngineControl'
import BaseDialog from '../BaseDialog.vue'

const { restartEngine } = useEngineControl()

function openSecurity() {
  window.__nmf?.switchPage?.('security')
}

const originTab = ref('builtin')
const typeFilter = ref('all')
const query = ref('')
const searchRef = ref(null)
const focusedPluginId = ref('')
const focusedPluginKind = ref('')
const focusReason = ref('')
const selectedPluginKey = ref('')
const permissionLabels = {
  notification: '发送通知',
  audio: '音频',
  clipboard: '剪贴板',
  network: '网络访问',
  external_binary: '外部程序',
  native_api: '原生 API',
  filesystem: '文件系统',
  process: '进程管理',
  registry: '注册表',
  screen_reader: '屏幕读取',
  input_monitor: '监听键盘和鼠标',
  admin: '管理员权限',
}
const allPlugins = computed(() => [
  ...Object.entries(store.pluginsData.triggers || {}).map(([id, meta]) => ({
    id, meta, kind: 'triggers', origin: meta?.origin === 'builtin' ? 'builtin' : 'user',
  })),
  ...Object.entries(store.pluginsData.actions || {}).map(([id, meta]) => ({
    id, meta, kind: 'actions', origin: meta?.origin === 'builtin' ? 'builtin' : 'user',
  })),
].sort((left, right) => String(left.meta?.name || left.id).localeCompare(
  String(right.meta?.name || right.id), 'zh-CN',
)))
const originCounts = computed(() => ({
  builtin: allPlugins.value.filter(plugin => plugin.origin === 'builtin').length,
  user: allPlugins.value.filter(plugin => plugin.origin === 'user').length,
}))
const filteredPlugins = computed(() => {
  const q = query.value.trim().toLowerCase()
  return allPlugins.value.filter(plugin => (
    plugin.origin === originTab.value
    && (typeFilter.value === 'all' || plugin.kind === typeFilter.value)
    && (!q || [plugin.id, plugin.meta?.name, plugin.meta?.description, plugin.meta?.package_name]
      .some(value => String(value || '').toLowerCase().includes(q)))
  )).sort((left, right) => (
    Number(pluginHasProblem(right.meta)) - Number(pluginHasProblem(left.meta))
    || String(left.meta?.name || left.id).localeCompare(String(right.meta?.name || right.id), 'zh-CN')
  ))
})
const visibleProblemCount = computed(() => filteredPlugins.value.filter(plugin => pluginHasProblem(plugin.meta)).length)
const focusedPlugin = computed(() => allPlugins.value.find(plugin => (
  plugin.id === focusedPluginId.value
  && (!focusedPluginKind.value || plugin.kind === focusedPluginKind.value)
)))
const focusedPluginMissing = computed(() => focusedPluginId.value && !focusedPlugin.value)
const selectedPlugin = computed(() => allPlugins.value.find(
  plugin => `${plugin.kind}:${plugin.id}` === selectedPluginKey.value,
))
const showInstall = ref(false)
const showKey = ref(false)
const signingPassword = ref('')
const forceInstall = ref(false)
const buildHookConfirmed = ref(false)
const fileInput = ref(null)
let keyResolve = null

const preview = ref(null)
const previewLoading = ref(false)
const previewError = ref('')
const fileForUpload = ref(null)
const installError = ref('')
// 页面暂时没有修改 showRegistry 的入口，索引面板和下载代码留给后续继续用。
const showRegistry = ref(false)
let savedRegistryUrl = ''
try { savedRegistryUrl = localStorage.getItem('nmf-plugin-registry-url') || '' } catch {}
const registryUrl = ref(savedRegistryUrl)
const registryPlugins = ref([])
const registryLoading = ref(false)
const registryError = ref('')
const registryDownloading = ref('')
const installedPackages = computed(() => new Set(
  Object.values(store.pluginsData.triggers || {}).concat(Object.values(store.pluginsData.actions || {}))
    .map(meta => meta?.package_name)
    .filter(Boolean),
))

async function loadRegistry() {
  const url = registryUrl.value.trim()
  if (!url) {
    registryError.value = '请输入插件索引地址'
    return
  }
  registryLoading.value = true
  registryError.value = ''
  try {
    const response = await apiWrite('/api/plugins/registry', 'POST', { url })
    const data = await response.json()
    if (!data.ok) {
      registryError.value = data.error || '读取插件索引失败'
      return
    }
    try { localStorage.setItem('nmf-plugin-registry-url', url) } catch {}
    registryPlugins.value = data.plugins || []
  } catch (error) {
    registryError.value = error.message || '读取插件索引失败'
  } finally {
    registryLoading.value = false
  }
}

async function previewRegistryPlugin(entry) {
  const identity = `${entry.package_name}@${entry.version}`
  registryDownloading.value = identity
  registryError.value = ''
  try {
    const response = await apiDownload('/api/plugins/registry/download', {
      url: registryUrl.value.trim(),
      package_name: entry.package_name,
      version: entry.version,
    })
    if (!response.ok) {
      const data = await response.json().catch(() => ({}))
      registryError.value = data.error || '下载插件包失败'
      return
    }
    const archive = await response.blob()
    const file = new File(
      [archive],
      `${entry.package_name}-${entry.version}.nmfp`,
      { type: 'application/octet-stream' },
    )
    openInstall()
    fileForUpload.value = file
    await uploadPreview(file)
  } catch (error) {
    registryError.value = error.message || '下载插件包失败'
  } finally {
    registryDownloading.value = ''
  }
}

async function refresh() {
  // 引擎离线时接口会失败，插件页保留上次数据。
  const [plugins, sch] = await Promise.all([
    loadPlugins().catch(() => store.pluginsData),
    getSchema().catch(() => store.schema),
  ])
  store.pluginsData = plugins
  store.schema = sch
}

// 插件变更要重启引擎才生效，直接问用户要不要现在重启。
async function promptRestart(message) {
  if (!await confirmDialog('重启引擎使更改生效？', message, '立即重启')) return
  await restartEngine()
}

function pluginKindLabel(kind) {
  return kind === 'triggers' ? '触发器' : '动作'
}

function pluginKindIcon(kind) {
  return kind === 'triggers' ? 'bolt' : 'play_arrow'
}

function pluginOriginLabel(origin) {
  return origin === 'builtin' ? '内置' : '用户安装'
}

function pluginVersion(meta) {
  return meta?.version || String(meta?.version_code || '—')
}

function pluginIsAvailable(meta) {
  return meta?.platform_compatible !== false && meta?.availability !== 'unavailable'
}

function pluginStatus(meta) {
  if (meta?._error) return '加载失败'
  if (!pluginIsAvailable(meta)) return '当前不可用'
  if (meta?.availability === 'partial') return '部分可用'
  if (meta?.enabled === false) return '已停用'
  return ''
}

function pluginHasProblem(meta) {
  return Boolean(pluginStatus(meta))
}

function pluginStatusDetail(meta) {
  if (meta?._error) return String(meta._error)
  const reasons = Array.isArray(meta?.unavailable_reasons) ? meta.unavailable_reasons : []
  return reasons.join('；')
}

const selectedProblemDetail = computed(() => {
  if (!selectedPlugin.value) return ''
  const isFocused = selectedPlugin.value.id === focusedPluginId.value
    && selectedPlugin.value.kind === focusedPluginKind.value
  return pluginStatusDetail(selectedPlugin.value.meta) || (isFocused ? focusReason.value : '')
})

function permissionLabel(permission) {
  return permissionLabels[permission] || permission
}

async function openPlugin(plugin) {
  selectedPluginKey.value = `${plugin.kind}:${plugin.id}`
  if (window.matchMedia?.('(max-width: 1100px)')?.matches) {
    await nextTick()
    document.querySelector('.plugin-back-button')?.focus()
  }
}

async function closePlugin() {
  const previousKey = selectedPluginKey.value
  selectedPluginKey.value = ''
  await nextTick()
  const previousButton = [...document.querySelectorAll('.plugin-open-button')].find(button => button.dataset.pluginKey === previousKey)
  ;(previousButton || searchRef.value)?.focus()
}

async function togglePlugin(plugin) {
  try {
    const r = await apiWrite('/api/plugins/toggle', 'POST', { type: plugin.kind, id: plugin.id })
    const d = await r.json()
    if (d.ok) {
      await refresh()
      if (d.restart_required) await promptRestart('刚才的插件状态变更会在引擎重启后生效。')
      else snackbar('状态已更新')
    }
    else alertDialog('操作失败', d.error || '未知错误')
  } catch (e) { alertDialog('请求失败', e.message) }
}

async function uninstallPlugin(plugin) {
  if (!await confirmDialog('卸载插件 "' + plugin.id + '"？', '此操作不可撤销。', '卸载')) return
  try {
    const r = await apiWrite('/api/plugins/' + plugin.kind + '/' + plugin.id, 'DELETE')
    const d = await r.json()
    if (d.ok) {
      selectedPluginKey.value = ''
      await refresh()
      await promptRestart('刚卸载的插件会在引擎重启后完全移除。')
    }
    else alertDialog('卸载失败', d.error || '未知错误')
  } catch (e) { alertDialog('请求失败', e.message) }
}

function openInstall() {
  showInstall.value = true
  preview.value = null
  previewError.value = ''
  fileForUpload.value = null
  forceInstall.value = false
  buildHookConfirmed.value = false
  installError.value = ''
  signingPassword.value = ''
}

function cancelKey() {
  showKey.value = false
  if (keyResolve) { keyResolve(false); keyResolve = null }
}

function submitKey() {
  if (!signingPassword.value.trim()) return
  showKey.value = false
  if (keyResolve) { keyResolve(true); keyResolve = null }
}

function riskLabel(level) {
  const m = { none: '无风险', low: '低风险', medium: '中风险', high: '高风险', unknown: '未知' }
  return m[level] || level
}

function riskClass(level) {
  const m = { none: 'risk-none', low: 'risk-low', medium: 'risk-med', high: 'risk-high', unknown: 'risk-unknown' }
  return 'risk-badge ' + (m[level] || '')
}

const updateKindLabel = {
  new: '全新安装',
  upgrade: `升级`,
  downgrade: '降级',
  reinstall: '同版本重装',
}
function updateKindChipClass(kind) {
  return kind === 'downgrade' ? 'chip chip-error'
    : kind === 'upgrade' ? 'chip chip-clean'
      : 'chip'
}
function diffRows(diff) {
  if (!diff) return []
  return [
    ...diff.added.map(item => ({ item, sign: '+', cls: 'diff-add' })),
    ...diff.removed.map(item => ({ item, sign: '-', cls: 'diff-remove' })),
  ]
}
const hasBuildHookRisk = computed(() =>
  (preview.value?.risks || []).some(risk => risk.id === 'build_hook'))

async function uploadPreview(file) {
  previewLoading.value = true
  previewError.value = ''
  preview.value = null
  try {
    const fd = new FormData()
    fd.append('file', file)
    const r = await apiWrite('/api/plugins/preview', 'POST', fd, true)
    const d = await r.json()
    if (!d.ok) { previewError.value = d.error || '预览失败'; return }
    preview.value = d
  } catch (e) { previewError.value = '预览请求失败: ' + e.message }
  finally { previewLoading.value = false }
}

async function doInstall() {
  if (!preview.value) return
  const p = preview.value

  if (!signingPassword.value) {
    try {
      const r = await apiRead('/api/plugins/key-status')
      if (r.ok) {
        const ks = await r.json()
        if (ks.encrypted) {
          signingPassword.value = ''
          showKey.value = true
          const ok = await new Promise(res => { keyResolve = res })
          if (!ok) return
        }
      }
    } catch (e) { /* 引擎离线时无法读取密钥状态，继续安装请求。 */ }
  }

  const fd = new FormData()
  fd.append('preview_token', p.preview_token)
  if (hasBuildHookRisk.value && buildHookConfirmed.value) {
    fd.append('confirmed_risk_ids', JSON.stringify(['build_hook']))
  }
  if (signingPassword.value) fd.append('signing_password', signingPassword.value)
  if (forceInstall.value) fd.append('force', 'true')
  try {
    const r = await apiWrite('/api/plugins/install', 'POST', fd, true)
    const d = await r.json()
    if (d.ok) {
      showInstall.value = false
      preview.value = null
      forceInstall.value = false
      await refresh()
      await promptRestart('插件 "' + d.id + '" 已安装，引擎重启后即可使用。')
    } else { installError.value = d.error || '安装失败，请重试' }
  } catch (e) { alertDialog('请求失败', e.message) }
}

function onFilePicked(e) {
  const file = e.target.files && e.target.files[0]
  if (!file) return
  fileForUpload.value = file
  uploadPreview(file)
}

function retryPreview() {
  if (fileForUpload.value) uploadPreview(fileForUpload.value)
}

async function consumePluginFocus() {
  const focus = store.pendingPluginFocus
  if (!focus) return
  store.pendingPluginFocus = null
  focusedPluginKind.value = focus.kind === 'trigger' ? 'triggers' : 'actions'
  focusedPluginId.value = focus.id || ''
  const target = allPlugins.value.find(plugin => (
    plugin.id === focusedPluginId.value && plugin.kind === focusedPluginKind.value
  ))
  originTab.value = target?.origin || 'user'
  typeFilter.value = focusedPluginKind.value
  focusReason.value = focus.reason || ''
  query.value = focus.id || ''
  selectedPluginKey.value = target ? `${target.kind}:${target.id}` : ''
  await nextTick()
  searchRef.value?.focus()
  document.querySelector('.plugin-list-row.focused')?.scrollIntoView?.({ block: 'center' })
}

onMounted(async () => {
  await refresh()
  await consumePluginFocus()
})
</script>

<template>
  <section class="page active plugin-management-page">
    <div class="page-head"><div><h2>插件管理</h2><p class="page-subtitle">异常插件优先显示；选择插件查看状态、权限和来源。</p></div><div class="actions">
      <button class="btn btn-filled" @click="openInstall"><span class="material-symbols-outlined">install_desktop</span>安装插件</button>
    </div></div>
    <div class="plugin-workbench" :class="{ 'has-selection': selectedPlugin }">
      <div class="plugin-list-view">
        <section v-if="showRegistry" class="plugin-registry-panel">
          <div class="plugin-registry-head">
            <div>
              <h3>只读插件索引</h3>
              <p>填写 registry.json 的 HTTPS 地址。下载后仍会进入本地安全预览，不会直接安装。</p>
            </div>
            <div class="plugin-registry-load">
              <input v-model="registryUrl" class="text-field" type="url" spellcheck="false" placeholder="https://raw.githubusercontent.com/.../registry.json" @keyup.enter="loadRegistry">
              <button class="btn btn-filled" :disabled="registryLoading" @click="loadRegistry">{{ registryLoading ? '读取中…' : '读取索引' }}</button>
            </div>
          </div>
          <p v-if="registryError" class="plugin-registry-error"><span class="material-symbols-outlined">warning</span>{{ registryError }}</p>
          <div v-if="registryPlugins.length" class="plugin-registry-grid">
            <article v-for="entry in registryPlugins" :key="entry.package_name + '@' + entry.version" class="plugin-registry-card">
              <div>
                <h4>{{ entry.name }}</h4>
                <p>{{ entry.package_name }}</p>
              </div>
              <div class="preview-meta">
                <span class="chip">v{{ entry.version }}</span>
                <span v-for="platform in entry.supported_platforms" :key="platform" class="chip">{{ platform }}</span>
                <span class="chip" :class="installedPackages.has(entry.package_name) ? 'chip-clean' : ''">{{ installedPackages.has(entry.package_name) ? '已安装' : '未安装' }}</span>
              </div>
              <div class="plugin-registry-actions">
                <a class="btn btn-text" :href="entry.homepage" target="_blank" rel="noreferrer">主页</a>
                <button class="btn btn-filled btn-sm" :disabled="registryDownloading === entry.package_name + '@' + entry.version" @click="previewRegistryPlugin(entry)">
                  {{ registryDownloading === entry.package_name + '@' + entry.version ? '下载中…' : installedPackages.has(entry.package_name) ? '检查更新' : '下载并检查' }}
                </button>
              </div>
            </article>
          </div>
          <p v-else-if="!registryLoading && !registryError" class="plugin-registry-empty">索引尚未读取。</p>
        </section>

        <div class="plugin-origin-switch" :class="{ 'user-active': originTab === 'user' }" role="tablist" aria-label="插件来源">
          <button type="button" role="tab" :aria-selected="originTab === 'builtin'"
            :class="{ active: originTab === 'builtin' }" @click="originTab = 'builtin'">
            内置<span>{{ originCounts.builtin }}</span>
          </button>
          <button type="button" role="tab" :aria-selected="originTab === 'user'"
            :class="{ active: originTab === 'user' }" @click="originTab = 'user'">
            用户<span>{{ originCounts.user }}</span>
          </button>
        </div>

        <div v-if="visibleProblemCount" class="plugin-problem-summary">
          <span class="material-symbols-outlined">report</span>
          <span>当前列表有 {{ visibleProblemCount }} 个插件需要处理，已排在前面。</span>
        </div>

        <div class="plugin-tools">
          <label class="plugin-search">
            <span class="material-symbols-outlined">search</span>
            <input ref="searchRef" v-model="query" type="search" aria-label="搜索插件" autocomplete="off" spellcheck="false" placeholder="搜索名称、说明、插件 ID 或包名">
          </label>
          <select v-model="typeFilter" class="select plugin-type-filter" aria-label="筛选插件类型">
            <option value="all">全部类型</option>
            <option value="triggers">触发器</option>
            <option value="actions">动作</option>
          </select>
          <p v-if="focusReason" class="plugin-focus-reason"><span class="material-symbols-outlined">info</span>{{ focusReason }}</p>
        </div>
        <div v-if="focusedPluginMissing" class="plugin-missing-callout">
          <span class="material-symbols-outlined">extension_off</span>
          <div><b>未找到插件 {{ focusedPluginId }}</b><p>请安装对应的 .nmfp 插件包；安装完成后可以继续使用刚才的模板。</p></div>
          <button class="btn btn-filled" @click="openInstall"><span class="material-symbols-outlined">install_desktop</span>安装插件</button>
        </div>

        <Transition :name="originTab === 'user' ? 'plugin-list-next' : 'plugin-list-prev'" mode="out-in">
          <div :key="originTab" class="plugin-list-stage" tabindex="0" role="region" aria-label="插件列表">
            <div v-if="!filteredPlugins.length" class="empty-state plugin-list-empty">
              <div class="material-symbols-outlined">extension_off</div>
              <h3>{{ query.trim() || typeFilter !== 'all' ? '没有匹配的插件' : originTab === 'user' ? '还没有用户插件' : '暂无内置插件' }}</h3>
              <p>{{ query.trim() || typeFilter !== 'all' ? '可以清除搜索或调整类型筛选。' : originTab === 'user' ? '安装 .nmfp 插件后会显示在这里。' : '启动引擎后刷新插件列表。' }}</p>
              <button v-if="query || typeFilter !== 'all'" class="btn btn-text" @click="query = ''; typeFilter = 'all'">清空筛选</button>
            </div>
            <div v-else class="plugin-list-shell">
              <article v-for="plugin in filteredPlugins" :key="plugin.kind + ':' + plugin.id"
                class="plugin-list-row" :class="{
                  focused: focusedPluginId === plugin.id && focusedPluginKind === plugin.kind,
                  selected: selectedPluginKey === `${plugin.kind}:${plugin.id}`,
                  disabled: plugin.meta.enabled === false || !pluginIsAvailable(plugin.meta),
                }" @click="openPlugin(plugin)">
                <span class="material-symbols-outlined plugin-list-icon" :class="plugin.kind === 'triggers' ? 'trigger' : 'action'">
                  {{ pluginKindIcon(plugin.kind) }}
                </span>
                <button class="plugin-list-copy plugin-open-button" :data-plugin-key="`${plugin.kind}:${plugin.id}`" :aria-label="`查看${plugin.meta.name || plugin.id}详情`" @click.stop="openPlugin(plugin)">
                  <b>{{ plugin.meta.name || plugin.id }}</b>
                  <small>{{ pluginKindLabel(plugin.kind) }} · v{{ pluginVersion(plugin.meta) }}</small>
                </button>
                <span v-if="pluginStatus(plugin.meta)" class="plugin-list-status" :class="{
                  error: plugin.meta._error || !pluginIsAvailable(plugin.meta),
                  muted: plugin.meta.enabled === false,
                }">{{ pluginStatus(plugin.meta) }}</span>
                <label class="switch plugin-list-switch" :class="{ 'is-disabled': !pluginIsAvailable(plugin.meta) }" @click.stop>
                  <input type="checkbox" :checked="plugin.meta.enabled !== false" :disabled="!pluginIsAvailable(plugin.meta)" :aria-label="`启用${plugin.meta.name || plugin.id}`" @change="togglePlugin(plugin)">
                  <span class="switch-track"><span class="switch-thumb"></span></span>
                </label>
                <span class="material-symbols-outlined plugin-list-go">chevron_right</span>
              </article>
            </div>
          </div>
        </Transition>
      </div>

      <Transition name="plugin-detail" mode="out-in">
      <div v-if="selectedPlugin" :key="selectedPluginKey" class="plugin-detail-view" tabindex="0" role="region" aria-label="插件详情">
        <button type="button" class="plugin-back-button" @click="closePlugin">
          <span class="material-symbols-outlined">arrow_back</span>插件管理
        </button>

        <section class="plugin-detail-hero">
          <span class="material-symbols-outlined plugin-detail-icon" :class="selectedPlugin.kind === 'triggers' ? 'trigger' : 'action'">
            {{ pluginKindIcon(selectedPlugin.kind) }}
          </span>
          <div class="plugin-detail-identity">
            <h2>{{ selectedPlugin.meta.name || selectedPlugin.id }}</h2>
            <p>{{ pluginKindLabel(selectedPlugin.kind) }} · v{{ pluginVersion(selectedPlugin.meta) }}</p>
            <span>{{ selectedPlugin.meta.description || '没有插件说明' }}</span>
          </div>
          <button v-if="selectedPlugin.origin === 'user'" type="button" class="btn btn-outlined plugin-uninstall-button" @click="uninstallPlugin(selectedPlugin)">
            <span class="material-symbols-outlined">delete</span>卸载插件
          </button>
        </section>

        <div v-if="selectedProblemDetail" class="plugin-detail-alert">
          <span class="material-symbols-outlined">error</span>
          <div v-if="selectedPlugin.origin === 'builtin' && /签名|完整性/.test(selectedProblemDetail)"><b>安装文件异常</b><p>内置插件属于 NotmyFault 安装内容，请重新安装完整应用。</p><button class="btn btn-outlined" @click="openSecurity">查看重新安装说明</button></div>
          <div v-else><b>{{ pluginStatus(selectedPlugin.meta) || '需要处理' }}</b><p>{{ selectedProblemDetail }}</p></div>
        </div>

        <section class="plugin-detail-section">
          <h3>插件状态</h3>
          <div class="plugin-detail-group">
            <label class="plugin-detail-row plugin-detail-toggle">
              <span><b>启用插件</b><small>控制这个插件是否参与自动化</small></span>
              <span class="switch" :class="{ 'is-disabled': !pluginIsAvailable(selectedPlugin.meta) }">
                <input type="checkbox" :checked="selectedPlugin.meta.enabled !== false" :disabled="!pluginIsAvailable(selectedPlugin.meta)" @change="togglePlugin(selectedPlugin)">
                <span class="switch-track"><span class="switch-thumb"></span></span>
              </span>
            </label>
            <div class="plugin-detail-row">
              <span>当前状态</span>
              <strong :class="{ error: selectedPlugin.meta._error || !pluginIsAvailable(selectedPlugin.meta) }">{{ pluginStatus(selectedPlugin.meta) || '已启用' }}</strong>
            </div>
          </div>
        </section>

        <section class="plugin-detail-section">
          <h3>权限</h3>
          <div class="plugin-detail-group">
            <div v-for="permission in selectedPlugin.meta.permissions || []" :key="permission" class="plugin-detail-row">
              <span>{{ permissionLabel(permission) }}</span>
            </div>
            <div v-if="!(selectedPlugin.meta.permissions || []).length" class="plugin-detail-row">
              <span>无特殊权限</span>
            </div>
          </div>
        </section>

        <section class="plugin-detail-section">
          <h3>插件信息</h3>
          <div class="plugin-detail-group">
            <div class="plugin-detail-row"><span>插件 ID</span><strong>{{ selectedPlugin.id }}</strong></div>
            <div class="plugin-detail-row"><span>包名</span><strong>{{ selectedPlugin.meta.package_name || '—' }}</strong></div>
            <div class="plugin-detail-row"><span>版本</span><strong>{{ pluginVersion(selectedPlugin.meta) }}</strong></div>
            <div class="plugin-detail-row"><span>类型</span><strong>{{ pluginKindLabel(selectedPlugin.kind) }}</strong></div>
            <div class="plugin-detail-row"><span>来源</span><strong>{{ pluginOriginLabel(selectedPlugin.origin) }}</strong></div>
          </div>
        </section>
      </div>
      <div v-else class="plugin-detail-empty">
        <span class="material-symbols-outlined">extension</span>
        <h3>选择一个插件</h3>
        <p>这里会显示插件状态、声明权限、版本和来源。</p>
      </div>
      </Transition>
    </div>

    <BaseDialog :open="showInstall" @close="showInstall = false">
      <div v-if="showInstall" class="preview-dialog">
        <Transition name="dialog-stage" mode="out-in">
        <div v-if="!preview && !previewLoading && !previewError" key="choose" class="preview-step">
          <span class="material-symbols-outlined dialog-ico">install_desktop</span>
          <h3 class="dialog-title">安装插件</h3>
          <p class="dialog-sub">选择 .nmfp 插件包</p>
          <div class="dropzone" @click="fileInput && fileInput.click()">
            <span class="material-symbols-outlined">cloud_upload</span>点击选择 .nmfp 文件
          </div>
          <input ref="fileInput" type="file" accept=".nmfp" class="hidden" @change="onFilePicked">
          <button class="btn btn-text" @click="showInstall = false">取消</button>
        </div>

        <div v-else-if="previewLoading" key="loading" class="preview-loading">
          <div class="spinner" style="margin:24px auto"></div>
          <p>正在解析插件...</p>
        </div>

        <div v-else-if="previewError" key="error" class="preview-error">
          <span class="material-symbols-outlined dialog-ico" style="color:var(--md-error)">error</span>
          <h3 class="dialog-title">解析失败</h3>
          <p class="dialog-sub">{{ previewError }}</p>
          <div class="dialog-actions" style="justify-content:center">
            <button class="btn btn-filled" @click="retryPreview">重试</button>
            <button class="btn btn-text" @click="showInstall = false">关闭</button>
          </div>
        </div>

        <div v-else-if="preview" key="preview" class="preview-result">
          <div class="preview-scroll">
            <div class="preview-hero">
              <span class="material-symbols-outlined preview-hero-ico">extension</span>
              <div>
                <h3>{{ preview.plugin.name }}</h3>
                <div class="preview-meta">
                  <span class="chip"
                    :class="preview.plugin.type === 'triggers' ? 'chip-origin-builtin' : 'chip-origin-user'">
                    {{ preview.plugin.type === 'triggers' ? '触发器' : '动作' }}
                  </span>
                  <span class="chip">v{{ preview.plugin.version }}</span>
                  <span v-if="preview.update_diff?.update && preview.update_diff.update.kind !== 'new'"
                    class="chip" :class="updateKindChipClass(preview.update_diff.update.kind)">
                    {{ updateKindLabel[preview.update_diff.update.kind] }}
                    v{{ preview.update_diff.update.installed_version_code }} →
                    v{{ preview.update_diff.update.incoming_version_code }}
                  </span>
                  <span v-if="preview.plugin.platform_compatible === false" class="chip chip-error">
                    当前系统不兼容
                  </span>
                  <span v-if="preview.plugin.author" class="chip">{{ preview.plugin.author }}</span>
                </div>

                <div v-if="preview.update_diff?.signature_identity_changed" class="preview-section warn">
                  <div class="preview-section-title">
                    <span class="material-symbols-outlined">gpp_maybe</span>签名身份变了
                  </div>
                  <div class="preview-section-body">
                    <p class="risk-item">这个包的签名者和已安装版本不同（{{ preview.update_diff.signature_old }} → {{ preview.update_diff.signature_new }}），确认来源可信再装</p>
                  </div>
                </div>
                <p class="preview-desc" v-if="preview.plugin.description">{{ preview.plugin.description }}</p>
                <p class="preview-pkg">{{ preview.plugin.package_name }}</p>
              </div>
            </div>

            <div v-if="!preview.schema_valid" class="preview-section warn">
              <div class="preview-section-title">
                <span class="material-symbols-outlined">warning</span>Schema 校验
              </div>
              <div class="preview-section-body">
                <p class="risk-item" v-for="err in preview.schema_errors" :key="err">{{ err }}</p>
              </div>
            </div>

            <div class="preview-section">
              <div class="preview-section-title">
                <span class="material-symbols-outlined">security</span>权限申请
                <span class="perm-count" v-if="preview.permissions.length">({{ preview.permissions.length }})</span>
                <span v-if="!preview.permissions.length" class="chip chip-clean" style="margin-left:auto">无特殊权限</span>
              </div>
              <div v-if="preview.permissions.length" class="perm-list">
                <div v-for="perm in preview.permissions" :key="perm.permission"
                  class="perm-list-item" :class="{ 'perm-unknown': !perm.known }">
                  <div class="perm-left">
                    <span class="perm-name">{{ perm.label }}</span>
                    <span class="perm-perm">{{ perm.permission }}</span>
                    <p class="perm-desc">{{ perm.description }}</p>
                  </div>
                  <div class="perm-right">
                    <span :class="riskClass(perm.risk)">{{ riskLabel(perm.risk) }}</span>
                  </div>
                </div>
              </div>
              <div v-if="!preview.permission_conform" class="perm-nonconform">
                <span class="material-symbols-outlined">error</span>
                包含不符合安全规范的权限，严格模式下将拒绝安装和加载
              </div>
            </div>

            <div v-if="preview.risks.length" class="preview-section">
              <div class="preview-section-title">
                <span class="material-symbols-outlined">bug_report</span>安全扫描
                <span class="perm-count">({{ preview.risks.length }})</span>
              </div>
              <div class="risk-list">
                <div v-for="(risk, i) in preview.risks" :key="i" class="risk-list-item">
                  <span :class="riskClass(risk.level)">{{ riskLabel(risk.level) }}</span>
                  <span class="risk-label">{{ risk.label }}</span>
                  <span class="risk-detail">{{ risk.detail }}</span>
                </div>
              </div>
            </div>

            <div v-if="diffRows(preview.update_diff?.permission_diff).length
              || diffRows(preview.update_diff?.capability_diff).length" class="preview-section">
              <div class="preview-section-title">
                <span class="material-symbols-outlined">difference</span>和已安装版本的差别
              </div>
              <div class="preview-section-body">
                <template v-if="diffRows(preview.update_diff.permission_diff).length">
                  <p class="diff-caption">权限</p>
                  <p v-for="row in diffRows(preview.update_diff.permission_diff)" :key="'p' + row.item"
                    class="diff-row" :class="row.cls"><span>{{ row.sign }}</span>{{ row.item }}</p>
                </template>
                <template v-if="diffRows(preview.update_diff.capability_diff).length">
                  <p class="diff-caption">系统能力</p>
                  <p v-for="row in diffRows(preview.update_diff.capability_diff)" :key="'c' + row.item"
                    class="diff-row" :class="row.cls"><span>{{ row.sign }}</span>{{ row.item }}</p>
                </template>
              </div>
            </div>

            <div v-if="!preview.risks.length && preview.permission_conform" class="preview-section">
              <div class="preview-section-title">
                <span class="material-symbols-outlined">check_circle</span>安全检查
              </div>
              <p class="safe-notice">未发现安全风险，权限符合规范</p>
            </div>
          </div>

          <div v-if="hasBuildHookRisk" class="preview-install-error">
            <span class="material-symbols-outlined">dangerous</span>
            这个插件声明了构建钩子，点安装会以你的身份执行它自带的命令。只给信得过的来源装
          </div>

          <div v-if="installError" class="preview-install-error">
            <span class="material-symbols-outlined">warning</span>{{ installError }}
          </div>

          <div class="preview-foot">
            <label class="check-row" v-if="preview.plugin.package_name">
              <input type="checkbox" v-model="forceInstall">强制覆盖已安装的同包名插件
            </label>
            <label class="check-row" v-if="hasBuildHookRisk">
              <input type="checkbox" v-model="buildHookConfirmed">我确认执行这个插件的构建命令
            </label>
            <div class="preview-foot-actions">
              <button class="btn btn-text" @click="showInstall = false">取消</button>
              <button class="btn btn-filled" @click="doInstall"
                :disabled="(!preview.permission_conform && store.engineStatus?.security_mode === 'strict')
                  || (hasBuildHookRisk && !buildHookConfirmed)">
                <span class="material-symbols-outlined">download</span>安装
              </button>
            </div>
          </div>
        </div>
        </Transition>
      </div>
    </BaseDialog>

    <BaseDialog :open="showKey" @close="cancelKey">
      <div v-if="showKey" class="install-dialog">
        <span class="material-symbols-outlined dialog-ico">key</span>
        <h3 class="dialog-title">NotmyFault 安装密钥</h3>
        <p class="dialog-sub">安装 NotmyFault 时输入的密钥</p>
        <input type="password" v-model="signingPassword" class="text-field" placeholder="输入私钥密码" style="width:100%;text-align:center">
        <div class="dialog-actions">
          <button class="btn btn-text" @click="cancelKey">取消</button>
          <button class="btn btn-filled" @click="submitKey">确认</button>
        </div>
      </div>
    </BaseDialog>
  </section>
</template>
