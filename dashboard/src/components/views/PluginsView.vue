<script setup>
import { ref, computed, onMounted } from 'vue'
import { store } from '../../lib/store'
import { apiRead, apiWrite, loadPlugins, getSchema } from '../../lib/api'
import { snackbar } from '../../lib/notify'
import PluginCard from '../PluginCard.vue'

const tab = ref('triggers')
const list = computed(() => store.pluginsData[tab.value] || {})
const showInstall = ref(false)
const showKey = ref(false)
const keyPw = ref('')
const forceInstall = ref(false)
const fileInput = ref(null)
let keyResolve = null

// 预览状态
const preview = ref(null)
const previewLoading = ref(false)
const previewError = ref('')
const fileForUpload = ref(null)
const installError = ref('')

async function refresh() {
  const [plugins, sch] = await Promise.all([loadPlugins(), getSchema()])
  store.pluginsData = plugins
  store.schema = sch
}

async function togglePlugin(pid) {
  try {
    const r = await apiWrite('/api/plugins/toggle', 'POST', { type: tab.value, id: pid })
    const d = await r.json()
    if (d.ok) { snackbar(d.restart_required ? '状态已更新，需重启引擎生效' : '状态已更新'); await refresh() }
    else alert('操作失败: ' + (d.error || '未知错误'))
  } catch (e) { alert('请求失败: ' + e.message) }
}

async function uninstallPlugin(pid) {
  if (!confirm('确定要卸载插件 "' + pid + '" 吗？此操作不可撤销。')) return
  try {
    const r = await apiWrite('/api/plugins/' + tab.value + '/' + pid, 'DELETE')
    const d = await r.json()
    if (d.ok) { snackbar('已卸载，需重启引擎生效'); await refresh() }
    else alert('卸载失败: ' + (d.error || '未知错误'))
  } catch (e) { alert('请求失败: ' + e.message) }
}

function openInstall() {
  showInstall.value = true
  preview.value = null
  previewError.value = ''
  fileForUpload.value = null
  forceInstall.value = false
  installError.value = ''
}

function cancelKey() {
  showKey.value = false
  if (keyResolve) { keyResolve(false); keyResolve = null }
}

function submitKey() {
  if (!keyPw.value.trim()) return
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

async function uploadPreview(file) {
  previewLoading.value = true
  previewError.value = ''
  preview.value = null
  try {
    const fd = new FormData()
    fd.append('file', file)
    if (keyPw.value) fd.append('password', keyPw.value)
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

  if (!keyPw.value) {
    try {
      const r = await apiRead('/api/plugins/key-status')
      if (r.ok) {
        const ks = await r.json()
        if (ks.encrypted) {
          keyPw.value = ''
          showKey.value = true
          const ok = await new Promise(res => { keyResolve = res })
          if (!ok) return
        }
      }
    } catch (e) { /* 引擎离线，跳过 */ }
  }

  const fd = new FormData()
  fd.append('preview_token', p.preview_token)
  if (keyPw.value) fd.append('password', keyPw.value)
  if (forceInstall.value) fd.append('force', 'true')
  try {
    const r = await apiWrite('/api/plugins/install', 'POST', fd, true)
    const d = await r.json()
    if (d.ok) {
      snackbar('插件 "' + d.id + '" 安装成功，需重启引擎生效')
      showInstall.value = false
      preview.value = null
      forceInstall.value = false
      await refresh()
    } else { installError.value = d.error || '安装失败，请重试' }
  } catch (e) { alert('请求失败: ' + e.message) }
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

onMounted(refresh)
</script>

<template>
  <section class="page active">
    <div class="page-head"><h2>插件管理</h2><div class="actions">
      <button class="btn btn-filled" @click="openInstall"><span class="material-symbols-outlined">install_desktop</span>安装插件</button>
    </div></div>
    <div class="tabs">
      <button class="tab" :class="{ active: tab === 'triggers' }" @click="tab = 'triggers'">触发器</button>
      <button class="tab" :class="{ active: tab === 'actions' }" @click="tab = 'actions'">动作</button>
    </div>
    <div v-if="!Object.keys(list).length" class="empty-state">
      <div class="material-symbols-outlined">extension_off</div><h3>暂无插件</h3><p>安装插件或启动引擎后刷新</p>
    </div>
    <div v-else class="plugin-grid">
      <PluginCard v-for="(meta, pid) in list" :key="pid" :pid="pid" :meta="meta" :type="tab"
        @toggle="togglePlugin" @uninstall="uninstallPlugin" />
    </div>

    <!-- 安装对话框 -->
    <div v-if="showInstall" class="modal-overlay" @click.self="showInstall = false">
      <div class="preview-dialog">
        <!-- 步骤 1：选择文件 -->
        <div v-if="!preview && !previewLoading && !previewError" class="preview-step">
          <span class="material-symbols-outlined dialog-ico">install_desktop</span>
          <h3 class="dialog-title">安装插件</h3>
          <p class="dialog-sub">选择 .nmfp 插件包</p>
          <div class="dropzone" @click="fileInput && fileInput.click()">
            <span class="material-symbols-outlined">cloud_upload</span>点击选择 .nmfp 文件
          </div>
          <input ref="fileInput" type="file" accept=".nmfp" class="hidden" @change="onFilePicked">
          <button class="btn btn-text" @click="showInstall = false">取消</button>
        </div>

        <!-- 加载中 -->
        <div v-else-if="previewLoading" class="preview-loading">
          <div class="spinner" style="margin:24px auto"></div>
          <p>正在解析插件...</p>
        </div>

        <!-- 预览错误 -->
        <div v-else-if="previewError" class="preview-error">
          <span class="material-symbols-outlined dialog-ico" style="color:var(--md-error)">error</span>
          <h3 class="dialog-title">解析失败</h3>
          <p class="dialog-sub">{{ previewError }}</p>
          <div class="dialog-actions" style="justify-content:center">
            <button class="btn btn-filled" @click="retryPreview">重试</button>
            <button class="btn btn-text" @click="showInstall = false">关闭</button>
          </div>
        </div>

        <!-- 步骤 2：预览详情 -->
        <template v-else-if="preview">
          <div class="preview-scroll">
            <!-- 插件基本信息 -->
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
                  <span v-if="preview.plugin.author" class="chip">{{ preview.plugin.author }}</span>
                </div>
                <p class="preview-desc" v-if="preview.plugin.description">{{ preview.plugin.description }}</p>
                <p class="preview-pkg">{{ preview.plugin.package_name }}</p>
              </div>
            </div>

            <!-- Schema 校验状态 -->
            <div v-if="!preview.schema_valid" class="preview-section warn">
              <div class="preview-section-title">
                <span class="material-symbols-outlined">warning</span>Schema 校验
              </div>
              <div class="preview-section-body">
                <p class="risk-item" v-for="err in preview.schema_errors" :key="err">{{ err }}</p>
              </div>
            </div>

            <!-- 权限列表 -->
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

            <!-- 安全风险 -->
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

            <!-- 无风险 -->
            <div v-if="!preview.risks.length && preview.permission_conform" class="preview-section">
              <div class="preview-section-title">
                <span class="material-symbols-outlined">check_circle</span>安全检查
              </div>
              <p class="safe-notice">未发现安全风险，权限符合规范</p>
            </div>
          </div>

          <div v-if="installError" class="preview-install-error">
            <span class="material-symbols-outlined">warning</span>{{ installError }}
          </div>

          <!-- 底部操作栏 -->
          <div class="preview-foot">
            <label class="check-row" v-if="preview.plugin.package_name">
              <input type="checkbox" v-model="forceInstall">强制覆盖已安装的同包名插件
            </label>
            <div class="preview-foot-actions">
              <button class="btn btn-text" @click="showInstall = false">取消</button>
              <button class="btn btn-filled" @click="doInstall"
                :disabled="!preview.permission_conform && store.engineStatus?.security_mode === 'strict'">
                <span class="material-symbols-outlined">download</span>安装
              </button>
            </div>
          </div>
        </template>
      </div>
    </div>

    <div v-if="showKey" class="modal-overlay" @click.self="cancelKey">
      <div class="install-dialog">
        <span class="material-symbols-outlined dialog-ico">key</span>
        <h3 class="dialog-title">NotmyFault 安装密钥</h3>
        <p class="dialog-sub">安装 NotmyFault 时输入的密钥</p>
        <input type="password" v-model="keyPw" class="text-field" placeholder="输入密钥" style="width:100%;text-align:center">
        <div class="dialog-actions">
          <button class="btn btn-text" @click="cancelKey">取消</button>
          <button class="btn btn-filled" @click="submitKey">确认</button>
        </div>
      </div>
    </div>
  </section>
</template>
