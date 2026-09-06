<script setup>
import { computed, ref } from 'vue'
import { store } from '../lib/store'
import { createBindingId, actionOutputDefs, expandBindingSources, parameterAllowsBinding } from '../lib/bindings'
import { fieldType } from '../lib/valueTypes'
import { buildDefaultParams, ensureParams, getVisibleParamDefs, groupActionKeys, pluginUnavailableReason } from '../lib/utils'
import PredicateEditor from './PredicateEditor.vue'
import ParamInput from './ParamInput.vue'
import PluginPicker from './PluginPicker.vue'
import ActionFailureSettings from './ActionFailureSettings.vue'
import VariableAssignmentEditor from './VariableAssignmentEditor.vue'

defineOptions({ name: 'IfActionEditor' })
const props = defineProps({ action: Object, rule: { type: Object, default: () => ({}) }, sources: { type: Array, default: () => [] } })
const actionKeys = computed(() => Object.keys(store.schema.actions).filter(key => !pluginUnavailableReason(store.schema.actions[key])))
const groups = computed(() => groupActionKeys(actionKeys.value))
const pickerOpen = ref(false)
let chooseAction = null
function pick(callback) { chooseAction = callback; pickerOpen.value = true }
function select(type) { chooseAction?.(type); pickerOpen.value = false }
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
  const id = item.binding_id
  Object.keys(item).forEach(key => delete item[key])
  Object.assign(item, newAction(type), { binding_id: id })
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
        <IfActionEditor v-if="item.type === 'if'" :action="item" :rule="rule" :sources="branchSources(branch, index)" />
        <VariableAssignmentEditor v-else-if="item.type === 'set_variable'" :action="item" :sources="branchSources(branch, index)" />
        <template v-else>
          <button class="btn btn-text btn-sm" @click="pick(type => replace(item, type))">更换动作类型</button>
          <p v-if="pluginUnavailableReason(store.schema.actions[item.type])" class="danger-text">{{ pluginUnavailableReason(store.schema.actions[item.type]) }}</p>
          <ParamInput v-for="param in getVisibleParamDefs(store.schema.actions[item.type], ensureParams(item))" :key="param.name" :def="param" :plugin-id="item.type" v-model="item.params[param.name]" :allow-binding="parameterAllowsBinding(store.schema.actions[item.type], param.name)" :binding-sources="branchSources(branch, index)" />
          <ActionFailureSettings :action="item" :meta="store.schema.actions[item.type]" :schema="store.schema.actions" :binding-sources="failureIndex => failureSources(branch, index, failureIndex)"
            @add-failure-action="pick(type => (item.failure_actions ||= []).push(newAction(type)))"
            @replace-failure-action="failureIndex => pick(type => replace(item.failure_actions[failureIndex], type))"
            @remove-failure-action="failureIndex => { item.failure_actions.splice(failureIndex, 1); if (!item.failure_actions.length) delete item.failure_actions }"
            @move-failure-action="(failureIndex, offset) => move(item.failure_actions, failureIndex, offset)" />
        </template>
      </article>
      <div class="flow-add-row">
        <button class="btn btn-tonal btn-sm" @click="pick(type => (action[branch] ||= []).push(newAction(type)))">添加动作</button>
        <button class="btn btn-text btn-sm" @click="action[branch] ||= []; addIf(branch)">添加 IF</button>
        <button class="btn btn-text btn-sm" @click="(action[branch] ||= []).push({ type: 'set_variable', binding_id: createBindingId('action'), variable: '', value: '' })">变量赋值</button>
      </div>
    </section>
    <PluginPicker :open="pickerOpen" kind="action" :keys="actionKeys" :groups="groups" title="选择分支动作" @close="pickerOpen = false" @select="select" />
  </div>
</template>

<style scoped>
.if-action-editor { display: grid; gap: 18px; }
.if-branch { border-left: 2px solid var(--outline-variant); padding-left: 12px; }
.if-branch h3 { margin: 0 0 12px; font-size: 14px; }
.if-branch-action { display: grid; gap: 12px; padding: 12px 0; border-bottom: 1px solid var(--outline-variant); }
.if-branch-action header { display: flex; align-items: center; gap: 4px; }
.if-branch-action header b { flex: 1; }
</style>
