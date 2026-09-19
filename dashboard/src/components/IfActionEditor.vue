<script setup>
import { computed, ref } from 'vue'
import { store } from '../lib/store'
import { createBindingId, actionOutputDefs, expandBindingSources } from '../lib/bindings'
import { fieldType } from '../lib/valueTypes'
import { buildDefaultParams, groupActionKeys, pluginUnavailableReason } from '../lib/utils'
import PredicateEditor from './PredicateEditor.vue'
import ActionForm from './ActionForm.vue'
import PluginPicker from './PluginPicker.vue'

defineOptions({ name: 'IfActionEditor' })
const props = defineProps({ action: Object, rule: { type: Object, default: () => ({}) }, sources: { type: Array, default: () => [] } })
const actionKeys = computed(() => Object.keys(store.schema.actions).filter(key => !pluginUnavailableReason(store.schema.actions[key])))
const groups = computed(() => groupActionKeys(actionKeys.value))
const pickerOpen = ref(false)
let chooseAction = null
function pick(callback) { chooseAction = callback; pickerOpen.value = true }
function select(type) { const callback = chooseAction; chooseAction = null; pickerOpen.value = false; callback?.(type) }
function newAction(type) {
  return { binding_id: createBindingId('action'), type, params: buildDefaultParams(store.schema.actions[type]) }
}
function addIf(branch) {
  props.action[branch].push({ binding_id: createBindingId('action'), type: 'if', condition: { op: 'is_true', left: false }, then: [], else: [] })
}
function branchSources(branch, index) {
  const result = [...props.sources]
  props.action[branch].slice(0, index).forEach(item => {
    if (item.type === 'if') return
    actionOutputDefs(item, store.schema, props.rule).forEach(output => result.push({
      group: '当前分支的前序动作', label: `${store.schema.actions[item.type]?.name || item.type} · ${output.label || output.name}`,
      type: fieldType(output), optional: output.required === false, sensitive: output.sensitive, value: { $ref: { scope: 'step', node: item.binding_id, path: [output.name] } },
    }))
  })
  return expandBindingSources(result, store.schema.data_types?.custom)
}
function failureSources(branch, index, failureIndex) {
  const result = branchSources(branch, index)
  const failures = props.action[branch][index].failure_actions || []
  failures.slice(0, failureIndex).forEach(item => actionOutputDefs(item, store.schema, props.rule).forEach(output => result.push({
    group: '之前的补救动作', label: `${item.type} · ${output.label || output.name}`, type: fieldType(output), optional: output.required === false, sensitive: output.sensitive,
    value: { $ref: { scope: 'step', node: item.binding_id, path: [output.name] } },
  })))
  return result
}
function move(items, index, offset) {
  if (index + offset < 0 || index + offset >= items.length) return
  const [item] = items.splice(index, 1)
  items.splice(index + offset, 0, item)
}
function replace(item, type) {
  item.type = type
  item.params = buildDefaultParams(store.schema.actions[type])
  if (store.schema.actions[type]?.cancellation_api !== 'runtime-v1') delete item.timeout_seconds
}
</script>

<template>
  <div class="if-action-editor">
    <PredicateEditor :node="action.condition" :sources="sources" />
    <section v-for="branch in ['then', 'else']" :key="branch" class="if-branch">
      <h3>{{ branch === 'then' ? '成立时 · THEN' : '否则 · ELSE' }}</h3>
      <p v-if="!action[branch]?.length" class="inspector-lead">此分支为空，继续后续动作。</p>
      <article v-for="(item, index) in action[branch]" :key="item.binding_id" class="if-branch-action">
        <header><b>{{ index + 1 }}. {{ item.type === 'if' ? 'IF 条件分支' : item.type === 'set_variable' ? '变量赋值' : store.schema.actions[item.type]?.name || item.type }}</b>
          <button class="icon-btn" title="上移" :disabled="index === 0" @click="move(action[branch], index, -1)"><span class="material-symbols-outlined">arrow_upward</span></button>
          <button class="icon-btn" title="下移" :disabled="index === action[branch].length - 1" @click="move(action[branch], index, 1)"><span class="material-symbols-outlined">arrow_downward</span></button>
          <button class="icon-btn danger-text" title="移除分支动作" @click="action[branch].splice(index, 1)"><span class="material-symbols-outlined">delete</span></button>
        </header>
        <ActionForm :action="item" :rule="rule" :index="index" :binding-sources="branchSources(branch, index)" :failure-binding-sources="failureIndex => failureSources(branch, index, failureIndex)"
          @replace="pick(type => replace(item, type))"
          @add-failure-action="pick(type => (item.failure_actions ||= []).push(newAction(type)))"
          @replace-failure-action="failureIndex => pick(type => replace(item.failure_actions[failureIndex], type))"
          @remove-failure-action="failureIndex => { item.failure_actions.splice(failureIndex, 1); if (!item.failure_actions.length) delete item.failure_actions }"
          @move-failure-action="(failureIndex, offset) => move(item.failure_actions, failureIndex, offset)" />
      </article>
      <div class="flow-add-row">
        <button class="btn btn-tonal btn-sm" @click="pick(type => (action[branch] ||= []).push(newAction(type)))">添加动作</button>
        <button class="btn btn-text btn-sm" @click="action[branch] ||= []; addIf(branch)">添加 IF</button>
        <button class="btn btn-text btn-sm" @click="(action[branch] ||= []).push({ type: 'set_variable', binding_id: createBindingId('action'), variable: '', value: '' })">变量赋值</button>
      </div>
    </section>
    <PluginPicker :open="pickerOpen" kind="action" :keys="actionKeys" :groups="groups" title="选择分支动作" @close="pickerOpen = false; chooseAction = null" @select="select" />
  </div>
</template>

<style scoped>
.if-action-editor { display: grid; gap: 18px; }
.if-branch { border-left: 2px solid var(--md-outline-variant); padding-left: 12px; }
.if-branch h3 { margin: 0 0 12px; font-size: 14px; }
.if-branch-action { display: grid; gap: 12px; padding: 12px 0; border-bottom: 1px solid var(--md-outline-variant); }
.if-branch-action header { display: flex; align-items: center; gap: 4px; }
.if-branch-action header b { flex: 1; }
</style>
