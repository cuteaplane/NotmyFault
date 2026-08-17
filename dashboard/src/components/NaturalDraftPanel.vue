<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import MarkdownIt from 'markdown-it'
import { streamRuleDraftWithAI } from '../lib/api'
import { store } from '../lib/store'

const MAX_HISTORY_ITEMS = 40
const MAX_MESSAGE_CHARS = 4000
const SCROLL_FOLLOW_SLACK = 150

const emit = defineEmits(['create'])
const composer = ref('')
const drafting = ref(false)
const messages = ref([])
const pendingConsent = ref(null)
const conversationRef = ref(null)
const composerRef = ref(null)
const userScrolledUp = ref(false)
let messageId = 0
let activeController = null
let activeMessageId = null

const QUICK_CHIPS = [
  { label: '定时提醒', prompt: '每天早上 9 点提醒我处理邮件' },
  { label: 'USB 触发', prompt: '插入 USB 设备时自动备份桌面文件夹到 U 盘' },
  { label: '文件监听', prompt: '某个文件夹有新文件时给我发桌面通知' },
  { label: '定时脚本', prompt: '每周五 18:00 运行一个 Python 脚本' },
]

const showWelcome = computed(() => messages.value.length === 0)

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

function proposalId(result) {
  return clipContent(result?.proposal?.id || result?.plugin_id || result?.plugin?.id || result?.id)
}

function assistantMessage(result) {
  return clipContent(result?.message || result?.content || result?.error)
    || '我还需要一点信息，才能继续起草。'
}

function triggerName(result) {
  const triggerType = result?.draft?.event?.type
  return store.schema.triggers?.[triggerType]?.name || triggerType || '还没听清什么时候开始'
}

function actionNames(result) {
  return (result?.draft?.actions || []).map(action => (
    store.schema.actions?.[action.type]?.name || action.type
  )).filter(Boolean)
}

function validationIssues(result) {
  return result?.validation?.issues?.slice(0, 5) || []
}

function pluginManifest(result) {
  const manifest = result?.manifest ?? result?.plugin?.manifest ?? result?.plugin_source?.manifest
  if (typeof manifest === 'string') return manifest
  if (manifest && typeof manifest === 'object') return JSON.stringify(manifest, null, 2)
  return '{}'
}

function pluginSource(result) {
  const source = result?.source_code
    ?? result?.plugin?.source_code
    ?? result?.plugin?.source
    ?? result?.plugin_source?.source_code
    ?? result?.plugin_source?.source
    ?? result?.code
    ?? (result?.source === 'ai' ? '' : result?.source)
  return typeof source === 'string' ? source : ''
}

function assistantSummary(result) {
  if (result?.result_type === 'assistant_message') return assistantMessage(result)
  if (result?.result_type === 'rule_draft') {
    const actions = actionNames(result).join('、') || '待补充动作'
    return clipContent(`已生成规则草稿：当 ${triggerName(result)}，然后 ${actions}。可在编辑器检查。`)
  }
  if (result?.result_type === 'plugin_proposal') {
    const id = proposalId(result) || '未命名插件'
    const name = clipContent(result?.proposal?.name) || id
    return clipContent(`已提出插件能力提案：${name}（${id}）。等待是否同意生成插件草稿。`)
  }
  if (result?.result_type === 'plugin_source') {
    const id = proposalId(result) || '未命名插件'
    return `已生成插件草稿 ${id} 供人工审阅。源代码未保存、未签名、未安装、未运行。`
  }
  return assistantMessage(result)
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
    content: String(content ?? ''),
    result,
    transient: state.transient === true,
    streaming: state.streaming === true,
    stopped: false,
    reasoning: '',
    reasoningOpen: false,
    reasoningDone: false,
  }
  messages.value.push(message)
  await scrollConversation()
  return message
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
  message.transient = false
  message.streaming = false
  settleReasoning(message)
}

// 思考结束就收起推理区，让答案回到视线中心；用户想回看仍可自己展开。
function settleReasoning(message) {
  message.reasoningDone = !!message.reasoning
  if (message.reasoningDone) message.reasoningOpen = false
}

function finishError(message, text) {
  message.content = text
  message.result = { result_type: 'assistant_message', message: text }
  message.transient = false
  message.streaming = false
  settleReasoning(message)
}

function requestMessages() {
  return messages.value
    .filter(message => !message.transient && (message.role === 'user' || message.role === 'assistant'))
    .map(message => ({ role: message.role, content: clipContent(message.content) }))
    .filter(message => message.content)
    .slice(-MAX_HISTORY_ITEMS)
}

function handleStreamEvent(message, event, markDone) {
  if (event.type === 'status') return
  if (event.type === 'reasoning') {
    const delta = String(event.data?.delta ?? '')
    if (delta) {
      message.reasoning += delta
      message.reasoningOpen = true
      message.reasoningDone = false
      void scrollConversation()
    }
    return
  }
  if (event.type === 'text') {
    const delta = String(event.data?.delta ?? '')
    if (delta) {
      message.content += delta
      void scrollConversation()
    }
    return
  }
  if (event.type === 'result') {
    finishResult(message, event.data)
    void scrollConversation()
    markDone()
    return
  }
  if (event.type === 'error') {
    finishError(message, assistantMessage(event.data))
    void scrollConversation()
    markDone()
    return
  }
  if (event.type === 'done') markDone()
}

async function sendTurn(content, consent = null) {
  const text = clipContent(content)
  if (!text || drafting.value) return

  // 自己发言等于回到当下，重新贴住底部。
  userScrolledUp.value = false
  drafting.value = true
  await appendMessage('user', text)
  const message = await appendMessage('assistant', '', null, { transient: true, streaming: true })
  const controller = new AbortController()
  let receivedDone = false
  activeController = controller
  activeMessageId = message.id
  try {
    await streamRuleDraftWithAI(requestMessages(), {
      consent,
      apiKey: store.aiApiKey,
      signal: controller.signal,
      onEvent: event => {
        if (!controller.signal.aborted) handleStreamEvent(message, event, () => { receivedDone = true })
      },
    })
    if (!controller.signal.aborted && !receivedDone) {
      finishError(message, '连接已中断，请检查网络后重试。')
    } else if (!controller.signal.aborted && message.transient) {
      finishError(message, '草稿服务没有返回结果，请重试。')
    }
  } catch (reason) {
    if (!controller.signal.aborted) {
      const messageText = reason?.name === 'TypeError'
        ? '连接已中断，请检查网络后重试。'
        : reason?.message || '草稿服务暂时不可用。'
      finishError(message, messageText)
    }
  } finally {
    if (activeController === controller) {
      activeController = null
      activeMessageId = null
      drafting.value = false
    }
    if (!controller.signal.aborted) {
      message.streaming = false
      settleReasoning(message)
      await scrollConversation()
      await nextTick()
      composerRef.value?.focus()
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
    settleReasoning(message)
  }
  void nextTick(() => composerRef.value?.focus())
}

// 结束一轮探索后清空重来，避免历史干扰后续起草。
function clearConversation() {
  if (drafting.value) return
  if (!messages.value.length) return
  if (!window.confirm('清空当前对话？已生成的草稿和插件提案都会消失。')) return
  messages.value = []
  pendingConsent.value = null
  userScrolledUp.value = false
  void nextTick(() => composerRef.value?.focus())
}

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

// Auto-grow the textarea as the user types
function growTextarea() {
  const el = composerRef.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 200) + 'px'
}

// Enter submits, Shift+Enter inserts newline
function onComposerKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    sendMessage()
  }
}

async function approvePluginProposal(result) {
  const id = proposalId(result)
  if (!id || drafting.value) return
  pendingConsent.value = { plugin_id: id }
  try {
    await sendTurn(`我同意生成插件草稿：${id}`, pendingConsent.value)
  } finally {
    pendingConsent.value = null
  }
}

function openDraft(result) {
  if (result?.draft) emit('create', result.draft)
}

function downloadFile(filename, text) {
  const a = document.createElement('a')
  a.href = URL.createObjectURL(new Blob([text], { type: 'text/plain' }))
  a.download = filename
  a.click()
  URL.revokeObjectURL(a.href)
}

function downloadPluginDraft(result) {
  const manifest = result?.manifest ?? result?.plugin?.manifest ?? result?.plugin_source?.manifest
  const source = result?.source_code ?? result?.plugin?.source_code ?? result?.plugin?.source
    ?? result?.plugin_source?.source_code ?? result?.plugin_source?.source
    ?? result?.code ?? (result?.source === 'ai' ? '' : result?.source)
  const id = (manifest?.id || 'plugin').replace(/[^a-zA-Z0-9_-]/g, '_')
  if (manifest) downloadFile(`${id}.action.json`, JSON.stringify(manifest, null, 2))
  if (source) downloadFile(`${id}.action.py`, String(source))
}

onMounted(async () => {
  await nextTick()
  composerRef.value?.focus()
})
onUnmounted(() => { activeController?.abort() })
</script>

<template>
  <section class="ai-draft-panel flex h-full min-h-0 flex-col gap-3">

    <!-- Welcome / empty state -->
    <transition name="ai-welcome">
      <div v-if="showWelcome" class="ai-welcome-state flex min-h-0 flex-1 flex-col items-center justify-center gap-5 pb-4">
        <span class="material-symbols-outlined ai-welcome-icon">auto_awesome</span>
        <div class="text-center">
          <p class="text-title-m text-on-surface">用自然语言描述你想自动化的事</p>
          <p class="mt-1 text-body-s text-on-surface-variant">AI 会把它变成可直接送进编辑器的规则草稿，或提出缺少的插件能力提案。</p>
        </div>
        <div class="ai-quick-chips" role="list" aria-label="快速开始">
          <button
            v-for="chip in QUICK_CHIPS"
            :key="chip.label"
            type="button"
            class="ai-quick-chip"
            role="listitem"
            :disabled="drafting"
            @click="sendChip(chip.prompt)"
          >
            <span class="material-symbols-outlined">chevron_right</span>{{ chip.label }}
          </button>
        </div>
      </div>
    </transition>

    <!-- Conversation -->
    <div v-if="!showWelcome && messages.length > 0" class="ai-conversation-header">
      <button v-if="userScrolledUp" class="btn btn-text btn-sm" type="button" @click="jumpToLatest">
        <span class="material-symbols-outlined">arrow_downward</span>回到底部
      </button>
      <div class="flex-1"></div>
      <button class="btn btn-text btn-sm" type="button" :disabled="drafting" @click="clearConversation">
        <span class="material-symbols-outlined">refresh</span>新对话
      </button>
    </div>

    <div v-if="!showWelcome" ref="conversationRef"
      class="natural-draft-conversation flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto pr-1!"
      role="log" tabindex="0" :aria-busy="drafting" aria-live="polite" aria-label="AI 草稿对话"
      @scroll.passive="onConversationScroll">
      <article v-for="message in messages" :key="message.id" class="natural-draft-message min-w-0"
        :class="message.role === 'user' ? 'self-end max-w-4/5 rounded-md bg-primary-container px-3! py-2! text-body-m text-on-primary-container' : 'w-full'">
        <div v-if="message.role === 'assistant' && message.reasoning"
          class="natural-draft-reasoning mb-2 rounded-md bg-surface-c"
          :class="{ 'reasoning-open': message.reasoningOpen }">
          <button
            type="button"
            class="reasoning-toggle flex w-full cursor-pointer items-center gap-2 px-3! py-2! text-label-m text-on-surface-variant"
            :aria-expanded="String(message.reasoningOpen)"
            @click="message.reasoningOpen = !message.reasoningOpen"
          >
            <span class="material-symbols-outlined text-body-l">psychology</span>
            <span class="min-w-0 flex-1 text-left">{{ message.reasoningDone ? '思考过程' : '正在思考…' }}</span>
            <span class="material-symbols-outlined reasoning-chevron text-body-l">expand_more</span>
          </button>
          <div class="reasoning-body">
            <p class="px-3! pb-2! whitespace-pre-wrap break-words text-body-s text-on-surface-variant">{{ message.reasoning }}</p>
          </div>
        </div>
        <template v-if="message.role === 'user'">
          <b class="block text-label-s">你</b>
          <p class="mt-1! whitespace-pre-wrap break-words">{{ message.content }}</p>
        </template>

        <template v-else-if="message.result?.result_type === 'rule_draft'">
          <div class="natural-draft-result rounded-md bg-surface-c-low p-3!">
            <div class="natural-draft-result-head">
              <div><small>我理解的是</small><b>{{ message.result.draft ? '先按这个结构起草' : '还缺少关键信息' }}</b></div>
              <span class="chip">AI 候选草稿</span>
            </div>
            <div class="natural-draft-sentence">
              <span><i>当</i><b class="min-w-0 break-words overflow-visible! text-clip! whitespace-normal!">{{ triggerName(message.result) }}</b></span>
              <span class="material-symbols-outlined">arrow_forward</span>
              <span><i>然后</i><b class="min-w-0 break-words overflow-visible! text-clip! whitespace-normal!">{{ actionNames(message.result).length ? actionNames(message.result).join('、') : '还没听清要做什么' }}</b></span>
            </div>
            <div v-if="validationIssues(message.result).length" class="natural-draft-check">
              <b>规则检查还发现：</b>
              <ul>
                <li v-for="issue in validationIssues(message.result)" :key="`${issue.code}-${issue.message}`">
                  <span class="material-symbols-outlined">{{ issue.severity === 'warning' ? 'warning' : 'error' }}</span>{{ issue.message }}
                </li>
              </ul>
            </div>
            <footer>
              <small>这里不会替你做决定。所有参数仍会经过普通规则检查。</small>
              <button class="btn btn-tonal" type="button" :disabled="!message.result.draft" @click="openDraft(message.result)">
                进编辑器检查<span class="material-symbols-outlined">arrow_forward</span>
              </button>
            </footer>
          </div>
        </template>

        <template v-else-if="message.result?.result_type === 'plugin_proposal' && message.result.proposal">
          <div class="natural-draft-result rounded-md bg-surface-c-low p-3!">
            <div class="natural-draft-result-head">
              <div>
                <small>当前插件目录缺少所需能力</small>
                <h3><b>只读能力提案：{{ message.result.proposal.name }}</b></h3>
              </div>
              <span class="chip">未安装 · 仅供人工评审</span>
            </div>
            <ul class="natural-draft-notes" aria-label="能力提案基本信息">
              <li><span class="material-symbols-outlined">extension</span><span class="min-w-0 break-words"><b>类型：</b>{{ message.result.proposal.kind === 'trigger' ? '触发器' : '动作' }}</span></li>
              <li><span class="material-symbols-outlined">fingerprint</span><span class="min-w-0 break-all"><b>插件 ID：</b>{{ proposalId(message.result) }}</span></li>
              <li v-if="message.result.proposal.description"><span class="material-symbols-outlined">description</span><span class="min-w-0 break-words"><b>说明：</b>{{ message.result.proposal.description }}</span></li>
            </ul>
            <div v-if="message.result.proposal.permissions?.length" class="natural-draft-missing" aria-label="已知权限">
              <b>已知权限：</b>
              <span v-for="permission in message.result.proposal.permissions" :key="permission" class="break-all">{{ permission }}</span>
            </div>
            <section v-if="message.result.proposal.parameters?.length" class="natural-draft-check">
              <h4>参数：</h4>
              <ul>
                <li v-for="parameter in message.result.proposal.parameters" :key="parameter.name">
                  <span class="material-symbols-outlined">tune</span><span class="min-w-0 break-words"><b>{{ parameter.label || parameter.name }}</b>（{{ parameter.type }}）</span>
                </li>
              </ul>
            </section>
            <section v-if="message.result.proposal.outputs?.length" class="natural-draft-check">
              <h4>输出：</h4>
              <ul>
                <li v-for="output in message.result.proposal.outputs" :key="output.name">
                  <span class="material-symbols-outlined">output</span><span class="min-w-0 break-words"><b>{{ output.label || output.name }}</b>（{{ output.type }}）</span>
                </li>
              </ul>
            </section>
            <section v-if="message.result.proposal.rationale" class="natural-draft-check">
              <h4>提出理由：</h4>
              <ul><li><span class="material-symbols-outlined">info</span><span class="min-w-0 break-words">{{ message.result.proposal.rationale }}</span></li></ul>
            </section>
            <section v-if="message.result.proposal.acceptance_criteria?.length" class="natural-draft-check">
              <h4>验收标准：</h4>
              <ul>
                <li v-for="criterion in message.result.proposal.acceptance_criteria" :key="criterion">
                  <span class="material-symbols-outlined">check_circle</span><span class="min-w-0 break-words">{{ criterion }}</span>
                </li>
              </ul>
            </section>
            <footer>
              <small>这只是能力提案：未安装、未签名、未保存，也不会运行；必须先经人工评审。</small>
              <button class="btn btn-tonal" type="button" :disabled="drafting || !proposalId(message.result)"
                @click="approvePluginProposal(message.result)">
                同意生成插件草稿<span class="material-symbols-outlined">arrow_forward</span>
              </button>
            </footer>
          </div>
        </template>

        <template v-else-if="message.result?.result_type === 'plugin_source'">
          <div class="natural-draft-result natural-draft-source rounded-md bg-surface-c-low p-3!">
            <div class="natural-draft-result-head">
              <div>
                <small>仅供人工审阅</small>
                <b>插件草稿：{{ proposalId(message.result) || '未命名插件' }}</b>
              </div>
              <span class="chip break-words">未保存、未签名、未安装、未运行</span>
            </div>
            <section class="mt-3! grid gap-2">
              <h4 class="text-label-m text-on-surface-variant">插件清单</h4>
              <pre tabindex="0" class="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md bg-surface-c-lowest p-3! font-mono text-body-s text-on-surface">{{ pluginManifest(message.result) }}</pre>
            </section>
            <section class="mt-3! grid gap-2">
              <h4 class="text-label-m text-on-surface-variant">插件源码</h4>
              <pre tabindex="0" class="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md bg-surface-c-lowest p-3! font-mono text-body-s text-on-surface">{{ pluginSource(message.result) }}</pre>
            </section>
            <footer class="mt-3! flex items-center justify-between gap-3">
              <small class="text-body-s text-on-surface-variant">未保存、未签名、未安装——需人工审阅后自行打包安装。</small>
              <button class="btn btn-tonal" type="button" @click="downloadPluginDraft(message.result)">
                <span class="material-symbols-outlined">download</span>下载草稿文件
              </button>
            </footer>
          </div>
        </template>

        <template v-else>
          <div class="ai-msg-bubble rounded-md bg-surface-c-low px-3! py-2! text-body-m text-on-surface">
            <b class="block text-label-s text-primary">AI</b>
            <!-- Waiting for first token -->
            <div v-if="message.streaming && !message.content" class="ai-typing-dots mt-2!" aria-label="AI 正在思考">
              <span></span><span></span><span></span>
            </div>
            <!-- Streaming: plain text + blinking cursor -->
            <div v-else-if="message.streaming && message.content"
              class="mt-1! whitespace-pre-wrap break-words text-body-m">{{ message.content }}<span class="ai-stream-cursor" aria-hidden="true"></span></div>
            <!-- Finished: rendered markdown -->
            <div v-else-if="message.content"
              class="natural-draft-markdown mt-1! break-words"
              v-html="renderMarkdown(message.content)"></div>
            <p v-if="message.stopped" class="mt-2! flex items-center gap-1 text-body-s text-on-surface-variant">
              <span class="material-symbols-outlined" style="font-size:15px">stop_circle</span>已停止
            </p>
          </div>
        </template>
      </article>
    </div>

    <form class="natural-draft-form mt-0! flex shrink-0 flex-col gap-2" @submit.prevent="sendMessage">
      <label for="natural-draft-description" class="sr-only">你想自动化什么</label>
      <div class="natural-draft-entry">
        <textarea
          id="natural-draft-description"
          ref="composerRef"
          v-model="composer"
          class="text-field textarea-field natural-draft-textarea"
          :maxlength="MAX_MESSAGE_CHARS"
          rows="1"
          placeholder="描述你想自动化的事，Enter 发送，Shift+Enter 换行"
          :disabled="drafting"
          @input="growTextarea"
          @keydown="onComposerKeydown"
        ></textarea>
        <div class="natural-draft-actions">
          <button v-if="drafting" class="btn btn-outlined natural-draft-stop w-full" type="button" @click="stopDrafting">
            <span class="material-symbols-outlined">stop_circle</span>停止
          </button>
          <button v-else class="btn btn-filled w-full" type="submit" :disabled="!composer.trim()">
            <span class="material-symbols-outlined">send</span>发送
          </button>
        </div>
      </div>
      <p class="text-body-s text-on-surface-variant" style="padding-left:2px">
        <span class="material-symbols-outlined" style="font-size:13px;vertical-align:middle">lock</span>
        消息仅发往你配置的 AI 服务，不经过 NotmyFault 服务器。
      </p>

    </form>

  </section>
</template>
