<script setup>
import MarkdownIt from 'markdown-it'
import NaturalDraftRuleCard from './NaturalDraftRuleCard.vue'

defineProps({
  message: { type: Object, required: true },
  drafting: { type: Boolean, default: false },
  latest: { type: Boolean, default: false },
  fullRule: { type: Object, default: null },
})
const emit = defineEmits(['retry', 'create', 'highlight-node'])

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

function renderMarkdown(content) {
  return markdown.render(String(content ?? ''))
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

function activityHeadline(message) {
  const activity = message.activity
  if (!activity) return ''
  if (activity.failed) return '已中断'
  if (activity.finished) return `已完成 · ${activity.steps.length} 步`
  const running = activity.steps.find(step => step.state === 'running')
  return running ? running.label : '正在处理…'
}
</script>

<template>
<article class="ai-message" :class="message.role">
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
        :disabled="drafting || !latest" @click="emit('retry', message)">
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

      <NaturalDraftRuleCard v-if="message.result?.result_type === 'rule_draft'"
        :message="message" :full-rule="fullRule"
        @create="emit('create', $event)" @highlight-node="emit('highlight-node', $event)" />

      <div v-else-if="message.content" class="ai-content">
        <div v-if="message.streaming" class="ai-stream-text">{{ message.content }}<span class="ai-stream-cursor" aria-hidden="true"></span></div>
        <div v-else class="natural-draft-markdown" v-html="renderMarkdown(message.content)"></div>
      </div>

      <div v-if="message.stopped" class="ai-stopped"><span class="material-symbols-outlined">stop_circle</span>已停止</div>
    </template>
  </template>
</article>
</template>

<style scoped>
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
.ai-error{display:flex;flex-direction:column;gap:8px;width:100%;min-width:0;max-width:100%;padding:12px 14px;border:1px solid color-mix(in srgb,var(--md-error) 35%,transparent);border-left:3px solid var(--md-error);border-radius:var(--r-sm);background:var(--md-error-container);color:var(--md-on-error-container)}
.ai-error-head{display:flex;align-items:center;gap:6px;font:var(--ts-label-m)}
.ai-error-head .material-symbols-outlined{flex:none;font-size:16px}
.ai-error p{margin:0;font-size:13px;line-height:19px;white-space:pre-line;overflow-wrap:anywhere}
.ai-error-retry{display:inline-flex;align-items:center;gap:5px;align-self:flex-start;height:32px;padding:0 14px;border:none;border-radius:var(--r-full);background:color-mix(in srgb,var(--md-on-error-container) 12%,transparent);color:var(--md-on-error-container);font:var(--ts-label-m);cursor:pointer}
.ai-error-retry:disabled{opacity:.5;cursor:default}
.ai-error-retry .material-symbols-outlined{flex:none;font-size:15px}
.ai-stopped{display:flex;align-items:center;gap:5px;font:var(--ts-label-m);color:var(--md-on-surface-variant)}
.ai-stopped .material-symbols-outlined{flex:none;font-size:14px}
@media (prefers-reduced-motion:reduce){.ai-activity-chevron,.ai-activity-body,.ai-reasoning-chevron,.ai-reasoning-body,.ai-stream-cursor,.ai-activity-spin{transition:none;animation:none}}
</style>
