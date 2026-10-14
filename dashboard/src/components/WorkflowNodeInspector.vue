<script setup>
import { computed } from 'vue'
import { store } from '../lib/store'
import { switchGroupType, removeGraphNode, copyGraphNode, findRecoveryGroup } from '../lib/workflowGraph'
import { precedingSources, appendActionSources } from '../lib/workflowConditions'
import { variableBindingSources, expandBindingSources, collectReferences } from '../lib/bindings'
import { alertDialog } from '../lib/dialog'
import ActionForm from './ActionForm.vue'
import TriggerForm from './TriggerForm.vue'
import PredicateEditor from './PredicateEditor.vue'
import ParamInput from './ParamInput.vue'
const props = defineProps({ rule: Object, node: Object })
const emit = defineEmits(['select-node', 'toggle-group', 'pick', 'operation'])
const raw = computed(() => props.node?.raw)
const sources = computed(() => precedingSources(props.rule, raw.value, store.schema))
const recoveryGroup = computed(() => findRecoveryGroup(props.rule.workflow, raw.value))
const inRecovery = computed(() => { let node = raw.value; while (node?.parent) { if (props.rule.workflow.edges.some(edge => edge.port === 'failure' && edge.target === node.parent)) return true; node = props.rule.workflow.nodes.find(item => item.binding_id === node.parent) } return false })
function editRecovery() { emit('toggle-group', recoveryGroup.value.binding_id); emit('select-node', `action-${recoveryGroup.value.binding_id}`) }
function disconnectRecovery() { props.rule.workflow.edges = props.rule.workflow.edges.filter(edge => !(edge.source === raw.value.binding_id && edge.port === 'failure')) }
const constants = computed(() => expandBindingSources(variableBindingSources(props.rule, { constantsOnly: true }), store.schema.data_types?.custom))
function descendants(node) {
  const found = new Set([node.binding_id])
  let changed
  do { changed = false; for (const child of props.rule.workflow.nodes) if (found.has(child.parent) && !found.has(child.binding_id)) { found.add(child.binding_id); changed = true } } while (changed)
  return props.rule.workflow.nodes.filter(child => found.has(child.binding_id))
}
async function remove(node = raw.value) {
  const removed = descendants(node)
  const ids = new Set(removed.flatMap(item => [item.binding_id, item.trigger?.binding_id]).filter(Boolean))
  const referenced = props.rule.workflow.nodes.filter(item => !ids.has(item.binding_id)).some(item => collectReferences(item).some(ref => ids.has(ref.node)))
  if (referenced) { await alertDialog('节点仍被引用', '请先更改引用这个节点输出的数据绑定，再删除节点。'); return }
  removeGraphNode(props.rule.workflow, node); emit('select-node', null)
}
function requestDelete(node = props.node) { return remove(node.raw) }
defineExpose({ requestDelete })
function duplicate() { const copy = copyGraphNode(raw.value, props.rule.workflow); emit('operation', { operation: 'duplicate', node: props.node, action: copy }) }
function changeGroup(event) { switchGroupType(props.rule.workflow, raw.value, event.target.value) }
const exportSources = computed(() => appendActionSources(sources.value, descendants(raw.value).filter(node => node !== raw.value), props.rule, store.schema))
function addExport() { const outputs = raw.value.exports ||= {}; let name = 'result'; for (let index = 2; Object.hasOwn(outputs, name); index++) name = `result${index}`; outputs[name] = '' }
function renameExport(old, name) { name = name.trim(); if (!name || name === old || Object.hasOwn(raw.value.exports, name)) return; raw.value.exports[name] = raw.value.exports[old]; delete raw.value.exports[old] }
</script>
<template>
  <aside v-if="node && raw" class="node-inspector" role="complementary" aria-label="节点设置" @pointerdown.stop>
    <header class="node-inspector-head"><span class="material-symbols-outlined">{{ node.icon }}</span><div><small>{{ node.kicker }}</small><h2>{{ node.label }}</h2></div><button class="icon-btn node-inspector-close" title="关闭设置" @click="emit('select-node', null)"><span class="material-symbols-outlined">close</span></button></header>
    <div :key="node.id" class="node-inspector-body inspector-content-arrive">
      <template v-if="node.gate && raw.group_type === 'if'">
        <label class="field"><span class="field-label">出口名称</span><input v-model="node.branch.label" class="text-field" :placeholder="node.label"></label>
        <p class="inspector-lead">{{ node.branch.else ? '所有 IF／ELIF 都不成立时执行，ELSE 不接条件。' : '从触发器或逻辑节点拖线接入条件，从右侧出口连接动作。' }}</p>
        <label v-if="!node.branch.else" class="field"><span class="field-label">条件取反</span><input v-model="node.branch.negated" type="checkbox" aria-label="分支条件取反"></label>
      </template>
      <template v-else-if="['event_group', 'action_group'].includes(raw.type)">
        <label class="field"><span class="field-label">组名称</span><input v-model="raw.name" class="text-field" placeholder="事件组"></label>
        <label v-if="raw.type === 'event_group'" class="field"><span class="field-label">组类型</span><select :value="raw.group_type" class="select" @change="changeGroup"><option value="if">IF／ELIF／ELSE</option><option value="all">AND · 全部满足</option><option value="any">OR · 任一满足</option><option value="not">NOT · 取反</option></select></label>
        <p class="inspector-lead">图块可拖入或拖出，改变归属时断开跨组连线。Ctrl 多选后可组合，内部连接由你编辑。</p>
        <label v-if="raw.group_type === 'all'" class="field"><span class="field-label">入口事件时间窗口（秒）</span><input v-model.number="raw.within_seconds" type="number" min="0.001" step="any" class="text-field" placeholder="10"></label>
        <template v-if="raw.type === 'event_group'">
          <label class="field"><span class="field-label">执行到此组时</span><select :value="raw.mode || 'immediate'" class="select" @change="raw.mode = $event.target.value; delete raw.timeout_seconds"><option value="immediate">立即判断已收到的事件</option><option value="wait">等待事件组合成立后判断</option></select></label>
          <template v-if="raw.mode === 'wait'">
            <label class="field"><span class="field-label">等待超时（秒，可留空）</span><input :value="raw.timeout_seconds ?? ''" class="text-field" type="number" min="0.001" max="86400" step="any" @input="$event.target.value === '' ? delete raw.timeout_seconds : raw.timeout_seconds = Number($event.target.value)"></label>
            <p class="inspector-lead">需连接触发器；等待事件组合成立后，按 IF／ELIF 顺序选择分支。纯数据比较不触发等待。</p>
          </template>
        </template>
        <label v-if="raw.group_type && raw.group_type !== 'if'" class="field"><span class="field-label">结果取反</span><input v-model="raw.negated" type="checkbox" aria-label="逻辑结果取反"></label>
        <button class="btn btn-tonal" @click="emit('toggle-group', raw.binding_id)">{{ node.open ? '收起组' : '展开组' }}</button>
        <template v-if="raw.type === 'action_group'">
          <p class="inspector-lead">从组内“动作组入口”连接起始动作；组完成后从外侧出口继续。对外输出可引用组内动作的结果。</p>
          <section v-for="(value, name) in raw.exports" :key="name" class="workflow-branch-settings">
            <label class="field"><span class="field-label">输出名称</span><input :value="name" class="text-field" @change="renameExport(name, $event.target.value)"></label>
            <ParamInput :def="{ name, label: '输出值', type: 'string', value_type: 'any' }" v-model="raw.exports[name]" allow-binding :binding-sources="exportSources" />
            <button class="btn btn-text danger-text" @click="delete raw.exports[name]">删除输出</button>
          </section>
          <button class="btn btn-text" @click="addExport">添加对外输出</button>
        </template>
      </template>
      <template v-else-if="raw.type === 'trigger'">
        <TriggerForm :node="raw.trigger" :rule="rule" :sources="constants" @replace="emit('pick', { kind: 'trigger', mode: 'replace', node })" />
        <label class="field"><span class="field-label">事件条件</span><select v-model="raw.occurrence" class="select" aria-label="事件发生条件"><option value="occurred">事件发生</option><option value="absent">事件未发生</option></select></label>
        <template v-if="raw.occurrence === 'absent'">
          <label class="field"><span class="field-label">流程中的判断范围</span><select v-model="raw.absence_scope" class="select"><option value="run">本次运行至今</option><option value="window">连续指定时长</option></select></label>
          <label class="field"><span class="field-label">连续未发生时长（秒）</span><input v-model.number="raw.duration_seconds" class="text-field" type="number" min="0.001" step="any"></label>
          <p class="inspector-lead">作为入口时按此时长计时触发。流程中连续时长不足时判为不成立，不自动等待。</p>
          <label class="field"><span class="field-label">持续未发生时</span><select v-model="raw.repeat" class="select"><option value="once_until_seen">触发一次，收到事件后重新计时</option><option value="every_window">每个时间窗口触发一次</option></select></label>
        </template>
      </template>
      <PredicateEditor v-else-if="raw.type === 'condition'" :node="raw.predicate" :rule="rule" :sources="sources" />
      <template v-else-if="raw.type === 'logic'">
        <label class="field"><span class="field-label">逻辑类型</span><select v-model="raw.op" class="select"><option value="all">AND · 全部满足</option><option value="any">OR · 任一满足</option><option value="not">NOT · 取反</option></select></label>
        <label class="field"><span class="field-label">结果取反</span><input v-model="raw.negated" type="checkbox" aria-label="逻辑结果取反"></label>
        <label v-if="raw.op === 'all'" class="field"><span class="field-label">入口事件时间窗口（秒）</span><input v-model.number="raw.within_seconds" type="number" min="0.001" step="any" class="text-field" placeholder="10"></label>
        <p class="inspector-lead">AND／OR 可连接多个条件，NOT 只连接一个条件或事件组。</p>
      </template>
      <template v-else-if="raw.type === 'set_variable'">
        <label class="field"><span class="field-label">赋值目标</span><select v-model="raw.variable" class="select" aria-label="赋值目标变量"><option value="" disabled>选择变量</option><option v-for="variable in rule.variables || []" :key="variable.id" :value="variable.id">{{ variable.name }}</option></select></label>
        <ParamInput :def="{ name: 'value', label: '赋值内容', type: 'string', value_type: 'any' }" v-model="raw.value" allow-binding :binding-sources="sources" />
      </template>
      <ActionForm v-else :action="raw" :rule="rule" :index="node.index" :binding-sources="sources" :recovery-group="recoveryGroup" :in-recovery="inRecovery"
        @replace="emit('pick', { kind: 'action', mode: 'replace', node })"
        @create-recovery="emit('operation', { operation: 'recovery-group', node })"
        @edit-recovery="editRecovery" @disconnect-recovery="disconnectRecovery" />
      <div v-if="!node.gate" class="inspector-action-row"><button class="btn btn-text" @click="duplicate"><span class="material-symbols-outlined">content_copy</span>复制</button><button class="btn btn-text danger-text" @click="remove"><span class="material-symbols-outlined">delete</span>删除节点</button></div>
    </div>
  </aside>
</template>
