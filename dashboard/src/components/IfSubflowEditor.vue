<script>
const ifSelections = new WeakMap()
</script>
<script setup>
import { computed, ref, watch, onUnmounted } from 'vue'
import RuleCanvas from './RuleCanvas.vue'
import PredicateEditor from './PredicateEditor.vue'
import ActionForm from './ActionForm.vue'
import ParamInput from './ParamInput.vue'
import ActionRunLimits from './ActionRunLimits.vue'
import PluginPicker from './PluginPicker.vue'
import { store } from '../lib/store'
import { buildIfSubflowGraph } from '../lib/flowGraph'
import { actionName, describeItem } from '../lib/rulePresentation'
import { createBindingId, regenerateBindingIds, parameterAllowsBinding } from '../lib/bindings'
import { ifBranches, precedingSources, predicateSummary } from '../lib/workflowConditions'
import { buildDefaultParams, getVisibleParamDefs, groupActionKeys, isSpecialBlockAction, pluginUnavailableReason } from '../lib/utils'
const props = defineProps({ rule: Object, action: Object, layout: String, initialNodeId: String })
const emit = defineEmits(['open-if'])
if (!ifSelections.has(props.rule)) ifSelections.set(props.rule, new Map())
let selections = ifSelections.get(props.rule)
const selectedId = ref(props.initialNodeId || selections.get(props.action.binding_id) || null)
onUnmounted(() => selections.set(props.action.binding_id, selectedId.value))
watch(() => [props.rule, props.action.binding_id], ([rule, id], [, previous]) => {
  if (previous) selections.set(previous, selectedId.value)
  if (!ifSelections.has(rule)) ifSelections.set(rule, new Map())
  selections = ifSelections.get(rule)
  selectedId.value = props.initialNodeId || selections.get(id) || (id === previous ? selectedId.value : null)
})
const canvas = ref(null)
const sources = computed(() => precedingSources(props.rule, props.action, store.schema))
const branches = computed(() => ifBranches(props.action))
const graph = computed(() => buildIfSubflowGraph({ rule: props.rule, action: props.action, schema: store.schema, sources: sources.value, rules: store.configData.rules, actionName, describeItem }))
const selected = computed(() => graph.value.nodes.find(node => node.id === selectedId.value))
watch(() => props.initialNodeId, id => { if (id) selectedId.value = id })
const actionKeys = computed(() => Object.keys(store.schema.actions).filter(key => !pluginUnavailableReason(store.schema.actions[key]) && !isSpecialBlockAction(key)))
const picker = ref(false)
let pickCallback = null
function pick(callback) { pickCallback = callback; picker.value = true }
function choose(type) { pickCallback?.(type); picker.value = false; pickCallback = null }
function newAction(type) { return { type, binding_id: createBindingId('action'), ...(type === 'if' ? { condition: { op: 'eq', left: '', right: '' }, then: [], elif: [], else: [] } : type === 'set_variable' ? { variable: '', value: '' } : { params: buildDefaultParams(store.schema.actions[type]) }) } }
function branchByKey(key) { return branches.value.find(branch => branch.key === key) }
function addAction(key, type) {
  const item = newAction(type)
  branchByKey(key).actions.push(item)
  if (type === 'if') emit('open-if', item)
  else selectedId.value = `action-${item.binding_id}`
}
function addElif() { (props.action.elif ||= []).push({ condition: { op: 'eq', left: '', right: '' }, then: [] }) }
function addCondition(key) {
  const entry = branchByKey(key)
  if (['all', 'any'].includes(entry.condition.op)) entry.condition.children.push({ op: 'eq', left: '', right: '' })
  else {
    const wrapped = { op: 'all', children: [entry.condition, { op: 'eq', left: '', right: '' }] }
    if (key === 'then') props.action.condition = wrapped
    else entry.branch.condition = wrapped
  }
}
function select(id) { const node = graph.value.nodes.find(node => node.id === id); if (node?.readOnly || node?.kind === 'if-merge') return; selectedId.value = id }
function nodeItems(node) { return node.kind === 'failure-action' ? node.owner.failure_actions : branchByKey(node.branchKey).actions }
function moveNode(node, offset) {
  const items = nodeItems(node)
  const target = node.index + offset
  if (target < 0 || target >= items.length) return
  items.splice(target, 0, items.splice(node.index, 1)[0])
}
function removeFailure(item, index) { item.failure_actions.splice(index, 1); if (!item.failure_actions.length) delete item.failure_actions }
function removeNode(node) { if (node.kind === 'failure-action') removeFailure(node.owner, node.index); else nodeItems(node).splice(node.index, 1); selectedId.value = null }
function duplicate(node) { const copy = regenerateBindingIds(JSON.parse(JSON.stringify(node.source)), 'action'); nodeItems(node).splice(node.index + 1, 0, copy); selectedId.value = `${node.kind === 'failure-action' ? 'failure-action' : 'action'}-${copy.binding_id}` }
function removePredicate(node) {
  if (!node.predicatePath.length) return
  let parent = branchByKey(node.branchKey).condition
  for (const index of node.predicatePath.slice(0, -1)) parent = parent.children[index]
  if (parent.op === 'not') return
  parent.children.splice(node.predicatePath.at(-1), 1)
  selectedId.value = null
}
function replace(item, type) { item.type = type; item.params = buildDefaultParams(store.schema.actions[type]); if (store.schema.actions[type]?.cancellation_api !== 'runtime-v1') delete item.timeout_seconds }
function moveFailure(item, index, offset) { const items = item.failure_actions; const target = index + offset; if (target >= 0 && target < items.length) items.splice(target, 0, items.splice(index, 1)[0]) }
const actionSources = computed(() => ['action', 'failure-action'].includes(selected.value?.kind) ? precedingSources(props.rule, selected.value.source, store.schema) : sources.value)
defineExpose({ selectNode: select })
</script>
<template>
  <section class="if-subflow-editor">
    <div class="if-subflow-tools">
      <p>按 IF、ELIF 的顺序判断，只执行第一个成立分支；全部不成立才走 ELSE。</p>
      <button class="btn btn-tonal btn-sm" @click="addCondition('then')">添加 IF 条件</button>
      <button class="btn btn-tonal btn-sm" @click="addElif">添加 ELIF</button>
    </div>
    <RuleCanvas ref="canvas" :rule="rule" :layout="layout" :graph-override="graph" :view-key="action.binding_id"
      v-model:selected-node-id="selectedId" @select-node="select" @open-if="emit('open-if', $event)" @predicate-select="node => select(node.id)" @branch-add="node => select(node.id)" @move-action="(index, offset, node) => moveNode(node, offset)" @remove-action="(index, node) => removeNode(node)" @move-failure-action="(parentIndex, index, offset, node) => moveNode(node, offset)" @remove-failure-action="(parentIndex, index, node) => removeNode(node)">
      <template #inspector>
        <aside v-if="selected" class="node-inspector" role="complementary" aria-label="IF 子流程设置" @pointerdown.stop>
          <header class="node-inspector-head"><span class="material-symbols-outlined">{{ selected.icon }}</span><div><small>{{ selected.kicker }}</small><h2>{{ selected.label }}</h2></div><button class="icon-btn" title="关闭设置" @click="selectedId = null"><span class="material-symbols-outlined">close</span></button></header>
          <div class="node-inspector-body">
            <template v-if="selected.kind === 'predicate'">
              <PredicateEditor :node="selected.source" :sources="sources" :rule="rule" shallow />
              <button v-if="selected.predicatePath.length" class="btn btn-text btn-sm danger-text" @click="removePredicate(selected)">移除此条件</button>
            </template>
            <template v-else-if="selected.kind === 'if-branch'">
              <section v-for="branch in (selected.addBranch ? [branchByKey(selected.branchKey)] : branches)" :key="branch.key" class="subflow-branch">
                <h3>{{ branch.label }}</h3><p v-if="branch.condition" class="inspector-lead">{{ predicateSummary(branch.condition, sources, store.configData.rules) }}</p>
                <button v-if="branch.branch" class="btn btn-text btn-sm" @click="addCondition(branch.key)">添加 ELIF 条件</button>
                <div class="flow-add-row"><button class="btn btn-tonal btn-sm" @click="pick(type => addAction(branch.key, type))">添加动作</button><button class="btn btn-text btn-sm" @click="addAction(branch.key, 'if')">添加 IF</button><button class="btn btn-text btn-sm" @click="addAction(branch.key, 'set_variable')">变量赋值</button><button class="btn btn-text btn-sm" @click="addAction(branch.key, 'wait')">等待</button></div>
                <button v-if="branch.branch" class="btn btn-text btn-sm danger-text" @click="action.elif.splice(branch.index, 1); selectedId = null">删除 ELIF</button>
              </section>
            </template>
            <template v-else-if="['action', 'failure-action'].includes(selected.kind)">
              <ActionForm v-if="selected.kind === 'action'" :action="selected.source" :rule="rule" :index="selected.index" :binding-sources="actionSources"
                :failure-binding-sources="index => precedingSources(rule, selected.source.failure_actions[index], store.schema)"
                @replace="pick(type => replace(selected.source, type))"
                @add-failure-action="pick(type => (selected.source.failure_actions ||= []).push(newAction(type)))"
                @replace-failure-action="index => pick(type => replace(selected.source.failure_actions[index], type))"
                @remove-failure-action="index => removeFailure(selected.source, index)"
                @move-failure-action="(index, offset) => moveFailure(selected.source, index, offset)" />
              <template v-else>
                <button class="plugin-type-button" type="button" @click="pick(type => replace(selected.source, type))"><span class="material-symbols-outlined">build</span>{{ actionName(selected.source) }}</button>
                <div class="param-grid"><ParamInput v-for="param in getVisibleParamDefs(store.schema.actions[selected.source.type], selected.source.params)" :key="param.name" :def="param" :plugin-id="selected.source.type" v-model="selected.source.params[param.name]" :allow-binding="parameterAllowsBinding(store.schema.actions[selected.source.type], param.name)" :binding-sources="actionSources" /></div>
                <label class="field"><span class="field-label">这个补救动作也失败时</span><select class="select" :value="selected.source.on_error === 'continue' ? 'continue' : 'stop'" @change="selected.source.on_error = $event.target.value"><option value="stop">停止剩余补救动作</option><option value="continue">继续执行剩余补救动作</option></select></label>
                <ActionRunLimits :action="selected.source" :meta="store.schema.actions[selected.source.type]" />
              </template>
              <div class="inspector-action-row"><button class="btn btn-text btn-sm" :disabled="selected.index === 0" @click="moveNode(selected, -1)">提前</button><button class="btn btn-text btn-sm" :disabled="selected.index === nodeItems(selected).length - 1" @click="moveNode(selected, 1)">稍后</button><button class="btn btn-text btn-sm" @click="duplicate(selected)">复制</button><button class="btn btn-text btn-sm danger-text" @click="removeNode(selected)">删除</button></div>
              <button class="btn btn-tonal btn-sm" @click="pick(type => { const item = newAction(type); nodeItems(selected).splice(selected.index, 0, item) })">在此前插入动作</button>
            </template>
          </div>
        </aside>
      </template>
    </RuleCanvas>
    <PluginPicker :open="picker" kind="action" :keys="actionKeys" :groups="groupActionKeys(actionKeys)" title="选择分支动作" @select="choose" @close="picker = false; pickCallback = null" />
  </section>
</template>
<style scoped>
.if-subflow-editor { display: flex; flex-direction: column; flex: 1 1 auto; min-height: 0; }
.if-subflow-tools { flex: none; display: flex; align-items: center; flex-wrap: wrap; gap: 8px; padding: 8px 0; }
.if-subflow-tools p { flex: 1; min-width: 260px; font-size: 13px; color: var(--md-on-surface-variant); }
.subflow-branch { padding: 12px 0; border-bottom: 1px solid var(--md-outline-variant); }
.subflow-branch h3 { font-size: 14px; }
</style>
