<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import MarkdownIt from 'markdown-it'
import { streamRuleDraftWithAI } from '../lib/api'
import { computeChangeSet } from '../lib/ruleDiff'
import { confirmDialog } from '../lib/dialog'
import { store } from '../lib/store'

const STORAGE_KEY = 'nmf_ai_conversation'
const MAX_HISTORY_ITEMS = 40
const MAX_MESSAGE_CHARS = 4000
const SCROLL_FOLLOW_SLACK = 150
const SAVE_CONVERSATION_DELAY_MS = 500
const COMPOSER_MAX_HEIGHT = 120

const props = defineProps({
  contextRule: { type: Object, default: null },
  fullRule: { type: Object, default: null },
})

const emit = defineEmits(['create', 'highlight-node'])
const composer = ref('')
const drafting = ref(false)
const messages = ref([])
const conversationRef = ref(null)
const composerRef = ref(null)
const userScrolledUp = ref(false)
let messageId = 0
let activeController = null
let activeMessageId = null
let saveConversationTimer = 0

const showWelcome = computed(() => messages.value.length === 0)

const contextChipLabel = computed(() => {
  const rule = props.contextRule
  if (!rule) return '当前规则'
  const name = String(rule.name || '未命名规则').trim() || '未命名规则'
  return `当前规则 · ${name} · ${rule.nodes} 节点`
})

const markdown = new MarkdownIt({ html: false, linkify: true, breaks: true })
const defaultLinkOpen = markdown.renderer.rules.link_open || ((tokens, index, options, _env, self) => (
  self.renderToken(tokens, index, options)
))
markdown.validateLink = (url) => {
  const value = String(url ?? '').trim()
  if (!/^(https?:|mailto:)/i.test(value)) return false
  try {
    const protocol = new URL(value).protocol
    return protocol === 'http:' || protocol === 'https:' || protocol === 'mailto:'
  } catch {
    return false
  }
}
markdown.renderer.rules.link_open = (tokens, index, options, env, self) => {
  tokens[index].attrSet('target', '_blank')
  tokens[index].attrSet('rel', 'noopener noreferrer')
  return defaultLinkOpen(tokens, index, options, env, self)
}
markdown.renderer.rules.image = () => ''

function clipContent(value) {
  return String(value ?? '').trim().slice(0, MAX_MESSAGE_CHARS)
}

function renderMarkdown(content) {
  return markdown.render(String(content ?? ''))
}

function assistantMessage(result) {
  return clipContent(result?.message || result?.content || result?.error)
    || '我还需要一点信息，才能继续起草。'
}
function formatErrorMessage(rawError) {
  const code = rawError?.code
  const text = String(rawError?.error || rawError?.message || rawError?.content || '').trim()

  const httpStatus = text.match(/HTTP\s+(\d{3})/)?.[1]
    || text.match(/请求失败（(\d{3})）/)?.[1]

  if (code === 'idle_timeout') {
    return 'AI 服务超过 120 秒未返回内容。\n请检查网络连接，或稍后重试。'
  }

  if (code === 'ai_provider_failed') {
    return joinErrorParts(text, providerErrorCause(text, httpStatus))
  }

  return text || 'AI 草稿请求失败，请重试。'
}

function joinErrorParts(rawText, cause) {
  // 后端的 ai_provider_failed 文案已经带上同一句前缀。
  const raw = rawText ? (rawText.startsWith('AI 服务返回错误：') ? rawText : `AI 服务返回错误：${rawText}`) : ''
  const hint = cause ? `可能的原因：${cause}` : ''
  return [raw, hint].filter(Boolean).join('\n\n') || 'AI 服务调用失败，请重试。'
}

function providerErrorCause(text, httpStatus) {
  if (!text) return 'AI 服务调用失败，请检查 API Key、模型名称和 endpoint 地址。'
  if (httpStatus === '400') return '请检查接口格式和模型名称，或换一种描述重试。'
  if (httpStatus === '401') return '请检查 API Key 是否有效，以及是否有当前模型的调用权限。'
  if (httpStatus === '403') return '当前 API Key 可能没有这个模型的调用权限，请到 AI 服务后台检查授权。'
  if (httpStatus === '404') return '找不到这个模型或 endpoint 路径，请检查模型名称和接口格式。'
  if (httpStatus === '405') return '这个 endpoint 不支持当前请求方式，请检查地址末尾的接口路径。'
  if (httpStatus === '408') return '等待请求超时，请检查网络后重试。'
  if (httpStatus === '429') return '请求太频繁或额度不足，请稍后重试或检查账户额度。'
  if (httpStatus && Number(httpStatus) >= 500) return '服务端暂时不可用，请稍后重试。'
  if (text.includes('草稿请求失败') || text.includes('网络') || text.includes('连接')) {
    return '无法连接 AI 服务，请检查网络、endpoint 地址和防火墙设置。'
  }
  return ''
}

function formatClientError(reason) {
  if (reason?.name === 'AbortError') return '已停止生成。'
  if (reason?.name === 'TypeError') return '无法连接草稿服务。\n请检查 Dashboard 和网络状态。'
  const text = String(reason?.message || '')
  const httpStatus = text.match(/HTTP\s+(\d{3})/)?.[1] || text.match(/请求失败（(\d{3})）/)?.[1]
  if (httpStatus) {
    return formatErrorMessage({ code: 'ai_provider_failed', error: `AI 服务返回 HTTP ${httpStatus}` })
  }
  return text || '草稿服务暂时不可用，请重试。'
}

function triggerName(result) {
  const triggerType = result?.draft?.event?.type
  return store.schema.triggers?.[triggerType]?.name || triggerType || '还没听清什么时候开始'
}

function nodeNames(nodes) {
  return (nodes || []).map(node => (
    store.schema.actions?.[node.type]?.name || node.type
  )).filter(Boolean)
}

function actionNames(result) {
  return nodeNames(result?.draft?.actions)
}

function preconditionNames(result) {
  return nodeNames(result?.draft?.preconditions)
}

function ruleNodeCount(result) {
  const draft = result?.draft
  if (!draft) return ''
  const count = 1 + (draft.preconditions?.length || 0) + (draft.actions?.length || 0)
  return `${count} 个节点`
}

function ruleParamRows(result) {
  const draft = result?.draft
  if (!draft) return []
  const rows = []
  const push = (nodeLabel, node, catalog) => {
    const definitions = new Map((catalog?.[node?.type]?.params || []).map(param => [param.name, param]))
    Object.entries(node?.params || {}).forEach(([key, value]) => {
      const display = definitions.get(key)?.sensitive
        ? '***'
        : (typeof value === 'string' ? value : JSON.stringify(value))
      rows.push({ node: nodeLabel, key, value: display })
    })
  }
  push(triggerName(result), draft.event, store.schema.triggers)
  ;(draft.preconditions || []).forEach((node, index) => {
    push(`检查 ${index + 1}`, node, store.schema.actions)
  })
  ;(draft.actions || []).forEach((node, index) => {
    push(`动作 ${index + 1}`, node, store.schema.actions)
  })
  return rows
}

function validationIssues(result) {
  return result?.validation?.issues?.slice(0, 5) || []
}

function assistantSummary(result) {
  if (result?.result_type === 'assistant_message') return assistantMessage(result)
  if (result?.result_type === 'rule_draft') {
    const parts = [`当 ${triggerName(result)}`]
    const checks = preconditionNames(result)
    if (checks.length) parts.push(`检查 ${checks.join('、')}`)
    parts.push(`然后 ${actionNames(result).join('、') || '待补充动作'}`)
    return clipContent(`已生成规则草稿：${parts.join('，')}。`)
  }
  return assistantMessage(result)
}

function formatTime(timestamp) {
  if (!timestamp) return ''
  const diff = Date.now() - timestamp
  if (diff < 60000) return '刚刚'
  if (diff < 3600000) return `${Math.floor(diff / 60000)} 分钟前`
  const date = new Date(timestamp)
  const now = new Date()
  if (date.toDateString() === now.toDateString()) {
    return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  }
  return date.toLocaleString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}



function draftEditsCurrentRule(draft) {
  const rule = props.fullRule
  if (!rule || !draft) return false
  const known = new Set()
  for (const node of rule.actions || []) if (node?.binding_id) known.add(node.binding_id)
  for (const node of rule.preconditions || []) if (node?.binding_id) known.add(node.binding_id)
  if (!known.size) return false
  const shares = list => Array.isArray(list) && list.some(node => node?.binding_id && known.has(node.binding_id))
  return shares(draft.actions) || shares(draft.preconditions)
}

function computeMessageChangeSet(message) {
  if (!message.result || message.result.result_type !== 'rule_draft') return []
  if (!props.fullRule) return []
  const draft = message.result.draft
  if (!draft) return []
  if (!draftEditsCurrentRule(draft)) return []
  return computeChangeSet(props.fullRule, draft, store.schema)
}

function hasHighImpactActions(draft) {
  if (!draft) return []
  const issues = []
  const checkAction = (action, label) => {
    if (!action) return
    const meta = store.schema.actions?.[action.type]
    if ((meta?.permissions || []).includes('admin')) {
      issues.push(label + ' 需要管理员权限')
    }
  }
  ;(draft.actions || []).forEach((a, i) => {
    checkAction(a, '动作 ' + (i + 1))
    ;(a.failure_actions || []).forEach((failureAction, failureIndex) => {
      checkAction(failureAction, `动作 ${i + 1} 的补救动作 ${failureIndex + 1}`)
    })
  })
  return issues
}

function highlightNode(item) {
  if (!props.fullRule) return
  let nodeId = null
  if (item.target === 'action' && props.fullRule.actions?.[item.index]?.binding_id) {
    nodeId = 'action-' + props.fullRule.actions[item.index].binding_id
  } else if (item.target === 'failure-action') {
    const parent = props.fullRule.actions?.[item.parentIndex]
    const failureAction = parent?.failure_actions?.[item.index]
    nodeId = failureAction?.binding_id
      ? 'failure-action-' + failureAction.binding_id
      : parent?.binding_id ? 'action-' + parent.binding_id : null
  } else if (item.target === 'precondition' && props.fullRule.preconditions?.[item.index]?.binding_id) {
    nodeId = 'precondition-' + props.fullRule.preconditions[item.index].binding_id
  } else if (item.target === 'trigger') {
    nodeId = 'trigger'
  }
  if (nodeId) emit('highlight-node', nodeId)
}

function applyChangeSet(draft) {
  if (!draft) return
  const next = { ...draft }
  delete next.preconditions
  emit('create', next)
}

function ensureActivity(message) {
  if (!message.activity) {
    message.activity = { steps: [], open: true, finished: false, failed: false }
  }
  return message.activity
}

function addActivityStep(message, label, state = 'running') {
  const activity = ensureActivity(message)
  if (!activity.steps.some(step => step.label === label)) {
    activity.steps.push({ label, state })
  }
}

function completeActivityStep(message, label) {
  const step = message.activity?.steps.find(item => item.label === label)
  if (step) step.state = 'done'
}

function finishActivity(message, failed = false) {
  const activity = message.activity
  if (!activity) return
  activity.steps.forEach(step => { step.state = 'done' })
  activity.finished = true
  activity.failed = failed
  activity.open = false
}

function activityHeadline(message) {
  const activity = message.activity
  if (!activity) return ''
  if (activity.failed) return '已中断'
  if (activity.finished) return `已完成 · ${activity.steps.length} 步`
  const running = activity.steps.find(step => step.state === 'running')
  return running ? running.label : '正在处理…'
}

async function scrollConversation() {
  await nextTick()
  const container = conversationRef.value
  if (!container || userScrolledUp.value) return
  container.scrollTop = container.scrollHeight
}

// 用户往上翻时暂停自动跟随，回到底部附近再恢复。
function onConversationScroll() {
  const container = conversationRef.value
  if (!container) return
  const distance = container.scrollHeight - container.scrollTop - container.clientHeight
  userScrolledUp.value = distance > SCROLL_FOLLOW_SLACK
}

function jumpToLatest() {
  userScrolledUp.value = false
  void scrollConversation()
}

async function appendMessage(role, content, result = null, state = {}) {
  const message = {
    id: ++messageId,
    role,
    timestamp: Date.now(),
    content: String(content ?? ''),
    result,
    error: false,
    activity: null,
    requestMessageId: state.requestMessageId || null,
    retryPrompt: '',
    transient: state.transient === true,
    streaming: state.streaming === true,
    stopped: false,
    reasoning: '',
    reasoningOpen: true,
    ruleDetailsOpen: false,
  }
  messages.value.push(message)
  await scrollConversation()
  // 裸对象的后续改动不会触发 Vue 更新，这里返回 messages 数组里的代理对象。
  return messages.value[messages.value.length - 1]
}

function messageById(id) {
  return messages.value.find(message => message.id === id) || null
}

function finishResult(message, result) {
  const isAssistantMessage = result?.result_type === 'assistant_message'
  message.content = isAssistantMessage && message.content.trim()
    ? message.content
    : isAssistantMessage
      ? assistantMessage(result)
      : assistantSummary(result)
  message.result = result
  message.error = false
  message.transient = false
  message.streaming = false
  message.retryPrompt = ''
  finishActivity(message)
}

function finishError(message, text, retryPrompt = '') {
  message.content = text
  message.result = null
  message.error = true
  message.transient = false
  message.streaming = false
  message.retryPrompt = retryPrompt
  finishActivity(message, true)
}

function requestMessages() {
  const ignoredRequestIds = new Set(messages.value
    .filter(message => message.role === 'assistant' && (message.error || message.stopped))
    .map(message => message.requestMessageId)
    .filter(Boolean))
  return messages.value
    .filter(message => !message.transient && (message.role === 'user' || message.role === 'assistant'))
    .filter(message => !message.error && !message.stopped && !ignoredRequestIds.has(message.id))
    .map(message => ({ role: message.role, content: clipContent(message.content) }))
    .filter(message => message.content)
    .slice(-MAX_HISTORY_ITEMS)
}
function loadConversation() {
  try {
    localStorage.removeItem(STORAGE_KEY)
    const saved = sessionStorage.getItem(STORAGE_KEY)
    if (!saved) return
    const parsed = JSON.parse(saved)
    if (Array.isArray(parsed) && parsed.length > 0) {
      messages.value = parsed.map(msg => {
        const activity = msg.activity
          ? {
            ...msg.activity,
            open: false,

            finished: true,
            failed: msg.activity.finished ? msg.activity.failed : true,
          }
          : null
        return {
          ...msg,
          timestamp: msg.timestamp || Date.now(),
          transient: false,
          streaming: false,
          stopped: false,
          error: msg.error === true,
          progress: null,
          activity,
          requestMessageId: msg.requestMessageId || null,
          retryPrompt: typeof msg.retryPrompt === 'string' ? msg.retryPrompt : '',
          ruleDetailsOpen: false,
          reasoningOpen: false,
        }
      })
      messageId = Math.max(...parsed.map(m => m.id || 0), 0)
    }
  } catch (err) {
    console.warn('加载对话失败:', err)
  }
}

function saveConversation() {
  try {
    const toSave = messages.value
      .filter(m => !m.transient)
      .slice(-MAX_HISTORY_ITEMS)
      .map(({ id, role, timestamp, content, stopped, error, requestMessageId, retryPrompt }) => ({
        id, role, timestamp, content, stopped, error, requestMessageId, retryPrompt,
      }))
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(toSave))
  } catch (err) {
    console.warn('保存对话失败:', err)
  }
}

function scheduleSaveConversation() {
  window.clearTimeout(saveConversationTimer)
  saveConversationTimer = window.setTimeout(saveConversation, SAVE_CONVERSATION_DELAY_MS)
}

function flushSaveConversation() {
  window.clearTimeout(saveConversationTimer)
  saveConversationTimer = 0
  saveConversation()
}

function handleStreamEvent(message, event, markDone, retryPrompt = '') {
  if (event.type === 'status') {
    ensureActivity(message)
    addActivityStep(message, '连接 AI 服务', 'done')
    return
  }
  if (event.type === 'reasoning') {
    const delta = String(event.data?.delta ?? '')
    if (delta) {
      addActivityStep(message, '分析需求', 'running')
      message.reasoning += delta
      void scrollConversation()
    }
    return
  }
  if (event.type === 'progress') {
    const phase = event.data?.phase
    if (phase === 'drafting') {
      completeActivityStep(message, '分析需求')
      addActivityStep(message, '生成草稿', 'running')
    } else if (phase === 'assembled') {
      completeActivityStep(message, '生成草稿')
    } else if (phase === 'validating') {
      completeActivityStep(message, '生成草稿')
      addActivityStep(message, '校验规则结构', 'running')
    }
    return
  }
  if (event.type === 'text') {
    const delta = String(event.data?.delta ?? '')
    if (delta) {
      if (message.reasoning && !message.content) message.reasoningOpen = false
      completeActivityStep(message, '分析需求')
      addActivityStep(message, '生成回复', 'running')
      message.content += delta
      void scrollConversation()
    }
    return
  }
  if (event.type === 'result') {
    completeActivityStep(message, '生成回复')
    completeActivityStep(message, '校验规则结构')
    finishResult(message, event.data)
    void scrollConversation()
    markDone()
    return
  }
  if (event.type === 'error') {
    finishError(message, formatErrorMessage(event.data), retryPrompt)
    void scrollConversation()
    markDone()
    return
  }
  if (event.type === 'done') markDone()
}

async function sendTurn(content) {
  const text = clipContent(content)
  if (!text || drafting.value) return

  // 自己发言等于回到当下，重新贴住底部。
  userScrolledUp.value = false
  drafting.value = true
  const requestMessage = await appendMessage('user', text)
  const message = await appendMessage('assistant', '', null, {
    requestMessageId: requestMessage.id,
    transient: true,
    streaming: true,
  })
  const controller = new AbortController()
  let receivedDone = false
  activeController = controller
  activeMessageId = message.id
  try {
    await streamRuleDraftWithAI(requestMessages(), {
      signal: controller.signal,
      onEvent: event => {
        if (!controller.signal.aborted) {
          handleStreamEvent(message, event, () => { receivedDone = true }, text)
        }
      },
    })
    if (!controller.signal.aborted && !receivedDone) {
      finishError(message, '连接已中断，请检查网络后重试。', text)
    } else if (!controller.signal.aborted && message.transient) {
      finishError(message, '草稿服务没有返回结果，请重试。', text)
    }
  } catch (reason) {
    if (!controller.signal.aborted) {
      finishError(message, formatClientError(reason), text)
    }
  } finally {
    if (activeController === controller) {
      activeController = null
      activeMessageId = null
      drafting.value = false
    }
    if (!controller.signal.aborted) {
      message.streaming = false
      finishActivity(message, message.error)
      await scrollConversation()
      await nextTick()
      composerRef.value?.focus({ preventScroll: true })
    }
  }
}

function stopDrafting() {
  if (!activeController) return
  const message = messageById(activeMessageId)
  activeController.abort()
  activeController = null
  activeMessageId = null
  drafting.value = false
  if (message) {
    message.streaming = false
    message.stopped = true
    message.transient = false
    finishActivity(message, true)
  }
  void nextTick(() => composerRef.value?.focus({ preventScroll: true }))
}

function isLatestMessage(message) {
  return messages.value.at(-1)?.id === message.id
}

async function retryFailedMessage(message) {
  if (drafting.value || !message.error || !message.retryPrompt || !isLatestMessage(message)) return
  const failedIndex = messages.value.findIndex(item => item.id === message.id)
  if (failedIndex < 0) return
  const requestIndex = messages.value.findIndex(item => item.id === message.requestMessageId)
  if (requestIndex >= 0 && requestIndex < failedIndex) {
    messages.value.splice(requestIndex, failedIndex - requestIndex + 1)
  } else {
    messages.value.splice(failedIndex, 1)
  }
  await sendTurn(message.retryPrompt)
}

// 结束一轮探索后清空重来，避免历史干扰后续起草。
async function clearConversation() {
  if (drafting.value) return
  if (!messages.value.length) return
  if (!await confirmDialog('清空当前对话？', '已生成的对话和规则草稿都会消失。', '清空')) return
  messages.value = []
  try { sessionStorage.removeItem(STORAGE_KEY) } catch {}
  try { localStorage.removeItem(STORAGE_KEY) } catch {}
  userScrolledUp.value = false
  void nextTick(() => composerRef.value?.focus({ preventScroll: true }))
}

async function requestNewConversation() {
  await clearConversation()
}

function downloadConversation() {
  const payload = messages.value
    .filter(m => !m.transient)
    .map(({ id, role, timestamp, content, stopped, error }) => ({ id, role, timestamp, content, stopped, error }))
  if (!payload.length) return
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = 'notmyfault-ai-conversation.json'
  a.click()
  URL.revokeObjectURL(a.href)
}

defineExpose({ requestNewConversation, downloadConversation })

async function sendMessage() {
  const text = clipContent(composer.value)
  if (!text) return
  composer.value = ''
  void nextTick(() => { growTextarea() })
  await sendTurn(text)
}

function sendChip(prompt) {
  if (drafting.value) return
  void sendTurn(prompt)
}

function growTextarea() {
  const el = composerRef.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, COMPOSER_MAX_HEIGHT) + 'px'
}

function onComposerKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    sendMessage()
  }
}

function openDraft(result) {
  if (result?.draft) emit('create', result.draft)
}

watch(messages, scheduleSaveConversation, { deep: true })

onMounted(async () => {
  loadConversation()
  await nextTick()
  composerRef.value?.focus({ preventScroll: true })
  void scrollConversation()
})
onUnmounted(() => {
  activeController?.abort()
  flushSaveConversation()
})
</script>

<template>
  <section class="ai-draft-panel natural-draft-panel flex h-full min-h-0 flex-1 flex-col">

    <div v-if="showWelcome" class="ai-empty ai-empty-enter flex min-h-0 flex-1 flex-col items-center justify-center gap-1.5 px-6 text-center">
      <span class="material-symbols-outlined text-[36px]! text-primary">auto_awesome</span>
      <p class="m-0 mt-2 text-title-m text-on-surface">AI 自动化助手</p>
      <p class="m-0 text-body-s text-on-surface-variant">用自然语言创建、修改或检查当前自动化规则。</p>
      <div class="mt-4 flex flex-wrap justify-center gap-2">
        <button type="button" class="ai-empty-chip" :disabled="drafting" @click="sendChip('帮我创建一条新的自动化规则')">创建自动化</button>
        <button type="button" class="ai-empty-chip" :disabled="drafting" @click="sendChip('检查当前规则是否存在问题')">检查当前规则</button>
      </div>
      <p class="m-0 mt-4 text-label-s text-on-surface-variant opacity-70">例如：打开 PowerPoint 时将系统音量设为 30%</p>
    </div>

    <div v-else class="relative flex min-h-0 flex-1 flex-col">
      <div ref="conversationRef"
        class="natural-draft-conversation min-h-0 flex-1 overflow-y-auto"
        role="log" tabindex="0" :aria-busy="drafting" aria-live="polite" aria-label="AI 对话"
        @scroll.passive="onConversationScroll">
        <TransitionGroup name="ai-msg" tag="div" class="ai-turns">
          <article v-for="message in messages" :key="message.id" class="ai-message" :class="message.role">
            <template v-if="message.role === 'user'">
              <div class="ai-meta user"><span>你</span><span class="ai-meta-time">{{ formatTime(message.timestamp) }}</span></div>
              <div class="ai-user-bubble">{{ message.content }}</div>
            </template>

            <template v-else>
              <div class="ai-meta assistant">
                <span class="material-symbols-outlined">auto_awesome</span>
                <span>AI 助手</span>
                <span class="ai-meta-time">{{ formatTime(message.timestamp) }}</span>
              </div>

              <div v-if="message.error" class="ai-error" role="alert">
                <div class="ai-error-head"><span class="material-symbols-outlined">warning</span><b>无法生成结果</b></div>
                <p>{{ message.content }}</p>
                <button v-if="message.retryPrompt" class="ai-error-retry" type="button"
                  :disabled="drafting || !isLatestMessage(message)" @click="retryFailedMessage(message)">
                  <span class="material-symbols-outlined">refresh</span>重新尝试
                </button>
              </div>

              <template v-else>
                <div v-if="message.activity && message.activity.steps.length" class="ai-activity"
                  :class="{ open: message.activity.open || !message.activity.finished, failed: message.activity.failed, finished: message.activity.finished }">
                  <button type="button" class="ai-activity-toggle"
                    :aria-expanded="String(message.activity.open)"
                    @click="message.activity.open = !message.activity.open">
                    <span v-if="message.activity.failed" class="material-symbols-outlined">error</span>
                    <span v-else-if="message.activity.finished" class="material-symbols-outlined">check_circle</span>
                    <span v-else class="material-symbols-outlined ai-activity-spin">progress_activity</span>
                    <span>{{ activityHeadline(message) }}</span>
                    <span v-if="message.activity.finished" class="material-symbols-outlined ai-activity-chevron">expand_more</span>
                  </button>
                  <div class="ai-activity-body">
                    <div class="ai-activity-steps">
                      <div v-for="step in message.activity.steps" :key="step.label" class="ai-activity-step" :class="step.state">
                        <span v-if="step.state === 'done'" class="material-symbols-outlined">check</span>
                        <span v-else class="material-symbols-outlined ai-activity-spin">progress_activity</span>
                        <span>{{ step.label }}</span>
                      </div>
                    </div>
                  </div>
                </div>

                <div v-if="message.reasoning" class="ai-reasoning" :class="{ open: message.reasoningOpen }">
                  <button type="button" class="ai-reasoning-toggle"
                    :aria-expanded="String(message.reasoningOpen)"
                    @click="message.reasoningOpen = !message.reasoningOpen">
                    <span class="material-symbols-outlined">psychology</span>
                    <span class="ai-reasoning-label">思考过程</span>
                    <span class="material-symbols-outlined ai-reasoning-chevron">expand_more</span>
                  </button>
                  <div class="ai-reasoning-body">
                    <div class="ai-reasoning-scroll">
                      <p class="ai-reasoning-text">{{ message.reasoning }}</p>
                    </div>
                  </div>
                </div>

                <div v-if="message.result?.result_type === 'rule_draft'" class="ai-rule-card">
                  <template v-if="computeMessageChangeSet(message).length">
                    <div class="ai-rule-head">
                      <span class="ai-card-kicker"><span class="material-symbols-outlined">difference</span>建议更改</span>
                      <span class="ai-rule-counts">{{ computeMessageChangeSet(message).length }} 项修改</span>
                    </div>
                    <div class="ai-changeset-list">
                      <div v-for="(item, ci) in computeMessageChangeSet(message)" :key="ci"
                        class="ai-changeset-item" :class="'ai-changeset-' + item.op"
                        :clickable="['action', 'failure-action', 'precondition', 'trigger'].includes(item.target)"
                        @click="highlightNode(item)">
                        <span class="ai-changeset-op">{{ item.op === 'add' ? 'A' : item.op === 'delete' ? 'D' : 'M' }}</span>
                        <span class="ai-changeset-label">{{ item.label }}</span>
                        <span v-if="item.detail" class="ai-changeset-detail">{{ item.detail }}</span>
                      </div>
                    </div>
                    <div v-if="hasHighImpactActions(message.result.draft).length" class="ai-security-warn">
                      <span class="material-symbols-outlined">shield</span>
                      <div>
                        <b>此修改包含需要额外确认的操作</b>
                        <ul><li v-for="(w, wi) in hasHighImpactActions(message.result.draft)" :key="wi">{{ w }}</li></ul>
                        <small>请在应用前检查生成内容。</small>
                      </div>
                    </div>
                    <div class="ai-rule-foot">
                      <button class="btn btn-text btn-sm" type="button" @click="message.ruleDetailsOpen = !message.ruleDetailsOpen">
                        {{ message.ruleDetailsOpen ? '收起详情' : '查看详情' }}
                      </button>
                      <button class="btn btn-tonal btn-sm" type="button" :disabled="!message.result.draft" @click="applyChangeSet(message.result.draft)">
                        全部应用<span class="material-symbols-outlined">arrow_forward</span>
                      </button>
                    </div>
                  </template>
                  <template v-else>
                    <div class="ai-rule-head">
                      <span class="ai-card-kicker"><span class="material-symbols-outlined">layers</span>候选规则</span>
                      <span class="ai-rule-counts">{{ ruleNodeCount(message.result) }}</span>
                    </div>
                    <div class="ai-rule-flow">
                      <div class="ai-mini-node">
                        <span class="material-symbols-outlined">bolt</span>
                        <span class="min-w-0 flex-1 truncate">{{ triggerName(message.result) }}</span>
                      </div>
                      <div v-for="name in preconditionNames(message.result)" :key="`check-${name}`" class="ai-mini-node">
                        <span class="material-symbols-outlined">verified_user</span>
                        <span class="min-w-0 flex-1 truncate">{{ name }}</span>
                      </div>
                      <div v-for="name in actionNames(message.result)" :key="`action-${name}`" class="ai-mini-node">
                        <span class="material-symbols-outlined">play_arrow</span>
                        <span class="min-w-0 flex-1 truncate">{{ name }}</span>
                      </div>
                    </div>
                    <div class="ai-rule-foot">
                      <button class="btn btn-text btn-sm" type="button" @click="message.ruleDetailsOpen = !message.ruleDetailsOpen">
                        {{ message.ruleDetailsOpen ? '收起参数' : '查看参数' }}
                      </button>
                      <button class="btn btn-tonal btn-sm" type="button" :disabled="!message.result.draft" @click="openDraft(message.result)">
                        应用到编辑器<span class="material-symbols-outlined">arrow_forward</span>
                      </button>
                    </div>
                  </template>
                  <div v-if="validationIssues(message.result).length" class="ai-issues">
                    <div v-for="issue in validationIssues(message.result)" :key="`${issue.code}-${issue.message}`" class="ai-issue">
                      <span class="material-symbols-outlined">{{ issue.severity === 'warning' ? 'warning' : 'error' }}</span>{{ issue.message }}
                    </div>
                  </div>
                  <div v-if="message.ruleDetailsOpen" class="ai-rule-params">
                    <div v-for="row in ruleParamRows(message.result)" :key="`${row.node}-${row.key}`" class="ai-rule-param-row">
                      <span>{{ row.node }}</span>
                      <code>{{ row.key }} = {{ row.value }}</code>
                    </div>
                  </div>
                </div>

                <div v-else-if="message.content" class="ai-content">
                  <div v-if="message.streaming" class="ai-stream-text">{{ message.content }}<span class="ai-stream-cursor" aria-hidden="true"></span></div>
                  <div v-else class="natural-draft-markdown" v-html="renderMarkdown(message.content)"></div>
                </div>

                <div v-if="message.stopped" class="ai-stopped"><span class="material-symbols-outlined">stop_circle</span>已停止</div>
              </template>
            </template>
          </article>
        </TransitionGroup>
      </div>
      <Transition name="ai-msg">
        <button v-if="userScrolledUp" type="button" class="ai-jump-latest" @click="jumpToLatest">
          <span class="material-symbols-outlined">arrow_downward</span>回到底部
        </button>
      </Transition>
    </div>

    <form class="ai-composer flex flex-none flex-col gap-2" @submit.prevent="sendMessage">
      <label for="natural-draft-description" class="sr-only">描述你想创建或修改的自动化</label>
      <textarea
        id="natural-draft-description"
        ref="composerRef"
        v-model="composer"
        class="ai-composer-input"
        :maxlength="MAX_MESSAGE_CHARS"
        rows="2"
        placeholder="描述你想创建或修改的自动化……"
        :disabled="drafting"
        @input="growTextarea"
        @keydown="onComposerKeydown"
      ></textarea>
      <div class="ai-composer-row">
        <span class="ai-context-chip" :title="contextChipLabel">
          <span class="material-symbols-outlined">layers</span>
          <span class="truncate">{{ contextChipLabel }}</span>
        </span>
        <div class="min-w-0 flex-1"></div>
        <span class="ai-mode-chip" title="AI 会自动调用规则工具完成任务">Agent</span>
        <button v-if="drafting" type="button" class="ai-send-btn ai-stop-btn" title="停止生成" @click="stopDrafting">
          <span class="material-symbols-outlined">stop_circle</span>
        </button>
        <button v-else type="submit" class="ai-send-btn" :disabled="!composer.trim()" title="发送（Enter）">
          <span class="material-symbols-outlined">send</span>
        </button>
      </div>
    </form>

  </section>
</template>

<style scoped>
/* P0 修复：AI Panel flex 布局 + Conversation 滚动容器 */
/* min-width:0 必须有：flex item 的 min-width:auto 会取子树 min-content，
   长代码行会把面板撑到内容宽后被 overflow:hidden 裁切。 */
.ai-draft-panel{display:flex;flex:1 1 0%;flex-direction:column;height:100%;min-height:0;min-width:0;max-width:100%}
.natural-draft-conversation{flex:1;min-height:0;overflow-y:auto;padding:16px}

.ai-turns{display:flex;flex-direction:column;gap:22px}
.ai-message{display:flex;min-width:0;max-width:100%;flex-direction:column;gap:8px}


.ai-meta{display:flex;align-items:center;gap:6px;margin-bottom:8px;font:var(--ts-label-m);color:var(--md-on-surface-variant)}
.ai-meta .material-symbols-outlined{font-size:14px;color:var(--md-primary)}
.ai-meta.user{justify-content:flex-end}
.ai-meta-time{margin-left:auto;opacity:.7}


.ai-user-bubble{max-width:78%;min-width:0;align-self:flex-end;padding:8px 12px;border-radius:12px 12px 4px 12px;background:var(--md-surface-c-high);color:var(--md-on-surface);font-size:14px;line-height:22px;white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere}


.ai-content{width:100%;min-width:0;max-width:100%;font-size:14px;line-height:22px;color:var(--md-on-surface);overflow-wrap:anywhere;word-break:break-word}
.ai-stream-text{white-space:pre-wrap;word-break:break-word}
.ai-stream-cursor{display:inline-block;width:2px;height:14px;margin-left:1px;background:var(--md-primary);vertical-align:-2px;animation:ai-cursor-blink 1s step-end infinite}
@keyframes ai-cursor-blink{50%{opacity:0}}

.natural-draft-markdown :deep(p){margin:0 0 8px}
.natural-draft-markdown :deep(p:last-child){margin-bottom:0}
.natural-draft-markdown :deep(ul),.natural-draft-markdown :deep(ol){margin:0 0 8px;padding-left:18px}
.natural-draft-markdown :deep(li){margin:2px 0}
.natural-draft-markdown :deep(pre){margin:0 0 8px;padding:8px 10px;border-radius:var(--r-sm);background:var(--md-surface-c-low);overflow:auto;font-family:ui-monospace,Consolas,monospace;font-size:13px;line-height:19px}
.natural-draft-markdown :deep(code){font-family:ui-monospace,Consolas,monospace;font-size:13px}
.natural-draft-markdown :deep(:not(pre)>code){padding:1px 5px;border-radius:4px;background:var(--md-surface-c-low)}
.natural-draft-markdown :deep(a){color:var(--md-primary)}
.natural-draft-markdown :deep(h1),.natural-draft-markdown :deep(h2),.natural-draft-markdown :deep(h3),.natural-draft-markdown :deep(h4){margin:8px 0 6px;font-size:14px;font-weight:600}


.ai-activity{margin-bottom:4px;font:var(--ts-label-m);color:var(--md-on-surface-variant)}
.ai-activity-toggle{display:flex;width:100%;align-items:center;gap:6px;padding:2px 0;border:none;background:transparent;color:inherit;font:inherit;text-align:left;cursor:pointer}
.ai-activity-toggle>.material-symbols-outlined{flex:none;font-size:15px}
.ai-activity.finished .ai-activity-toggle>.material-symbols-outlined:first-child{color:var(--md-success)}
.ai-activity.failed .ai-activity-toggle>.material-symbols-outlined:first-child{color:var(--md-error)}
.ai-activity-spin{animation:ai-activity-rotate 1.6s linear infinite}
@keyframes ai-activity-rotate{to{transform:rotate(360deg)}}
.ai-activity-chevron{margin-left:auto;font-size:16px;transition:transform .2s ease}
.ai-activity.open .ai-activity-chevron{transform:rotate(180deg)}
/* overflow:hidden 让 0fr 折叠真正归零；纵向 padding 移到子项，
   否则 padding 会成为 fr 轨道（minmax(auto,0fr)）的最小高度。 */
.ai-activity-body{display:grid;grid-template-rows:0fr;transition:grid-template-rows .2s ease;overflow:hidden}
.ai-activity.open .ai-activity-body{grid-template-rows:1fr}
.ai-activity-steps{min-height:0;max-height:180px;overflow-y:auto;display:flex;flex-direction:column;gap:4px;padding:0 0 0 21px}
.ai-activity-step:first-child{margin-top:8px}
.ai-activity-step:last-child{margin-bottom:4px}
.ai-activity-step{display:flex;align-items:center;gap:6px;font-size:13px}
.ai-activity-step .material-symbols-outlined{flex:none;font-size:13px}
.ai-activity-step.done .material-symbols-outlined{color:var(--md-success)}
.ai-activity-step:not(.done){opacity:.85}
.ai-reasoning{margin-bottom:4px;font:var(--ts-label-m);color:var(--md-on-surface-variant)}
.ai-reasoning-toggle{display:flex;width:100%;align-items:center;gap:6px;padding:2px 0;border:none;background:transparent;color:inherit;font:inherit;text-align:left;cursor:pointer}
.ai-reasoning-toggle>.material-symbols-outlined{flex:none;font-size:15px}
.ai-reasoning-label{flex:none}
.ai-reasoning-chevron{margin-left:auto;font-size:16px;transition:transform .2s ease}
.ai-reasoning.open .ai-reasoning-chevron{transform:rotate(180deg)}
.ai-reasoning-body{display:grid;grid-template-rows:0fr;transition:grid-template-rows .2s ease;overflow:hidden}
.ai-reasoning.open .ai-reasoning-body{grid-template-rows:1fr}
.ai-reasoning-scroll{min-height:0;max-height:200px;overflow-y:auto;padding:0 0 0 21px}
.ai-reasoning-text{margin:8px 0 4px;white-space:pre-wrap;word-break:break-word;font:var(--ts-body-s);color:var(--md-on-surface-variant)}


.ai-rule-card{display:flex;flex-direction:column;gap:12px;width:100%;min-width:0;max-width:100%;padding:14px;border:1px solid var(--md-outline-variant);border-radius:var(--r-md);background:var(--md-surface-c-low)}
.ai-rule-head{display:flex;align-items:center;justify-content:space-between;gap:8px}
.ai-card-kicker{display:inline-flex;align-items:center;gap:6px;color:var(--md-primary);font:var(--ts-label-m);font-weight:500}
.ai-card-kicker .material-symbols-outlined{font-size:18px}
.ai-rule-counts{font:var(--ts-label-m);color:var(--md-on-surface-variant)}
.ai-rule-flow{display:flex;flex-direction:column;gap:0}
.ai-mini-node{display:flex;align-items:center;gap:8px;min-height:32px;min-width:0;padding:6px 10px;border:1px solid var(--md-outline-variant);border-radius:var(--r-sm);background:var(--md-surface);font-size:13px;line-height:18px;color:var(--md-on-surface)}
.ai-mini-node .material-symbols-outlined{flex:none;font-size:16px;color:var(--md-primary)}
.ai-mini-node>span:last-child{min-width:0;flex:1}
.ai-mini-node+.ai-mini-node{margin-top:10px;position:relative}
.ai-mini-node+.ai-mini-node::before{content:'';position:absolute;left:17px;top:-10px;height:10px;border-left:2px solid var(--md-outline-variant)}
.ai-rule-foot{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}
.ai-rule-params{display:flex;flex-direction:column;gap:6px;padding-top:12px;margin-top:4px;border-top:1px dashed var(--md-outline-variant)}
.ai-rule-param-row{display:grid;grid-template-columns:auto minmax(0,1fr);gap:10px;font-size:12px;line-height:17px;color:var(--md-on-surface-variant)}
.ai-rule-param-row>span:first-child{white-space:nowrap}
.ai-rule-param-row code{font-family:ui-monospace,Consolas,monospace;font-size:12px;word-break:break-all;color:var(--md-on-surface);overflow-wrap:anywhere}



.ai-issues{display:flex;flex-direction:column;gap:4px;padding:8px 10px;border-radius:var(--r-sm);background:color-mix(in srgb,var(--md-warn) 9%,transparent)}
.ai-issue{display:flex;align-items:flex-start;gap:6px;font-size:12px;line-height:17px;color:var(--md-on-surface-variant)}
.ai-issue .material-symbols-outlined{flex:none;margin-top:1px;font-size:14px;color:var(--md-warn)}

.ai-error{display:flex;flex-direction:column;gap:8px;width:100%;min-width:0;max-width:100%;padding:12px 14px;border:1px solid color-mix(in srgb,var(--md-error) 35%,transparent);border-left:3px solid var(--md-error);border-radius:var(--r-sm);background:var(--md-error-container);color:var(--md-on-error-container)}
.ai-error-head{display:flex;align-items:center;gap:6px;font:var(--ts-label-m)}
.ai-error-head .material-symbols-outlined{flex:none;font-size:16px}
.ai-error p{margin:0;font-size:13px;line-height:19px;white-space:pre-line;overflow-wrap:anywhere}
.ai-error-retry{display:inline-flex;align-items:center;gap:5px;align-self:flex-start;height:32px;padding:0 14px;border:none;border-radius:var(--r-full);background:color-mix(in srgb,var(--md-on-error-container) 12%,transparent);color:var(--md-on-error-container);font:var(--ts-label-m);cursor:pointer}
.ai-error-retry:disabled{opacity:.5;cursor:default}
.ai-error-retry .material-symbols-outlined{flex:none;font-size:15px}

.ai-stopped{display:flex;align-items:center;gap:5px;font:var(--ts-label-m);color:var(--md-on-surface-variant)}
.ai-stopped .material-symbols-outlined{flex:none;font-size:14px}

.ai-msg-move{transition:transform .18s ease}

.ai-jump-latest{position:absolute;right:8px;bottom:10px;z-index:5;display:inline-flex;align-items:center;gap:4px;height:28px;padding:0 10px;border:1px solid var(--md-outline-variant);border-radius:var(--r-full);background:var(--md-surface-c-highest);color:var(--md-on-surface-variant);font:var(--ts-label-s);box-shadow:var(--md-elev2);cursor:pointer}
.ai-jump-latest .material-symbols-outlined{font-size:14px}


.ai-composer{flex:0 0 auto;flex-direction:column;gap:8px;padding:12px 14px;border-top:1px solid var(--md-outline-variant);background:var(--md-surface-c)}
.ai-composer-input{width:100%;min-height:56px;max-height:120px;resize:none;overflow-y:auto;padding:10px 12px;border:1px solid var(--md-outline);border-radius:var(--r-sm);background:var(--md-surface-c-lowest);color:var(--md-on-surface);font:var(--ts-body-m);line-height:22px;outline:none;transition:border-color .15s ease}
.ai-composer-input:focus{border-color:var(--md-primary)}
.ai-composer-input:disabled{opacity:.6}
.ai-composer-input::placeholder{color:var(--md-on-surface-variant);opacity:.75}
.ai-composer-row{display:flex;align-items:center;gap:8px;min-width:0}
.ai-context-chip{display:inline-flex;max-width:58%;align-items:center;gap:5px;height:26px;padding:0 10px;border-radius:var(--r-full);background:var(--md-surface-c-high);color:var(--md-on-surface-variant);font:var(--ts-label-s);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ai-context-chip .material-symbols-outlined{flex:none;font-size:14px;color:var(--md-primary)}
.ai-mode-chip{display:inline-flex;align-items:center;height:26px;padding:0 10px;border-radius:var(--r-full);background:var(--md-secondary-container);color:var(--md-on-secondary-container);font:var(--ts-label-s);font-weight:500}
.ai-send-btn{display:inline-flex;align-items:center;justify-content:center;flex:none;width:38px;height:38px;border:none;border-radius:var(--r-full);background:var(--md-primary);color:var(--md-on-primary);cursor:pointer;transition:background-color .15s ease,opacity .15s ease}
.ai-send-btn .material-symbols-outlined{flex:none;font-size:19px}
.ai-send-btn:hover:not(:disabled){box-shadow:var(--md-elev1)}
.ai-send-btn:disabled{background:var(--md-surface-c-high);color:color-mix(in srgb,var(--md-on-surface-variant) 55%,transparent);cursor:default}
.ai-stop-btn{background:var(--md-error-container);color:var(--md-on-error-container)}
.ai-stop-btn:hover{box-shadow:var(--md-elev1)}


.ai-empty-chip{display:inline-flex;align-items:center;gap:6px;height:32px;padding:0 14px;border:1px solid var(--md-outline-variant);border-radius:var(--r-full);background:transparent;color:var(--md-primary);font:var(--ts-label-m);cursor:pointer;transition:background-color .15s ease,color .15s ease,border-color .15s ease}
.ai-empty-chip:hover:not(:disabled){background:var(--md-primary-container);color:var(--md-on-primary-container);border-color:transparent}
.ai-empty-chip:focus-visible{outline:2px solid var(--md-primary);outline-offset:2px}
.ai-empty-chip:disabled{opacity:.5;cursor:default}


.ai-changeset-list{display:flex;flex-direction:column;gap:2px;margin-top:4px}
.ai-changeset-item{display:grid;grid-template-columns:24px auto minmax(0,1fr);align-items:center;gap:8px;padding:6px 8px;border-radius:var(--r-xs);font-size:13px;line-height:18px;color:var(--md-on-surface);transition:background .12s ease}
.ai-changeset-item[clickable]{cursor:pointer}
.ai-changeset-item[clickable]:hover{background:color-mix(in srgb,var(--md-primary) 8%,transparent)}
.ai-changeset-op{display:flex;width:20px;height:20px;align-items:center;justify-content:center;border-radius:var(--r-xs);font-size:11px;font-weight:600;font-family:ui-monospace,Consolas,monospace}
.ai-changeset-modify .ai-changeset-op{background:var(--md-primary-container);color:var(--md-on-primary-container)}
.ai-changeset-add .ai-changeset-op{background:var(--md-success-container);color:var(--md-success)}
.ai-changeset-delete .ai-changeset-op{background:var(--md-error-container);color:var(--md-on-error-container)}
.ai-changeset-label{font-weight:500;white-space:nowrap}
.ai-changeset-detail{min-width:0;color:var(--md-on-surface-variant);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

.ai-security-warn{display:flex;gap:10px;padding:10px 12px;border:1px solid color-mix(in srgb,var(--md-warn) 40%,var(--md-outline-variant));border-radius:var(--r-sm);background:color-mix(in srgb,var(--md-warn) 8%,var(--md-surface-c-low));font-size:13px;line-height:19px;margin-top:8px}
.ai-security-warn>.material-symbols-outlined{flex:none;font-size:18px;color:var(--md-warn);margin-top:2px}
.ai-security-warn b{display:block;font-weight:500;margin-bottom:4px}
.ai-security-warn ul{margin:0;padding-left:18px}
.ai-security-warn li{margin:2px 0;color:var(--md-on-surface-variant)}
.ai-security-warn small{display:block;margin-top:6px;color:var(--md-on-surface-variant);font-size:12px}

.ai-empty-enter{animation:ai-empty-arrive .3s cubic-bezier(.16,1,.3,1) both}
@keyframes ai-empty-arrive{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}

.ai-msg-enter-active{transition:opacity .2s ease-out,transform .22s cubic-bezier(.16,1,.3,1)}
.ai-msg-enter-from{opacity:0;transform:translateY(8px)}
.ai-msg-leave-active{transition:opacity .12s ease-in}
.ai-msg-leave-to{opacity:0}

.natural-draft-conversation{scroll-behavior:smooth}

@media (prefers-reduced-motion:reduce){
  .ai-changeset-item,.ai-empty-enter,.ai-msg-enter-active,.ai-msg-leave-active,.ai-msg-move,.ai-activity-chevron,.ai-activity-body,.ai-reasoning-chevron,.ai-reasoning-body,.ai-stream-cursor,.ai-activity-spin{transition:none;animation:none}
}
</style>
