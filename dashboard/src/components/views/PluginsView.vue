<script setup>
import { ref, computed, onMounted } from 'vue'
import { store } from '../../lib/store'
import { apiWrite, loadPlugins, getSchema, API } from '../../lib/api'
import { snackbar } from '../../lib/notify'
import PluginCard from '../PluginCard.vue'

const tab = ref('triggers')
const list = computed(() => store.pluginsData[tab.value] || {})
const showInstall = ref(false)
const showKey = ref(false)
const keyPw = ref('')
const fileInput = ref(null)
let keyResolve = null

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

async function onFilePicked(e) {
  const file = e.target.files && e.target.files[0]
  if (!file) return

  if (!keyPw.value) {
    try {
      const r = await fetch(API + '/api/plugins/key-status')
      if (r.ok) {
        const ks = await r.json()
        if (ks.encrypted) {
          keyPw.value = ''
          showKey.value = true
          const ok = await new Promise(res => { keyResolve = res })
          if (!ok) return
        }
      }
    } catch (e) { /* 引擎离线，跳过密码检查 */ }
  }

  const fd = new FormData()
  fd.append('file', file)
  if (keyPw.value) fd.append('password', keyPw.value)
  try {
    const r = await apiWrite('/api/plugins/install', 'POST', fd, true)
    const d = await r.json()
    if (d.ok) { snackbar('插件 "' + d.id + '" 安装成功，需重启引擎生效'); showInstall.value = false; await refresh() }
    else alert('安装失败: ' + (d.error || '未知错误'))
  } catch (e) { alert('请求失败: ' + e.message) }
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

    <div v-if="showInstall" class="modal-overlay" @click.self="showInstall = false">
      <div class="install-dialog">
        <span class="material-symbols-outlined dialog-ico">install_desktop</span>
        <h3 class="dialog-title">安装插件</h3>
        <p class="dialog-sub">选择 .nmfp 插件包，点击下方区域上传</p>
        <div class="dropzone" @click="fileInput && fileInput.click()">
          <span class="material-symbols-outlined">cloud_upload</span>点击选择 .nmfp 文件
        </div>
        <input ref="fileInput" type="file" accept=".nmfp" class="hidden" @change="onFilePicked">
        <div class="dialog-actions">
          <button class="btn btn-text" @click="showInstall = false">取消</button>
        </div>
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
