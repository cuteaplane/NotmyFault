<script setup>
import { computed, ref } from 'vue'
import { store } from '../lib/store'
import { createBindingId } from '../lib/bindings'
import { ifBranches, appendActionSources } from '../lib/workflowConditions'
import { buildDefaultParams, groupActionKeys, isSpecialBlockAction, pluginUnavailableReason } from '../lib/utils'
import PredicateEditor from './PredicateEditor.vue'
import ActionForm from './ActionForm.vue'
import PluginPicker from './PluginPicker.vue'

defineOptions({ name: 'IfActionEditor' })
const props = defineProps({ action: Object, rule: { type: Object, default: () => ({}) }, sources: { type: Array, default: () => [] } })
const actionKeys = computed(() => Object.keys(store.schema.actions).filter(key => !pluginUnavailableReason(store.schema.actions[key]) && !isSpecialBlockAction(key)))
const groups = computed(() => groupActionKeys(actionKeys.value))
const pickerOpen = ref(false)
let chooseAction = null
function pick(callback) { chooseAction = callback; pickerOpen.value = true }
function select(type) { const callback = chooseAction; chooseAction = null; pickerOpen.value = false; callback?.(type) }
function newAction(type) {
  return { binding_id: createBindingId('action'), type, params: buildDefaultParams(store.schema.actions[type]) }
}
function addIf(branch) {
  branchItems(branch).push({ binding_id: createBindingId('action'), type: 'if', condition: { op: 'is_true', left: false }, then: [], elif: [], else: [] })
}
const branches = computed(() => ifBranches(props.action))
function branchItems(key) { return branches.value.find(branch => branch.key === key)?.actions || [] }
function addElif() { (props.action.elif ||= []).push({ condition: { op: 'eq', left: '', right: '' }, then: [] }) }
function branchSources(branch, index) {
  return appendActionSources(props.sources, branchItems(branch).slice(0, index), props.rule, store.schema)
}
function failureSources(branch, index, failureIndex) {
  return appendActionSources(branchSources(branch, index), (branchItems(branch)[index].failure_actions || []).slice(0, failureIndex), props.rule, store.schema)
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
    <PredicateEditor :node="action.condition" :sources="sources" :rule="rule" />
    <p class="inspector-lead">按 IF、ELIF 的顺序判断，只执行第一个成立分支；全部不成立才执行 ELSE。</p>
    <section v-for="entry in branches" :key="entry.key" class="if-branch">
      <h3>{{ entry.label }}</h3>
      <template v-if="entry.branch"><PredicateEditor :node="entry.condition" :sources="sources" :rule="rule" /><button class="btn btn-text btn-sm danger-text" @click="action.elif.splice(entry.index, 1)">删除 ELIF</button></template>
      <p v-if="!branchItems(entry.key)?.length" class="inspector-lead">此分支为空，继续后续动作。</p>
      <article v-for="(item, index) in branchItems(entry.key)" :key="item.binding_id" class="if-branch-action">
        <header><b>{{ index + 1 }}. {{ item.type === 'if' ? 'IF 条件分支' : item.type === 'set_variable' ? '变量赋值' : item.type === 'wait' ? (store.schema.actions.wait?.name || '等待') : store.schema.actions[item.type]?.name || item.type }}</b>
          <button class="icon-btn" title="上移" :disabled="index === 0" @click="move(branchItems(entry.key), index, -1)"><span class="material-symbols-outlined">arrow_upward</span></button>
          <button class="icon-btn" title="下移" :disabled="index === branchItems(entry.key).length - 1" @click="move(branchItems(entry.key), index, 1)"><span class="material-symbols-outlined">arrow_downward</span></button>
          <button class="icon-btn danger-text" title="移除分支动作" @click="branchItems(entry.key).splice(index, 1)"><span class="material-symbols-outlined">delete</span></button>
        </header>
        <ActionForm :action="item" :rule="rule" :index="index" :binding-sources="branchSources(entry.key, index)" :failure-binding-sources="failureIndex => failureSources(entry.key, index, failureIndex)"
          @replace="pick(type => replace(item, type))"
          @add-failure-action="pick(type => (item.failure_actions ||= []).push(newAction(type)))"
          @replace-failure-action="failureIndex => pick(type => replace(item.failure_actions[failureIndex], type))"
          @remove-failure-action="failureIndex => { item.failure_actions.splice(failureIndex, 1); if (!item.failure_actions.length) delete item.failure_actions }"
          @move-failure-action="(failureIndex, offset) => move(item.failure_actions, failureIndex, offset)" />
      </article>
      <div class="flow-add-row">
        <button class="btn btn-tonal btn-sm" @click="pick(type => branchItems(entry.key).push(newAction(type)))">添加动作</button>
        <button class="btn btn-text btn-sm" @click=" addIf(entry.key)">添加 IF</button>
        <button class="btn btn-text btn-sm" @click="branchItems(entry.key).push({ type: 'set_variable', binding_id: createBindingId('action'), variable: '', value: '' })">变量赋值</button>
        <button class="btn btn-text btn-sm" @click="branchItems(entry.key).push(newAction('wait'))">等待</button>
      </div>
    </section>
    <button class="btn btn-tonal btn-sm" @click="addElif">添加 ELIF</button>
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
