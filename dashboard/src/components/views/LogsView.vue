<script setup>
import { ref, onMounted, onUnmounted, nextTick } from 'vue'
import { readLogRaw } from '../../lib/api'
import { snackbar } from '../../lib/notify'

const logText = ref('加载中...')
const autoRefresh = ref(true)
let timer = null

async function refresh() {
  logText.value = await readLogRaw(300)
  await nextTick()
  const el = document.getElementById('logViewer')
  if (el) el.scrollTop = el.scrollHeight
}

async function copyLog() {
  try {
    await navigator.clipboard.writeText(logText.value)
    snackbar('日志已复制到剪贴板')
  } catch (e) {
    snackbar('复制失败：' + e.message)
  }
}

function syncTimer() {
  if (autoRefresh.value) {
    if (!timer) timer = setInterval(refresh, 3000)
  } else if (timer) {
    clearInterval(timer); timer = null
  }
}

onMounted(() => { refresh(); syncTimer() })
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<template>
  <section class="page active">
    <div class="page-head"><h2>引擎日志</h2><div class="actions">
      <label class="check-row"><input type="checkbox" v-model="autoRefresh" @change="syncTimer">自动刷新</label>
      <button class="btn btn-outlined" @click="copyLog"><span class="material-symbols-outlined">content_copy</span>复制</button>
      <button class="btn btn-outlined" @click="refresh"><span class="material-symbols-outlined">refresh</span>刷新</button>
    </div></div>
    <pre class="log-viewer" id="logViewer">{{ logText }}</pre>
  </section>
</template>
