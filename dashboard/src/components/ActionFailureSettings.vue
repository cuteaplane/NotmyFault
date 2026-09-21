<script setup>
import { watch } from 'vue'
import { ensureParams, getVisibleParamDefs } from '../lib/utils'
import { parameterAllowsBinding } from '../lib/bindings'
import ActionRunLimits from './ActionRunLimits.vue'
import ParamInput from './ParamInput.vue'

const props = defineProps({
  action: { type: Object, required: true },
  meta: { type: Object, default: () => ({}) },
  schema: { type: Object, default: () => ({}) },
  bindingSources: { type: Function, default: () => [] },
})
const emit = defineEmits(['add-failure-action', 'replace-failure-action', 'remove-failure-action', 'move-failure-action'])

watch(() => props.action.failure_actions, actions => actions?.forEach(ensureParams), { immediate: true, deep: true })
function setOnError(event, action = props.action) {
  if (event.target.value === 'continue') action.on_error = 'continue'
  else delete action.on_error
}

function actionName(action) {
  return props.schema[action?.type]?.name || action?.type || '未选择动作'
}

function actionParams(action) {
  return getVisibleParamDefs(props.schema[action?.type], action.params || {})
}

function actionParamAllowsBinding(action, param) {
  return parameterAllowsBinding(props.schema[action?.type], param.name)
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
    <ActionRunLimits :action="action" :meta="meta" />
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
              :plugin-id="failureAction.type" v-model="failureAction.params[param.name]"
              :allow-binding="actionParamAllowsBinding(failureAction, param)" :binding-sources="bindingSources(index)" />
          </div>
          <div class="failure-action-policy-grid">
            <label class="field"><span class="field-label">这个补救动作也失败时</span>
              <select class="select" :value="failureAction.on_error === 'continue' ? 'continue' : 'stop'" @change="setOnError($event, failureAction)">
                <option value="stop">停止剩余补救动作</option>
                <option value="continue">继续执行剩余补救动作</option>
              </select>
            </label>
          </div>
          <ActionRunLimits :action="failureAction" :meta="schema[failureAction.type]" />
        </article>
        <button class="btn btn-tonal btn-sm failure-action-add" type="button" @click="emit('add-failure-action')"><span class="material-symbols-outlined">add</span>添加补救动作</button>
      </div>
    </details>
  </section>
</template>
