<script setup>
import { computed, ref, watch } from 'vue'
import { store } from '../lib/store'
import { branchRemovalImpact, hasExternalRemovalReferences, removeWorkflowBranch, branchMigrationBlock, removalReferences, nodeRemovalImpact, nodeRemovalBlock, removeWorkflowNode } from '../lib/workflowInspectorOps'
import { graphId, predicateToGraph, copyGraphNode, findGraphNode } from '../lib/workflowGraph'
import { precedingSources, appendActionSources } from '../lib/workflowConditions'
import { variableBindingSources, expandBindingSources } from '../lib/bindings'
import ActionForm from './ActionForm.vue'
import TriggerForm from './TriggerForm.vue'
import PredicateEditor from './PredicateEditor.vue'
import EventActivationEditor from './EventActivationEditor.vue'
import ParamInput from './ParamInput.vue'
import BaseDialog from './BaseDialog.vue'
const props = defineProps({ rule: Object, node: Object, mutations: Object })
const emit = defineEmits(['select-node', 'toggle-group', 'pick', 'operation'])
const action = computed(() => props.node?.source)
const sources = computed(() => props.node?.groupOwner ? precedingSources(props.rule, props.node.groupOwner, store.schema) : action.value?.binding_id ? precedingSources(props.rule, action.value, store.schema) : [])
const exportSources = computed(() => action.value?.workflow ? appendActionSources(sources.value, action.value.workflow.nodes, props.rule, store.schema) : sources.value)
function addExport() { const exports = action.value.exports ||= {}; let name = 'result'; for (let index = 2; Object.hasOwn(exports, name); index++) name = `result${index}`; const source = exportSources.value.find(source => source.value?.$ref?.scope === 'step' && action.value.workflow.nodes.some(node => node.binding_id === source.value.$ref.node)); exports[name] = source ? { $ref: { ...source.value.$ref, on_missing: 'error' } } : '' }
function renameExport(old, name) { name = name.trim(); if (!name || name === old || Object.hasOwn(action.value.exports, name)) return; action.value.exports[name] = action.value.exports[old]; delete action.value.exports[old] }
function removeCondition() { const graph = props.node.conditionGraph, selected = props.node.graphCondition.id, ids = new Set(); function visit(id) { if (ids.has(id)) return; ids.add(id); graph.edges.filter(edge => edge.target === id).forEach(edge => visit(edge.source)) }; visit(selected); graph.nodes = graph.nodes.filter(node => !ids.has(node.id)); graph.edges = graph.edges.filter(edge => !ids.has(edge.source) && !ids.has(edge.target)); if (ids.has(graph.root)) { const replacement = predicateToGraph(); Object.assign(graph, replacement) }; emit('select-node', null) }
function reorderCondition(edge, offset) { const incoming = props.node.conditionGraph.edges.filter(item => item.target === edge.target).sort((a, b) => a.order - b.order), index = incoming.indexOf(edge), target = index + offset; if (!incoming[target]) return; [incoming[index], incoming[target]] = [incoming[target], incoming[index]]; incoming.forEach((item, order) => item.order = order) }
const constants = computed(() => expandBindingSources(variableBindingSources(props.rule, { constantsOnly: true }), store.schema.data_types?.custom))
const entry = computed(() => props.node?.workflow?.entry === action.value?.binding_id && (props.node?.ancestry || []).every(parent => parent.workflow.entry === parent.source.binding_id))
function addBranch() {
  const branch = { id: graphId('b'), condition_graph: predicateToGraph() }
  const branches = action.value.branches
  branches.splice(Math.max(0, branches.findIndex(branch => branch.else)), 0, branch)
  props.node.workflow.edges.push({ source: action.value.binding_id, port: branch.id, target: null })
}
const pendingRemoval = ref(null)
const pendingNodeRemoval = ref(null)
watch([() => props.node?.id, () => props.node?.source], () => { pendingRemoval.value = null; pendingNodeRemoval.value = null })
const migrationBlock = computed(() => pendingRemoval.value?.method === 'move' ? branchMigrationBlock(props.node.workflow, action.value, pendingRemoval.value.branch, pendingRemoval.value.destination) : '')
const removalImpact = computed(() => pendingNodeRemoval.value && !pendingNodeRemoval.value.failure ? nodeRemovalImpact(props.node.workflow, action.value, pendingNodeRemoval.value.port) : null)
const nodeBlock = computed(() => pendingNodeRemoval.value && !pendingNodeRemoval.value.failure ? nodeRemovalBlock(props.rule, props.node.workflow, removalImpact.value) : '')
const nodeReferences = computed(() => removalImpact.value ? removalReferences(props.rule, [action.value, ...removalImpact.value.detached]) : [])
const branchReferences = computed(() => pendingRemoval.value ? removalReferences(props.rule, pendingRemoval.value.nodes, pendingRemoval.value.branch) : [])
function referenceLabel(reference) { const owner = findGraphNode(props.rule, reference.owner)?.action; return `${owner?.name || store.schema.actions?.[owner?.type]?.name || owner?.type || '规则'} · ${reference.path.join('.')} → ${reference.node}` }
function removeBranch(branch) {
  const impact = branchRemovalImpact(props.node.workflow, action.value, branch)
  if (!impact.nodes.length) { removeWorkflowBranch(props.node.workflow, action.value, branch); return }
  pendingRemoval.value = { branch, nodes: impact.nodes, destination: '', method: 'move', referenced: hasExternalRemovalReferences(props.rule, impact.nodes, branch) }
}
function confirmBranchRemoval() {
  const pending = pendingRemoval.value
  if (!pending || migrationBlock.value || pending.method === 'delete' && branchReferences.value.length) return
  if (removeWorkflowBranch(props.node.workflow, action.value, pending.branch, { destination: pending.destination, deleteNodes: pending.method === 'delete' })) pendingRemoval.value = null
}
function changeMode() { if (action.value.mode === 'immediate') delete action.value.timeout_seconds }

function remove() {
  if (props.node.kind === 'failure-action') {
    const references = removalReferences(props.rule, [action.value])
    if (references.length) { pendingNodeRemoval.value = { failure: true, references }; return }
    props.node.owner.failure_actions.splice(props.node.index, 1); emit('select-node', null); return
  }
  const outgoing = props.node.workflow.edges.filter(edge => edge.source === action.value.binding_id)
  const impact = nodeRemovalImpact(props.node.workflow, action.value)
  if (impact && !impact.detached.length && !nodeRemovalBlock(props.rule, props.node.workflow, impact) && !['event_group', 'action_group'].includes(action.value.type)) { removeWorkflowNode(props.rule, props.node.workflow, action.value); emit('select-node', null); return }
  pendingNodeRemoval.value = { port: impact ? outgoing[0]?.port ?? null : '', outgoing }
}
function confirmNodeRemoval() {
  if (pendingNodeRemoval.value?.failure || nodeBlock.value) return
  if (removeWorkflowNode(props.rule, props.node.workflow, action.value, { port: pendingNodeRemoval.value.port })) { pendingNodeRemoval.value = null; emit('select-node', null) }
}

function duplicate() { const copy = copyGraphNode(action.value); if (props.node.kind === 'failure-action') { props.node.owner.failure_actions.splice(props.node.index + 1, 0, copy); emit('select-node', `failure-action-${copy.binding_id}`); return }; emit('operation', { kind: 'control', node: props.node, action: copy }) }
function replaceAction() { emit('pick', { kind: 'action', mode: 'graph-replace', action: action.value }) }
function moveBranch(index, offset) { const branches = action.value.branches, target = index + offset; if (target < 0 || target >= branches.length || branches[target].else) return; const [branch] = branches.splice(index, 1); branches.splice(target, 0, branch) }
</script>
<template>
  <aside v-if="node" class="node-inspector" role="complementary" aria-label="节点设置" @pointerdown.stop>
    <header class="node-inspector-head"><span class="material-symbols-outlined">{{ node.icon }}</span><div><small>{{ node.kicker }}</small><h2>{{ node.label }}</h2></div><button class="icon-btn node-inspector-close" title="关闭设置" @click="emit('select-node', null)"><span class="material-symbols-outlined">close</span></button></header>
    <div :key="node.id" class="node-inspector-body inspector-content-arrive">
      <template v-if="node.kind === 'event_group'">
        <label class="field"><span class="field-label">组名称</span><input v-model="action.name" class="text-field" placeholder="事件组"></label>
        <label v-if="!entry" class="field"><span class="field-label">判定时机</span><select v-model="action.mode" class="select" @change="changeMode"><option value="immediate">到达时立即判断</option><option value="wait">等待事件组合后判断</option></select></label>
        <template v-if="entry || action.mode === 'wait'">
          <h3>{{ entry ? '启动事件组合' : '等待事件组合' }}</h3>
          <EventActivationEditor v-if="action.activation" :node="action.activation" :rule="rule" :sources="constants" @remove="delete action.activation" @simplify="replacement => replacement ? action.activation = replacement : delete action.activation" @pick="target => emit('pick', { kind: 'trigger', mode: 'graph-trigger', owner: action, target })" />
          <button v-else class="btn btn-tonal btn-sm" @click="emit('pick', { kind: 'trigger', mode: 'graph-activation', owner: action })">选择事件</button>
          <label v-if="!entry" class="field"><span class="field-label">等待超时（秒，可选）</span><input v-model.number="action.timeout_seconds" type="number" min="0.001" step="any" class="text-field"></label>
        </template>
        <h3>事件来源</h3>
        <TriggerForm v-for="trigger in action.triggers || []" :key="trigger.binding_id" :node="trigger" :rule="rule" :sources="constants" @replace="emit('pick', { kind: 'trigger', mode: 'graph-trigger', owner: action, target: trigger })" />
        <button class="btn btn-text btn-sm" @click="emit('pick', { kind: 'trigger', mode: 'graph-observer', owner: action })"><span class="material-symbols-outlined">bolt</span>添加事件来源</button>
        <h3>出口</h3><p class="inspector-lead">按 IF、ELIF 顺序，只走第一个成立分支；全部不成立才走 ELSE。</p>
        <section v-for="(branch, index) in action.branches" :key="branch.id" class="workflow-branch-settings">
          <label class="field"><span class="field-label">{{ branch.else ? 'ELSE' : index ? `ELIF ${index}` : 'IF' }} 出口名称</span><input v-model="branch.label" class="text-field" :placeholder="branch.else ? 'ELSE' : index ? `ELIF ${index}` : 'IF'"></label>
          <button v-if="branch.condition_graph" class="btn btn-text btn-sm" @click="emit('operation', { kind: 'branch-condition', node, branch })">编辑条件图</button>
          <div v-if="!branch.else" class="inspector-action-row"><button class="icon-btn" :disabled="index === 0" title="提前判断" @click="moveBranch(index, -1)"><span class="material-symbols-outlined">arrow_upward</span></button><button class="icon-btn" :disabled="action.branches[index + 1]?.else || !action.branches[index + 1]" title="稍后判断" @click="moveBranch(index, 1)"><span class="material-symbols-outlined">arrow_downward</span></button><button class="btn btn-text danger-text" @click="removeBranch(branch)">删除出口</button></div>
        </section>
        <button class="btn btn-tonal btn-sm" @click="addBranch">添加 ELIF</button>
        <button class="btn btn-text btn-sm" @click="emit('toggle-group', action.binding_id)">{{ node.open ? '收起内部条件' : '展开内部条件' }}</button>
      </template>
      <template v-else-if="node.kind === 'action_group'">
        <label class="field"><span class="field-label">组名称</span><input v-model="action.name" class="text-field" placeholder="动作组"></label>
        <p class="inspector-lead">内部动作共享本次运行的变量，完成后从组出口继续。</p>
        <button class="btn btn-tonal" @click="emit('toggle-group', action.binding_id)">{{ node.open ? '收起内部流程' : '展开内部流程' }}</button>
        <h3>导出数据</h3><section v-for="(value, name) in action.exports || {}" :key="name" class="workflow-branch-settings"><input :value="name" class="text-field" aria-label="导出字段名称" @change="renameExport(name, $event.target.value)"><ParamInput :def="{ name, label: '输出值', type: 'string', value_type: 'any' }" v-model="action.exports[name]" allow-binding :binding-sources="exportSources" /><button class="btn btn-text danger-text" @click="delete action.exports[name]">删除导出字段</button></section><button class="btn btn-tonal btn-sm" @click="addExport">添加导出字段</button>
      </template>
      <PredicateEditor v-else-if="node.kind === 'predicate'" :node="action" :rule="rule" :sources="sources" shallow leaf-only />
      <template v-else-if="node.kind === 'condition-expression'">
        <label class="field"><span class="field-label">组合方式</span><select v-model="action.op" class="select"><option value="all">全部满足 · AND</option><option value="any">任一满足 · OR</option><option value="not" :disabled="node.conditionGraph.edges.filter(edge => edge.target === node.graphCondition.id).length !== 1">取反 · NOT</option></select></label>
        <p v-if="node.conditionGraph.edges.filter(edge => edge.target === node.graphCondition.id).length !== 1" class="inspector-lead">NOT 只能接受一个子条件。可在条件出口菜单选择“取反 NOT”，为整个组合添加取反节点。</p>
        <div v-for="edge in node.conditionGraph.edges.filter(edge => edge.target === node.graphCondition.id).sort((a, b) => a.order - b.order)" :key="edge.source" class="inspector-action-row"><span>子条件 {{ edge.order + 1 }}</span><button class="icon-btn" title="提前判断" @click="reorderCondition(edge, -1)"><span class="material-symbols-outlined">arrow_upward</span></button><button class="icon-btn" title="稍后判断" @click="reorderCondition(edge, 1)"><span class="material-symbols-outlined">arrow_downward</span></button></div>
        <button v-if="action.op !== 'not'" class="btn btn-tonal" @click="emit('operation', { kind: 'condition-child', node })">添加子条件</button>
      </template>
      <TriggerForm v-else-if="node.kind === 'trigger'" :node="action" :rule="rule" :sources="constants" @replace="emit('pick', { kind: 'trigger', mode: 'graph-trigger', owner: node.groupOwner, target: action })" />
      <template v-else-if="['action', 'failure-action'].includes(node.kind)">
        <ActionForm :action="action" :rule="rule" :index="node.index" :binding-sources="sources" :allow-failure-actions="node.kind !== 'failure-action'" @replace="replaceAction" @add-failure-action="emit('pick', { kind: 'action', mode: 'graph-failure', owner: action })" @remove-failure-action="index => action.failure_actions.splice(index, 1)" @replace-failure-action="index => emit('pick', { kind: 'action', mode: 'graph-replace', action: action.failure_actions[index] })" />
      </template>
      <button v-else class="btn btn-tonal" @click="emit('operation', { kind: 'control', node })">选择起始操作</button>
      <button v-if="node.conditionGraph" class="btn btn-text danger-text" @click="removeCondition">删除此条件</button>
      <div v-if="['action', 'failure-action', 'event_group', 'action_group'].includes(node.kind)" class="inspector-action-row"><button class="btn btn-text" @click="duplicate"><span class="material-symbols-outlined">content_copy</span>复制</button><button class="btn btn-text danger-text" @click="remove"><span class="material-symbols-outlined">delete</span>删除</button></div>
      <BaseDialog :open="!!pendingRemoval" @close="pendingRemoval = null"><section v-if="pendingRemoval" class="workflow-branch-settings branch-removal-dialog" role="dialog" aria-label="移除出口"><h3>移除出口后处理动作</h3><p>这个出口独占 {{ pendingRemoval.nodes.length }} 个节点：{{ pendingRemoval.nodes.map(node => node.name || node.type).join('、') }}。</p><label class="field"><span class="field-label">处理方式</span><select v-model="pendingRemoval.method" class="select"><option value="move">保留动作，接入另一个出口</option><option value="delete" :disabled="branchReferences.length">同时删除这些动作</option></select></label><template v-if="pendingRemoval.method === 'move'"><p>选定出口将先执行这些动作，再执行它原有的动作。共享的后续节点继续保留。</p><select v-model="pendingRemoval.destination" class="select" aria-label="接入出口"><option value="" disabled>选择出口</option><option v-for="branch in action.branches.filter(branch => branch.id !== pendingRemoval.branch.id)" :key="branch.id" :value="branch.id">{{ branch.label || (branch.else ? 'ELSE' : `分支 ${action.branches.indexOf(branch) + 1}`) }}</option></select></template><p v-if="migrationBlock" class="inspector-lead">{{ migrationBlock }}</p><p v-else-if="pendingRemoval.method === 'delete'">这些动作及其组内流程将一并删除，此操作可通过撤销恢复。</p><p v-if="branchReferences.length" class="inspector-lead">其他条件或动作仍引用这些节点，必须保留动作或先移除相关引用。</p><ul v-if="branchReferences.length"><li v-for="(reference, index) in branchReferences" :key="index">{{ referenceLabel(reference) }}</li></ul><div class="inspector-action-row"><button class="btn btn-text" @click="pendingRemoval = null">取消</button><button class="btn btn-tonal" :disabled="!!migrationBlock || pendingRemoval.method === 'move' && !pendingRemoval.destination" @click="confirmBranchRemoval">确认移除出口</button></div></section></BaseDialog>
      <BaseDialog :open="!!pendingNodeRemoval" @close="pendingNodeRemoval = null"><section v-if="pendingNodeRemoval" class="workflow-branch-settings branch-removal-dialog" role="dialog" aria-label="删除节点"><h3>删除 {{ node.label }}</h3><template v-if="!pendingNodeRemoval.failure"><p>节点{{ action.type === 'action_group' ? '及其内部流程' : '' }}将被移除。入线将接到选定出口，其他独占路径也会删除，共享后续节点保留。</p><label class="field"><span class="field-label">删除后续接</span><select v-model="pendingNodeRemoval.port" class="select" aria-label="保留出口"><option value="" disabled>选择保留的出口</option><option v-for="(edge, index) in pendingNodeRemoval.outgoing" :key="edge.port" :value="edge.port">{{ action.branches?.find(branch => branch.id === edge.port)?.label || (action.branches?.find(branch => branch.id === edge.port)?.else ? 'ELSE' : action.type === 'event_group' ? `分支 ${index + 1}` : '完成') }} · {{ edge.target ? props.node.workflow.nodes.find(node => node.binding_id === edge.target)?.name || props.node.workflow.nodes.find(node => node.binding_id === edge.target)?.type : '结束' }}</option><option :value="null">结束当前路径</option></select></label><p v-if="removalImpact?.detached.length">同时删除的独占节点：{{ removalImpact.detached.map(node => node.name || node.type).join('、') }}。</p><p v-if="nodeBlock" class="inspector-lead">{{ nodeBlock }}</p></template><p v-else>其他节点仍引用此补救动作的输出，请先调整引用。</p><ul v-if="nodeReferences.length || pendingNodeRemoval.references?.length"><li v-for="(reference, index) in nodeReferences.length ? nodeReferences : pendingNodeRemoval.references" :key="index">{{ referenceLabel(reference) }}</li></ul><div class="inspector-action-row"><button class="btn btn-text" @click="pendingNodeRemoval = null">取消</button><button v-if="!pendingNodeRemoval.failure" class="btn btn-tonal" :disabled="!!nodeBlock" @click="confirmNodeRemoval">确认删除节点及列出的路径</button></div></section></BaseDialog>
    </div>
  </aside>
</template>
<style scoped>.workflow-branch-settings{display:grid;gap:8px;margin:12px 0;padding:12px;border:1px solid var(--md-outline-variant);border-radius:var(--r-sm)}.branch-removal-dialog{width:min(520px,calc(100vw - 32px));max-height:85vh;overflow:auto;background:var(--md-surface-c-high);padding:24px;box-shadow:var(--md-elev3)}h3{font:var(--ts-title-s);margin:20px 0 12px}</style>
