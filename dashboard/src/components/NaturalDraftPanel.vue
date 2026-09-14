<script setup>
import { computed, nextTick, onMounted, ref } from 'vue'
import NaturalDraftMessage from './NaturalDraftMessage.vue'
import { clipContent, MAX_MESSAGE_CHARS, useNaturalDraftConversation } from '../composables/useNaturalDraftConversation'

const SCROLL_FOLLOW_SLACK = 150
const COMPOSER_MAX_HEIGHT = 120
const props = defineProps({
  contextRule: { type: Object, default: null },
  fullRule: { type: Object, default: null },
})
const emit = defineEmits(['create', 'highlight-node'])
const composer = ref('')
const conversationRef = ref(null)
const composerRef = ref(null)
const userScrolledUp = ref(false)
const {
  messages, drafting, sendTurn, stopDrafting, retryFailedMessage,
  isLatestMessage, requestNewConversation, downloadConversation,
} = useNaturalDraftConversation({
  scrollConversation,
  focusComposer: () => composerRef.value?.focus({ preventScroll: true }),
  followLatest: () => { userScrolledUp.value = false },
})
const showWelcome = computed(() => messages.value.length === 0)
const contextChipLabel = computed(() => {
  const rule = props.contextRule
  if (!rule) return '当前规则'
  const name = String(rule.name || '未命名规则').trim() || '未命名规则'
  return `当前规则 · ${name} · ${rule.nodes} 节点`
})

defineExpose({ requestNewConversation, downloadConversation })

async function scrollConversation() {
  await nextTick()
  const container = conversationRef.value
  if (!container || userScrolledUp.value) return
  container.scrollTop = container.scrollHeight
}

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

onMounted(async () => {
  await nextTick()
  composerRef.value?.focus({ preventScroll: true })
  void scrollConversation()
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
          <NaturalDraftMessage v-for="message in messages" :key="message.id"
            :message="message" :drafting="drafting" :latest="isLatestMessage(message)" :full-rule="fullRule"
            @retry="retryFailedMessage" @create="emit('create', $event)" @highlight-node="emit('highlight-node', $event)" />
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
.ai-draft-panel{display:flex;flex:1 1 0%;flex-direction:column;height:100%;min-height:0;min-width:0;max-width:100%}
.natural-draft-conversation{flex:1;min-height:0;overflow-y:auto;padding:16px}
.ai-turns{display:flex;flex-direction:column;gap:22px}
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
.ai-empty-enter{animation:ai-empty-arrive .3s cubic-bezier(.16,1,.3,1) both}
@keyframes ai-empty-arrive{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
.ai-msg-enter-active{transition:opacity .2s ease-out,transform .22s cubic-bezier(.16,1,.3,1)}
.ai-msg-enter-from{opacity:0;transform:translateY(8px)}
.ai-msg-leave-active{transition:opacity .12s ease-in}
.ai-msg-leave-to{opacity:0}
.natural-draft-conversation{scroll-behavior:smooth}
@media (prefers-reduced-motion:reduce){.ai-empty-enter,.ai-msg-enter-active,.ai-msg-leave-active,.ai-msg-move{transition:none;animation:none}}
</style>
