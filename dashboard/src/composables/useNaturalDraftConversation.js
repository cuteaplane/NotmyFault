import { nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { streamRuleDraftWithAI } from '../lib/api'
import { confirmDialog } from '../lib/dialog'
import { store } from '../lib/store'
import { clearSavedConversation, loadConversation, MAX_HISTORY_ITEMS, saveConversation } from '../lib/naturalDraftHistory'
import { normalizeDraftResult, ruleSummary } from '../lib/naturalDraftRules'

export const MAX_MESSAGE_CHARS = 4000
const SAVE_CONVERSATION_DELAY_MS = 500

export function clipContent(value) {
  return String(value ?? '').trim().slice(0, MAX_MESSAGE_CHARS)
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

export function useNaturalDraftConversation({ scrollConversation, focusComposer, followLatest }) {
  const messages = ref([])
  const drafting = ref(false)
  let messageId = 0
  let activeController = null
  let activeMessageId = null
  let saveConversationTimer = 0

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
    // 要拿到 messages 里的代理对象，后续改动才会触发更新，这里返回 messages 数组里的代理对象。
    return messages.value[messages.value.length - 1]
  }

  function messageById(id) {
    return messages.value.find(message => message.id === id) || null
  }

  function finishResult(message, result) {
    result = normalizeDraftResult(result)
    const isAssistantMessage = result?.result_type === 'assistant_message'
    message.content = isAssistantMessage && message.content.trim()
      ? message.content
      : isAssistantMessage
        ? assistantMessage(result)
        : ruleSummary(result, store.schema)
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

  function scheduleSaveConversation() {
    window.clearTimeout(saveConversationTimer)
    saveConversationTimer = window.setTimeout(() => saveConversation(messages.value), SAVE_CONVERSATION_DELAY_MS)
  }

  function flushSaveConversation() {
    window.clearTimeout(saveConversationTimer)
    saveConversationTimer = 0
    saveConversation(messages.value)
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

    followLatest()
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
        focusComposer()
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
    void nextTick(() => focusComposer())
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

  async function clearConversation() {
    if (drafting.value) return
    if (!messages.value.length) return
    if (!await confirmDialog('清空当前对话？', '已生成的对话和规则草稿都会消失。', '清空')) return
    messages.value = []
    clearSavedConversation()
    followLatest()
    void nextTick(() => focusComposer())
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

  watch(messages, scheduleSaveConversation, { deep: true })
  onMounted(() => {
    messages.value = loadConversation()
    messageId = Math.max(...messages.value.map(message => message.id || 0), 0)
  })
  onUnmounted(() => {
    activeController?.abort()
    flushSaveConversation()
  })

  return {
    messages, drafting, sendTurn, stopDrafting, retryFailedMessage,
    isLatestMessage, requestNewConversation, downloadConversation,
  }
}
