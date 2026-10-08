<script setup>
import { computed } from 'vue'
import { store } from '../lib/store'
import { PREDICATE_LABELS, RUN_STATUS_OPTIONS, workflowEventChoices } from '../lib/workflowConditions'
import ConditionValueInput from './ConditionValueInput.vue'
defineOptions({ name: 'PredicateEditor' })
const props = defineProps({ node: Object, sources: { type: Array, default: () => [] }, rule: Object, shallow: Boolean })
const group = computed(() => ['all', 'any', 'not'].includes(props.node.op))
const statusComparison = computed({ get: () => props.node.comparison === undefined ? 'eq' : props.node.comparison, set: value => { props.node.comparison = value } })
const triggers = computed(() => workflowEventChoices(props.rule, store.schema))
const occurredExpected = computed({ get: () => props.node.expected !== false, set: value => { props.node.expected = value } })
const operandType = computed(() => ['is_true', 'is_false'].includes(props.node.op) ? 'bool' : ['gt', 'gte', 'lt', 'lte'].includes(props.node.op) ? 'number' : 'any')
const unary = computed(() => ['is_true', 'is_false', 'exists', 'not_exists'].includes(props.node.op))
function changeOperator(op) {
  const previous = JSON.parse(JSON.stringify(props.node))
  const wasGroup = ['all', 'any', 'not'].includes(previous.op)
  props.node.op = op
  if (group.value) {
    if (!Array.isArray(props.node.children)) props.node.children = [wasGroup ? { op: 'eq', left: '', right: '' } : previous]
    if (props.node.op === 'not' && props.node.children.length > 1) props.node.children = [{ op: 'all', children: props.node.children }]
    delete props.node.left; delete props.node.right
  } else if (props.node.op === 'event_occurred') {
    props.node.trigger_id ||= previous.left?.$ref?.scope === 'trigger' && triggers.value.some(trigger => trigger.id === previous.left.$ref.node) ? previous.left.$ref.node : triggers.value[0]?.id || ''
    props.node.expected = previous.op === 'is_false' ? false : props.node.expected !== false
    delete props.node.children; delete props.node.left; delete props.node.right
  } else if (props.node.op === 'run_status') {
    props.node.rule_id ||= props.rule?.rule_id || store.configData.rules[0]?.rule_id || ''
    props.node.status ||= 'running'; props.node.comparison ||= 'eq'
    delete props.node.children; delete props.node.left; delete props.node.right
  } else {
    props.node.left ??= ''; props.node.right ??= ''
    if (['is_true', 'is_false'].includes(props.node.op) && !props.node.left?.$ref && typeof props.node.left !== 'boolean') props.node.left = false
    delete props.node.children
    if (['exists', 'not_exists'].includes(props.node.op)) {
      delete props.node.right
      if (!props.node.left?.$ref) {
        props.node.left = JSON.parse(JSON.stringify(props.sources[0]?.value || { $ref: { scope: 'trigger', node: '', path: [] } }))
        if (props.sources[0]?.optional || props.sources[0]?.conditional) props.node.left.$ref.on_missing = 'error'
      }
    }
  }
  if (props.node.op !== 'event_occurred') { delete props.node.trigger_id; delete props.node.expected }
  if (props.node.op !== 'run_status') { delete props.node.rule_id; delete props.node.status; delete props.node.comparison }
}
const rules = computed(() => [...store.configData.rules.filter(rule => rule.rule_id !== props.rule?.rule_id), ...(props.rule ? [props.rule] : [])])
</script>
<template>
  <div class="predicate-editor">
    <label class="field"><span class="field-label">判断方式</span>
      <select :value="node.op" class="select" @change="changeOperator($event.target.value)">
        <option v-for="(name, op) in PREDICATE_LABELS" :key="op" :value="op">{{ name }}</option>
        <option value="event_occurred">事件是否发生</option><option value="run_status">自动化状态</option><option value="all">全部满足（AND）</option><option value="any">任一满足（OR）</option><option value="not">取反（NOT）</option>
      </select>
    </label>
    <template v-if="group">
      <p class="inspector-lead">{{ node.op === 'not' ? '将一个条件或条件组的结果取反。' : node.op === 'all' ? '所有子条件成立，整个组才成立。' : '任一子条件成立，整个组就成立。' }}</p>
      <template v-if="!shallow">
        <div v-for="(child, index) in node.children" :key="child" class="predicate-child"><PredicateEditor :node="child" :sources="sources" :rule="rule" /><button v-if="node.op !== 'not'" class="btn btn-text btn-sm danger-text" @click="node.children.splice(index, 1)">移除条件</button></div>
      </template>
      <button v-if="node.op !== 'not'" class="btn btn-text btn-sm" @click="node.children.push({ op: 'eq', left: '', right: '' })">添加条件</button>
      <button v-if="node.op !== 'not'" class="btn btn-text btn-sm" @click="node.children.push({ op: 'all', children: [{ op: 'eq', left: '', right: '' }] })">添加条件组</button>
    </template>
    <template v-else-if="node.op === 'event_occurred'">
      <label class="field"><span class="field-label">事件来源</span><select v-model="node.trigger_id" class="select"><option value="" disabled>选择事件来源</option><option v-for="trigger in triggers" :key="trigger.id" :value="trigger.id">{{ trigger.label }}</option></select></label>
      <label class="field"><span class="field-label">事件判断</span><select v-model="occurredExpected" class="select"><option :value="true">已发生</option><option :value="false">未发生</option></select></label>
      <p class="inspector-lead">判断本次流程收到的事件；执行到此条件前收到的新事件也计入。</p>
      <p v-if="!triggers.length" class="inspector-lead">请先在主流程添加触发器。</p>
    </template>
    <template v-else-if="node.op === 'run_status'">
      <label class="field"><span class="field-label">自动化</span><select v-model="node.rule_id" class="select"><option value="" disabled>选择自动化</option><option v-for="item in rules" :key="item.rule_id" :value="item.rule_id">{{ item.name }}{{ item.rule_id === rule?.rule_id ? '（本自动化）' : '' }}</option></select></label>
      <label class="field"><span class="field-label">状态比较</span><select v-model="statusComparison" class="select"><option value="eq">是</option><option value="ne">不是</option></select></label>
      <label class="field"><span class="field-label">状态</span><select v-model="node.status" class="select"><option v-for="[value, name] in RUN_STATUS_OPTIONS" :key="value" :value="value">{{ name }}</option></select></label>
      <p class="inspector-lead">正在运行与排队判断当前任务。最近一次结果只包含本次引擎启动后的记录。</p>
    </template>
    <template v-else>
      <ConditionValueInput v-model="node.left" :sources="sources" :target-type="operandType" label="判断数据" />
      <ConditionValueInput v-if="!unary" v-model="node.right" :sources="sources" :target-type="operandType" label="比较目标" />
      <p class="inspector-lead">比较引用的数据缺失时，本次运行会报错。可先使用“存在”判断，再用 AND 连接比较条件；“缺失”可单独判断数据是否可读。</p>
    </template>
  </div>
</template>
<style scoped>
.predicate-editor { display: grid; gap: 12px; }
.predicate-child { border-left: 2px solid var(--md-outline-variant); padding-left: 12px; }
</style>
