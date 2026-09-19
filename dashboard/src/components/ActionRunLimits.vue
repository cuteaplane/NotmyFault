<script setup>
import { computed } from 'vue'
const props = defineProps({ action: { type: Object, required: true }, meta: { type: Object, default: () => ({}) } })
function retryCountFor(action) {
  return Math.min(Math.max(Number(action?.retry || 0), 0), 3)
}
const retryCount = computed(() => retryCountFor(props.action))
const retrySummary = computed(() => retryCount.value
  ? `最多重试 ${retryCount.value} 次`
  : '不重试')
const supportsCancellation = computed(() => props.meta.cancellation_api === 'runtime-v1')
const timeoutSummary = computed(() => props.action.timeout_seconds
  ? `超过 ${props.action.timeout_seconds} 秒就停止`
  : supportsCancellation.value ? '不限制运行时间' : '这个动作不能安全停止')

function setRetry(event, action = props.action) {
  const count = Math.min(Math.max(Number(event.target.value || 0), 0), 3)
  if (!count) {
    delete action.retry
    delete action.retry_delay_seconds
    delete action.retry_backoff
    return
  }
  action.retry = count
  if (action.retry_delay_seconds === undefined) action.retry_delay_seconds = 2
}

function setDelay(event, action = props.action) {
  if (event.target.value.trim() === '') { delete action.retry_delay_seconds; return }
  const value = Number(event.target.value)
  action.retry_delay_seconds = Number.isFinite(value)
    ? Math.min(Math.max(value, 0), 3600)
    : 0
}

function setBackoff(event, action = props.action) {
  if (event.target.value === 'exponential') action.retry_backoff = 'exponential'
  else delete action.retry_backoff
}

function setActionTimeout(event, action = props.action) {
  const value = Number(event.target.value)
  if (!Number.isFinite(value) || value <= 0) {
    delete action.timeout_seconds
    return
  }
  action.timeout_seconds = Math.min(Math.max(value, 1), 86400)
}

function clearActionTimeout(action = props.action) {
  delete action.timeout_seconds
}

</script>
<template>
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
    <details class="action-timeout-settings" :open="action.timeout_seconds != null">
      <summary>
        <span><b>最长运行时间</b><small>{{ timeoutSummary }}</small></span>
        <span class="material-symbols-outlined">expand_more</span>
      </summary>
      <div class="action-timeout-body">
        <label v-if="supportsCancellation" class="field">
          <span class="field-label">超过多少秒就停止</span>
          <span class="action-delay-field"><input class="text-field" type="number" min="1" max="86400" step="1" placeholder="不限制" :value="action.timeout_seconds ?? ''" @input="setActionTimeout"><small>秒</small></span>
        </label>
        <p v-else class="action-timeout-unavailable"><span class="material-symbols-outlined">info</span>插件没有提供安全停止能力，所以这里不能设置一个假的超时。动作自身仍可能有网络或子进程超时。<button v-if="action.timeout_seconds != null" class="btn btn-text btn-sm" type="button" @click="clearActionTimeout()">清除旧设置</button></p>
      </div>
    </details>
</template>
