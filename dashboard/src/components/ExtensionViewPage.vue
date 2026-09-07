<script setup>
import { nextTick, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { closeExtensionSession, getExtensionViewPage, invokeExtensionCommand } from '../lib/api'

const props = defineProps({
  open: Boolean,
  pluginId: { type: String, default: '' },
  viewId: { type: String, default: '' },
  title: { type: String, default: '插件编辑器' },
  sessionId: { type: String, default: '' },
  initialState: { type: [Object, Array, String, Number, Boolean], default: null },
  windowControls: { type: Array, default: () => [] },
})
const emit = defineEmits(['close', 'commit'])
const html = ref('')
const loading = ref(false)
const closing = ref(false)
const error = ref('')
const frameRef = ref(null)
const pageRef = ref(null)
const activeSession = shallowRef(null)
const closingSessions = new Set()
let pageGeneration = 0

function scriptSafeJson(value) {
  return JSON.stringify(value ?? null).replace(/[<>&\u2028\u2029]/g, character => ({
    '<': '\\u003c',
    '>': '\\u003e',
    '&': '\\u0026',
    '\u2028': '\\u2028',
    '\u2029': '\\u2029',
  }[character]))
}

function isolatePage(source, initialState) {
  const documentNode = new DOMParser().parseFromString(source, 'text/html')
  const policy = documentNode.createElement('meta')
  policy.httpEquiv = 'Content-Security-Policy'
  policy.content = [
    "default-src 'none'",
    "script-src 'unsafe-inline'",
    "style-src 'unsafe-inline'",
    'img-src data:',
    'font-src data:',
    'media-src data:',
    "connect-src 'none'",
    "frame-src 'none'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'none'",
  ].join('; ')
  documentNode.head.prepend(policy)
  const bootstrap = documentNode.createElement('script')
  bootstrap.textContent = `window.dispatchEvent(new MessageEvent('message',{source:window.parent,data:{source:'notmyfault:extension-host',type:'init',state:${scriptSafeJson(initialState)}}}))`
  documentNode.body.append(bootstrap)
  return `<!DOCTYPE html>\n${documentNode.documentElement.outerHTML}`
}

function sessionKey(session) {
  return session ? `${session.pluginId}:${session.sessionId}` : ''
}

async function closeSession(session) {
  if (!session?.pluginId || !session?.sessionId) return
  const key = sessionKey(session)
  if (closingSessions.has(key)) return
  closingSessions.add(key)
  try {
    await closeExtensionSession(session.pluginId, session.sessionId)
  } catch (reason) {
    throw reason
  } finally {
    closingSessions.delete(key)
  }
}

watch(() => [props.open, props.pluginId, props.viewId, props.sessionId], async ([open]) => {
  const generation = ++pageGeneration
  const previous = activeSession.value
  if (!open) {
    activeSession.value = null
    html.value = ''
    error.value = ''
    await closeSession(previous).catch(() => {})
    return
  }
  const session = {
    pluginId: props.pluginId,
    sessionId: props.sessionId,
    generation,
  }
  activeSession.value = session
  if (previous && sessionKey(previous) !== sessionKey(session)) void closeSession(previous).catch(() => {})
  loading.value = true
  closing.value = false
  error.value = ''
  try {
    const page = isolatePage(
      await getExtensionViewPage(props.pluginId, props.viewId),
      props.initialState,
    )
    if (pageGeneration === generation && activeSession.value === session) html.value = page
  } catch (reason) {
    if (pageGeneration === generation && activeSession.value === session) {
      error.value = reason.message || '插件编辑器加载失败。'
    }
  } finally {
    if (pageGeneration === generation && activeSession.value === session) {
      loading.value = false
      await nextTick()
      pageRef.value?.focus()
    }
  }
}, { immediate: true })

function sendToView(message) {
  frameRef.value?.contentWindow?.postMessage({
    source: 'notmyfault:extension-host',
    ...message,
  }, '*')
}

function initializeView() {
  sendToView({ type: 'init', state: props.initialState })
}

async function requestClose() {
  if (closing.value) return
  closing.value = true
  pageGeneration += 1
  const session = activeSession.value
  try {
    await closeSession(session)
    activeSession.value = null
    emit('close')
  } catch (reason) {
    error.value = reason.message || '关闭插件会话失败，请重试。'
  } finally {
    closing.value = false
  }
}

function handlePageKey(event) {
  if (event.key !== 'Escape') return
  event.preventDefault()
  requestClose()
}

async function applyWindowAction(action) {
  if (!props.windowControls.includes(action)) return
  try {
    const response = await window.pywebview?.api?.set_window_state?.(action)
    if (!response?.ok) throw new Error(response?.error || 'Dashboard 不支持这个窗口操作。')
  } catch (reason) {
    sendToView({ type: 'window-error', error: reason.message || '窗口操作失败。' })
  }
}

async function receiveFromView(event) {
  if (!props.open || event.source !== frameRef.value?.contentWindow) return
  const generation = pageGeneration
  const session = activeSession.value
  const sourceWindow = event.source
  if (!session) return
  const message = event.data
  if (!message || message.source !== 'notmyfault:extension-view') return
  if (message.type === 'ready') {
    initializeView()
    return
  }
  if (message.type === 'close') {
    requestClose()
    return
  }
  if (message.type === 'window-control') {
    applyWindowAction(message.action)
    return
  }
  if (message.type !== 'invoke' || typeof message.command !== 'string') return
  const requestId = typeof message.request_id === 'string' ? message.request_id : ''
  let response
  try {
    response = await invokeExtensionCommand(session.pluginId, message.command, {
      payload: message.payload,
      sessionId: session.sessionId,
    })
  } catch (reason) {
    response = { ok: false, error: reason.message || '插件命令调用失败。' }
  }
  if (
    !props.open
    || pageGeneration !== generation
    || activeSession.value !== session
    || frameRef.value?.contentWindow !== sourceWindow
  ) return
  sendToView({ type: 'result', request_id: requestId, response })
  if (response?.ok && response?.data?.window_action) {
    applyWindowAction(response.data.window_action)
  }
  if (response?.ok && response.value !== undefined) emit('commit', response.value)
  if (response?.ok && response.close) window.setTimeout(requestClose, 0)
}

window.addEventListener('message', receiveFromView)
onBeforeUnmount(() => {
  window.removeEventListener('message', receiveFromView)
  pageGeneration += 1
  const session = activeSession.value
  activeSession.value = null
  void closeSession(session).catch(() => {})
})
</script>

<template>
  <Teleport to="body">
    <main v-if="open" ref="pageRef" class="extension-page-layer" tabindex="-1"
      :aria-label="title" @keydown="handlePageKey">
      <header class="extension-page-head">
        <button class="icon-btn" title="返回（Esc）" :disabled="closing" @click="requestClose">
          <span class="material-symbols-outlined">arrow_back</span>
        </button>
        <h1>{{ title }}</h1>
      </header>
      <section class="extension-page-body">
        <p v-if="loading" class="extension-page-message"><span class="spinner"></span>正在打开插件编辑器…</p>
        <p v-else-if="error" class="extension-page-message error"><span class="material-symbols-outlined">error</span>{{ error }}</p>
        <iframe v-else-if="html" ref="frameRef" class="extension-page-frame"
          :srcdoc="html" sandbox="allow-scripts" @load="initializeView"></iframe>
        <p v-else class="extension-page-message">这个插件没有返回可用页面。</p>
      </section>
    </main>
  </Teleport>
</template>
