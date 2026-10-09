<script>
const ifSelections = new WeakMap()
</script>
<script setup>
import { computed, ref, watch, onUnmounted } from 'vue'
import RuleCanvas from './RuleCanvas.vue'
import PredicateEditor from './PredicateEditor.vue'
import { store } from '../lib/store'
import { buildIfSubflowGraph } from '../lib/flowGraph'
import { ifBranches, precedingSources, predicateSummary, workflowEventChoices } from '../lib/workflowConditions'
const props = defineProps({ rule: Object, action: Object, layout: String, initialNodeId: String })
const emit = defineEmits(['show-main'])
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
const graph = computed(() => buildIfSubflowGraph({ rule: props.rule, action: props.action, schema: store.schema, sources: sources.value, rules: store.configData.rules }))
const selected = computed(() => graph.value.nodes.find(node => node.id === selectedId.value))
watch(() => props.initialNodeId, id => { if (id) selectedId.value = id })
function branchByKey(key) { return branches.value.find(branch => branch.key === key) }
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
function removePredicate(node) {
  if (!node.predicatePath.length) return
  let parent = branchByKey(node.branchKey).condition
  for (const index of node.predicatePath.slice(0, -1)) parent = parent.children[index]
  if (parent.op === 'not') return
  parent.children.splice(node.predicatePath.at(-1), 1)
  selectedId.value = null
}
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
      v-model:selected-node-id="selectedId" @select-node="select" @predicate-select="node => select(node.id)">
      <template #inspector>
        <aside v-if="selected" class="node-inspector" role="complementary" aria-label="IF 子流程设置" @pointerdown.stop>
          <header class="node-inspector-head"><span class="material-symbols-outlined">{{ selected.icon }}</span><div><small>{{ selected.kicker }}</small><h2>{{ selected.label }}</h2></div><button class="icon-btn" title="关闭设置" @click="selectedId = null"><span class="material-symbols-outlined">close</span></button></header>
          <div class="node-inspector-body">
            <template v-if="selected.kind === 'predicate'">
              <PredicateEditor :node="selected.source" :sources="sources" :rule="rule" shallow />
              <button v-if="selected.predicatePath.length" class="btn btn-text btn-sm danger-text" @click="removePredicate(selected)">移除此条件</button>
            </template>
            <template v-else-if="selected.kind === 'if-branch'">
              <section v-for="branch in branches" :key="branch.key" class="subflow-branch">
                <h3>{{ branch.label }}</h3><p v-if="branch.condition" class="inspector-lead">{{ predicateSummary(branch.condition, sources, store.configData.rules, workflowEventChoices(rule, store.schema)) }}</p>
                <button v-if="branch.branch" class="btn btn-text btn-sm" @click="addCondition(branch.key)">添加 ELIF 条件</button>
                <button class="btn btn-tonal btn-sm" @click="emit('show-main', branch.key)">在主流程编辑动作</button>
                <button v-if="branch.branch" class="btn btn-text btn-sm danger-text" @click="action.elif.splice(branch.index, 1); selectedId = null">删除 ELIF</button>
              </section>
            </template>

          </div>
        </aside>
      </template>
    </RuleCanvas>

  </section>
</template>
<style scoped>
.if-subflow-editor { display: flex; flex-direction: column; flex: 1 1 auto; min-height: 0; }
.if-subflow-tools { flex: none; display: flex; align-items: center; flex-wrap: wrap; gap: 8px; padding: 8px 0; }
.if-subflow-tools p { flex: 1; min-width: 260px; font-size: 13px; color: var(--md-on-surface-variant); }
.subflow-branch { padding: 12px 0; border-bottom: 1px solid var(--md-outline-variant); }
.subflow-branch h3 { font-size: 14px; }
</style>
