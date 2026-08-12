<script setup>
import { computed } from 'vue'
import { getVisibleParamDefs } from '../lib/utils'
import ParamInput from './ParamInput.vue'

const props = defineProps({
  action: { type: Object, required: true },
  meta: { type: Object, default: () => ({}) },
  schema: { type: Object, default: () => ({}) },
  bindingSources: { type: Function, default: () => [] },
})
const emit = defineEmits(['add-failure-action', 'replace-failure-action', 'remove-failure-action', 'move-failure-action'])

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

function setOnError(event, action = props.action) {
  if (event.target.value === 'continue') action.on_error = 'continue'
  else delete action.on_error
}

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
  const value = Number(event.target.value)
  action.retry_delay_seconds = Number.isFinite(value)
    ? Math.min(Math.max(value, 0), 3600)
    : 0
}

function setBackoff(event, action = props.action) {
  if (event.target.value === 'exponential') action.retry_backoff = 'exponential'
  else delete action.retry_backoff
}

function setTimeout(event, action = props.action) {
  const value = Number(event.target.value)
  if (!Number.isFinite(value) || value <= 0) {
    delete action.timeout_seconds
    return
  }
  action.timeout_seconds = Math.min(Math.max(value, 1), 86400)
}

function actionName(action) {
  return props.schema[action?.type]?.name || action?.type || '未选择动作'
}

function actionParams(action) {
  return getVisibleParamDefs(props.schema[action?.type], action?.params)
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
    <details class="action-timeout-settings" :open="action.timeout_seconds != null">
      <summary>
        <span><b>最长运行时间</b><small>{{ timeoutSummary }}</small></span>
        <span class="material-symbols-outlined">expand_more</span>
      </summary>
      <div class="action-timeout-body">
        <label v-if="supportsCancellation" class="field">
          <span class="field-label">超过多少秒就停止</span>
          <span class="action-delay-field"><input class="text-field" type="number" min="1" max="86400" step="1" placeholder="不限制" :value="action.timeout_seconds ?? ''" @input="setTimeout"><small>秒</small></span>
        </label>
        <p v-else class="action-timeout-unavailable"><span class="material-symbols-outlined">info</span>插件没有提供安全停止能力，所以这里不能设置一个假的超时。动作自身仍可能有网络或子进程超时。</p>
      </div>
    </details>
    <details class="failure-actions-settings" :open="action.failure_actions?.length > 0">
      <summary>
        <span><b>失败时先做这些事</b><small>{{ action.failure_actions?.length ? `${action.failure_actions.length} 个补救动作` : '没有补救动作' }}</small></span>
        <span class="material-symbols-outlined">expand_more</span>
      </summary>
      <div class="failure-actions-body">
        <p class="failure-actions-help">只有这个动作最终失败时才会执行。补救完成后，再按上面的选择停止或继续主流程。</p>
        <article v-for="(failureAction, index) in action.failure_actions || []" :key="failureAction.binding_id || index" class="failure-action-card">
          <header>
            <span class="failure-action-index">{{ index + 1 }}</span>
            <b>{{ actionName(failureAction) }}</b>
            <span class="failure-action-tools">
              <button class="icon-btn" :disabled="index === 0" title="上移" @click="emit('move-failure-action', index, -1)"><span class="material-symbols-outlined">arrow_upward</span></button>
              <button class="icon-btn" :disabled="index === action.failure_actions.length - 1" title="下移" @click="emit('move-failure-action', index, 1)"><span class="material-symbols-outlined">arrow_downward</span></button>
              <button class="icon-btn icon-btn-danger" title="删除补救动作" @click="emit('remove-failure-action', index)"><span class="material-symbols-outlined">delete</span></button>
            </span>
          </header>
          <button class="plugin-type-button" type="button" @click="emit('replace-failure-action', index)">
            <span class="material-symbols-outlined">build</span>
            <span>{{ actionName(failureAction) }}</span>
            <span class="material-symbols-outlined">arrow_forward</span>
          </button>
          <div class="param-grid">
            <ParamInput v-for="param in actionParams(failureAction)" :key="param.name" :def="param"
              :plugin-id="failureAction.type" v-model="failureAction.params[param.name]" allow-binding :binding-sources="bindingSources(index)" />
          </div>
          <div class="failure-action-policy-grid">
            <label class="field"><span class="field-label">这个补救动作也失败时</span>
              <select class="select" :value="failureAction.on_error === 'continue' ? 'continue' : 'stop'" @change="setOnError($event, failureAction)">
                <option value="stop">停止剩余补救动作</option>
                <option value="continue">继续执行剩余补救动作</option>
              </select>
            </label>
            <label class="field"><span class="field-label">最多再试几次</span>
              <select class="select" :value="retryCountFor(failureAction)" @change="setRetry($event, failureAction)">
                <option :value="0">不重试</option><option :value="1">1 次</option><option :value="2">2 次</option><option :value="3">3 次</option>
              </select>
            </label>
            <label v-if="retryCountFor(failureAction)" class="field"><span class="field-label">每次重试前等待</span>
              <span class="action-delay-field"><input class="text-field" type="number" min="0" max="3600" step="0.5" :value="failureAction.retry_delay_seconds ?? 2" @input="setDelay($event, failureAction)"><small>秒</small></span>
            </label>
            <label v-if="retryCountFor(failureAction) > 1" class="field"><span class="field-label">连续失败时</span>
              <select class="select" :value="failureAction.retry_backoff === 'exponential' ? 'exponential' : 'fixed'" @change="setBackoff($event, failureAction)">
                <option value="fixed">每次等待相同时间</option><option value="exponential">等待时间逐次加倍</option>
              </select>
            </label>
            <label v-if="schema[failureAction.type]?.cancellation_api === 'runtime-v1'" class="field"><span class="field-label">超过多少秒就停止</span>
              <span class="action-delay-field"><input class="text-field" type="number" min="1" max="86400" step="1" placeholder="不限制" :value="failureAction.timeout_seconds ?? ''" @input="setTimeout($event, failureAction)"><small>秒</small></span>
            </label>
          </div>
          <p v-if="retryCountFor(failureAction) && schema[failureAction.type]?.idempotent !== true" class="action-retry-warning"><span class="material-symbols-outlined">warning</span>这个补救动作没有声明可安全重复执行。重试可能重复产生结果。</p>
        </article>
        <button class="btn btn-tonal btn-sm failure-action-add" type="button" @click="emit('add-failure-action')"><span class="material-symbols-outlined">add</span>添加补救动作</button>
      </div>
    </details>
  </section>
</template>
