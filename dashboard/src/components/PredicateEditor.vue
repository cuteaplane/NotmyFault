<script setup>
import { computed } from 'vue'
import { store } from '../lib/store'
import { PREDICATE_LABELS, RUN_STATUS_OPTIONS } from '../lib/workflowConditions'
import ConditionValueInput from './ConditionValueInput.vue'
import { typesCompatible } from '../lib/bindings'
defineOptions({ name: 'PredicateEditor' })
const props = defineProps({ node: Object, sources: { type: Array, default: () => [] }, rule: Object })
const statusComparison = computed({ get: () => props.node.comparison || 'eq', set: value => { props.node.comparison = value } })
const operandType = computed(() => ['is_true', 'is_false'].includes(props.node.op) ? 'bool' : ['gt', 'gte', 'lt', 'lte'].includes(props.node.op) ? 'number' : 'any')
const unary = computed(() => ['is_true', 'is_false', 'exists', 'not_exists'].includes(props.node.op))
function changeOperator(op) {
  props.node.op = op
  if (op === 'run_status') {
    props.node.rule_id ||= props.rule?.rule_id || store.configData.rules[0]?.rule_id || ''
    props.node.status ||= 'running'; props.node.comparison ||= 'eq'
    delete props.node.left; delete props.node.right
  } else {
    props.node.left ??= ''; props.node.right ??= ''
    const source = props.sources.find(source => typesCompatible(source.type, operandType.value))
    if (!props.node.left?.$ref) {
      props.node.left = JSON.parse(JSON.stringify(source?.value || { $ref: { scope: 'step', node: '', path: [] } }))
      if (source?.optional || source?.conditional) props.node.left.$ref.on_missing = 'error'
    }
    if (unary.value) delete props.node.right
    if (['exists', 'not_exists'].includes(props.node.op)) {
      if (!props.node.left?.$ref) {
        props.node.left = JSON.parse(JSON.stringify(props.sources[0]?.value || { $ref: { scope: 'trigger', node: '', path: [] } }))
        if (props.sources[0]?.optional || props.sources[0]?.conditional) props.node.left.$ref.on_missing = 'error'
      }
    }
  }
  if (props.node.op !== 'run_status') { delete props.node.rule_id; delete props.node.status; delete props.node.comparison }
}
const rules = computed(() => [...store.configData.rules.filter(rule => rule.rule_id !== props.rule?.rule_id), ...(props.rule ? [props.rule] : [])])
</script>
<template>
  <div class="predicate-editor">
    <label class="field"><span class="field-label">判断方式</span>
      <select :value="node.op" class="select" @change="changeOperator($event.target.value)">
        <option v-for="(name, op) in PREDICATE_LABELS" :key="op" :value="op">{{ name }}</option>
        <option value="run_status">自动化状态</option>
      </select>
    </label>
    <template v-if="node.op === 'run_status'">
      <label class="field"><span class="field-label">自动化</span><select v-model="node.rule_id" class="select"><option value="" disabled>选择自动化</option><option v-for="item in rules" :key="item.rule_id" :value="item.rule_id">{{ item.name }}{{ item.rule_id === rule?.rule_id ? '（本自动化）' : '' }}</option></select></label>
      <label class="field"><span class="field-label">状态比较</span><select v-model="statusComparison" class="select"><option value="eq">是</option><option value="ne">不是</option></select></label>
      <label class="field"><span class="field-label">状态</span><select v-model="node.status" class="select"><option v-for="[value, name] in RUN_STATUS_OPTIONS" :key="value" :value="value">{{ name }}</option></select></label>
      <p class="inspector-lead">正在运行与排队判断当前任务。最近一次结果只包含本次引擎启动后的记录。</p>
    </template>
    <template v-else>
      <ConditionValueInput v-model="node.left" :sources="sources" :target-type="operandType" :allow-literal="false" label="判断数据" />
      <ConditionValueInput v-if="!unary" v-model="node.right" :sources="sources" :target-type="operandType" label="比较目标" />
      <p v-if="node.left?.$ref && !['exists', 'not_exists'].includes(node.op)" class="inspector-lead">判断引用的数据缺失时，本次运行会报错。可先使用“存在”判断，再用 AND 连接比较条件。</p>
    </template>
  </div>
</template>
<style scoped>
.predicate-editor { display: grid; gap: 12px; }
</style>
