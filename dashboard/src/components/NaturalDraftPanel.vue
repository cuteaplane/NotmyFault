<script setup>
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
import MarkdownIt from 'markdown-it'
import { streamRuleDraftWithAI } from '../lib/api'
import { store } from '../lib/store'

const MAX_HISTORY_ITEMS = 40
const MAX_MESSAGE_CHARS = 4000

const emit = defineEmits(['create'])
const composer = ref('')
const drafting = ref(false)
const messages = ref([])
const pendingConsent = ref(null)
const conversationRef = ref(null)
const composerRef = ref(null)
const streamNotice = ref('')
let messageId = 0
let activeController = null
let activeMessageId = null

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
  if (conversationRef.value) conversationRef.value.scrollTop = conversationRef.value.scrollHeight
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
    status: '',
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
  message.reasoningDone = !!message.reasoning
}

function finishError(message, text) {
  message.content = text
  message.result = { result_type: 'assistant_message', message: text }
  message.transient = false
  message.streaming = false
  message.reasoningDone = !!message.reasoning
}

function requestMessages() {
  return messages.value
    .filter(message => !message.transient && (message.role === 'user' || message.role === 'assistant'))
    .map(message => ({ role: message.role, content: clipContent(message.content) }))
    .filter(message => message.content)
    .slice(-MAX_HISTORY_ITEMS)
}

function handleStreamEvent(message, event, markDone) {
  if (event.type === 'status') {
    message.status = event.data?.status === 'started' ? '正在准备回复' : ''
    return
  }
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
      message.status = '正在回复'
      void scrollConversation()
    }
    return
  }
  if (event.type === 'result') {
    finishResult(message, event.data)
    void scrollConversation()
    return
  }
  if (event.type === 'error') {
    finishError(message, assistantMessage(event.data))
    void scrollConversation()
    return
  }
  if (event.type === 'done') markDone()
}

async function sendTurn(content, consent = null) {
  const text = clipContent(content)
  if (!text || drafting.value) return

  streamNotice.value = ''
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
      message.reasoningDone = !!message.reasoning
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
    message.reasoningDone = !!message.reasoning
  }
  void nextTick(() => composerRef.value?.focus())
}

async function sendMessage() {
  const text = clipContent(composer.value)
  if (!text) return
  composer.value = ''
  await sendTurn(text)
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

    <div v-if="messages.length" ref="conversationRef"
      class="natural-draft-conversation flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto pr-1!"
      role="log" tabindex="0" :aria-busy="drafting" aria-live="polite" aria-label="AI 草稿对话">
      <article v-for="message in messages" :key="message.id" class="natural-draft-message min-w-0"
        :class="message.role === 'user' ? 'self-end max-w-4/5 rounded-md bg-primary-container px-3! py-2! text-body-m text-on-primary-container' : 'w-full'">
        <details v-if="message.role === 'assistant' && message.reasoning" class="natural-draft-reasoning mb-2 rounded-md bg-surface-c px-3! py-2!"
          :open="message.reasoningOpen" @toggle="message.reasoningOpen = $event.currentTarget.open">
          <summary class="flex cursor-pointer list-none items-center gap-2 text-label-m text-on-surface-variant">
            <span class="material-symbols-outlined text-body-l">psychology</span>
            <span class="min-w-0 flex-1">{{ message.reasoningDone ? '思考已结束' : '正在思考…' }}</span>
            <span class="material-symbols-outlined text-body-l">{{ message.reasoningOpen ? 'expand_less' : 'expand_more' }}</span>
          </summary>
          <p class="mt-2! whitespace-pre-wrap break-words text-body-s text-on-surface-variant">{{ message.reasoning }}</p>
        </details>
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
          <div class="rounded-md bg-surface-c-low px-3! py-2! text-body-m text-on-surface">
            <b class="block text-label-s text-primary">AI</b>
            <div v-if="message.content" class="natural-draft-markdown mt-1! break-words" v-html="renderMarkdown(message.content)"></div>
            <p v-else class="mt-1! text-body-s text-on-surface-variant">{{ message.status || '正在等待 AI 回复…' }}</p>
            <p v-if="message.stopped" class="mt-1! text-body-s text-on-surface-variant">已停止</p>
          </div>
        </template>
      </article>
    </div>

    <form class="natural-draft-form mt-0! flex shrink-0 flex-col gap-2" @submit.prevent="sendMessage">
      <label for="natural-draft-description" class="sr-only">你想自动化什么</label>
      <div class="natural-draft-entry grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto]">
        <textarea id="natural-draft-description" ref="composerRef" v-model="composer" class="text-field textarea-field"
          :maxlength="MAX_MESSAGE_CHARS" rows="2" placeholder="例如：每天 08:30 提醒我提交月报，USB 插入时自动备份文件"
          :disabled="drafting"></textarea>
        <button v-if="drafting" class="natural-draft-stop btn btn-outlined" type="button" @click="stopDrafting">
          <span class="material-symbols-outlined">stop_circle</span>停止
        </button>
        <button v-else class="btn btn-filled" type="submit" :disabled="!composer.trim()">
          <span class="material-symbols-outlined">send</span>发送
        </button>
      </div>
      <p v-if="streamNotice" class="natural-draft-status text-body-s text-on-surface-variant" aria-live="polite">{{ streamNotice }}</p>

    </form>
  </section>
</template>
