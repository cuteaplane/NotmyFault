<script setup>
import { computed } from 'vue'

const props = defineProps({
  action: { type: Object, required: true },
  meta: { type: Object, default: () => ({}) },
})

const retryCount = computed(() => Math.min(Math.max(Number(props.action.retry || 0), 0), 3))
const retrySummary = computed(() => retryCount.value
  ? `最多重试 ${retryCount.value} 次`
  : '不重试')

function setOnError(event) {
  if (event.target.value === 'continue') props.action.on_error = 'continue'
  else delete props.action.on_error
}

function setRetry(event) {
  const count = Math.min(Math.max(Number(event.target.value || 0), 0), 3)
  if (!count) {
    delete props.action.retry
    delete props.action.retry_delay_seconds
    delete props.action.retry_backoff
    return
  }
  props.action.retry = count
  if (props.action.retry_delay_seconds === undefined) props.action.retry_delay_seconds = 2
}

function setDelay(event) {
  const value = Number(event.target.value)
  props.action.retry_delay_seconds = Number.isFinite(value)
    ? Math.min(Math.max(value, 0), 3600)
    : 0
}

function setBackoff(event) {
  if (event.target.value === 'exponential') props.action.retry_backoff = 'exponential'
  else delete props.action.retry_backoff
}
</script>

<template>
  <section class="action-failure-settings">
    <header class="action-failure-head">
      <span class="material-symbols-outlined">report</span>
      <div><b>这个动作失败以后</b><small>决定后面的动作是否继续，以及要不要再试</small></div>
    </header>
    <label class="field action-failure-policy">
      <span class="field-label">接下来怎么做</span>
      <select class="select" :value="action.on_error === 'continue' ? 'continue' : 'stop'" @change="setOnError">
        <option value="stop">停止，不再执行后面的动作</option>
        <option value="continue">记下失败，继续执行后面的动作</option>
      </select>
    </label>
    <details class="action-retry-settings" :open="retryCount > 0">
      <summary>
        <span><b>失败后再试</b><small>{{ retrySummary }}</small></span>
        <span class="material-symbols-outlined">expand_more</span>
      </summary>
      <div class="action-retry-body">
        <label class="field">
          <span class="field-label">最多再试几次</span>
          <select class="select" :value="retryCount" @change="setRetry">
            <option :value="0">不重试</option>
            <option :value="1">1 次</option>
            <option :value="2">2 次</option>
            <option :value="3">3 次</option>
          </select>
        </label>
        <template v-if="retryCount">
          <label class="field">
            <span class="field-label">每次重试前等待</span>
            <span class="action-delay-field"><input class="text-field" type="number" min="0" max="3600" step="0.5" :value="action.retry_delay_seconds ?? 2" @input="setDelay"><small>秒</small></span>
          </label>
          <label v-if="retryCount > 1" class="field">
            <span class="field-label">连续失败时</span>
            <select class="select" :value="action.retry_backoff === 'exponential' ? 'exponential' : 'fixed'" @change="setBackoff">
              <option value="fixed">每次等待相同时间</option>
              <option value="exponential">等待时间逐次加倍</option>
            </select>
          </label>
          <p v-if="meta.idempotent !== true" class="action-retry-warning"><span class="material-symbols-outlined">warning</span>这个动作没有声明可安全重复执行。重试可能重复发通知、写文件或启动程序。</p>
        </template>
      </div>
    </details>
  </section>
</template>
