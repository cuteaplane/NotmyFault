<script setup>
import { computed } from 'vue'
import ParamInput from './ParamInput.vue'

defineOptions({ name: 'PredicateEditor' })
const props = defineProps({ node: Object, sources: { type: Array, default: () => [] } })
const group = computed(() => ['all', 'any', 'not'].includes(props.node.op))
function changeOperator() {
  if (group.value) {
    if (!Array.isArray(props.node.children)) props.node.children = [{ op: 'eq', left: '', right: '' }]
    if (props.node.op === 'not' && props.node.children.length > 1) {
      props.node.children = [{ op: 'all', children: props.node.children }]
    }
    delete props.node.left
    delete props.node.right
  } else {
    props.node.left ??= ''
    props.node.right ??= ''
    delete props.node.children
  }
}
function valueType(key) {
  const value = props.node[key]
  return typeof value === 'number' ? 'number' : typeof value === 'boolean' ? 'bool' : 'string'
}
function setType(key, type) {
  props.node[key] = type === 'number' ? 0 : type === 'bool' ? false : ''
}
</script>

<template>
  <div class="predicate-editor">
    <label class="field"><span class="field-label">判断方式</span>
      <select v-model="node.op" class="select" @change="changeOperator">
        <option value="eq">等于</option><option value="ne">不等于</option>
        <option value="gt">大于</option><option value="gte">大于或等于</option>
        <option value="lt">小于</option><option value="lte">小于或等于</option>
        <option value="contains">包含</option><option value="is_true">为真</option><option value="is_false">为假</option>
        <option value="all">全部满足（AND）</option><option value="any">任一满足（OR）</option><option value="not">取反（NOT）</option>
      </select>
    </label>
    <template v-if="group">
      <div v-for="(child, index) in node.children" :key="child" class="predicate-child">
        <PredicateEditor :node="child" :sources="sources" />
        <button v-if="node.op !== 'not'" class="btn btn-text btn-sm danger-text" @click="node.children.splice(index, 1)">移除条件</button>
      </div>
      <button v-if="node.op !== 'not'" class="btn btn-text btn-sm" @click="node.children.push({ op: 'eq', left: '', right: '' })">添加条件</button>
    </template>
    <template v-else>
      <div v-for="key in (['is_true', 'is_false'].includes(node.op) ? ['left'] : ['left', 'right'])" :key="key" class="predicate-value">
        <select v-if="!node[key]?.$ref" class="select" :aria-label="key === 'left' ? '比较值类型' : '目标值类型'" :value="valueType(key)" @change="setType(key, $event.target.value)">
          <option value="string">文本</option><option value="number">数字</option><option value="bool">布尔值</option>
        </select>
        <ParamInput :def="{ name: key, label: key === 'left' ? '比较值' : '目标值', type: valueType(key), value_type: 'any' }" v-model="node[key]" allow-binding :binding-sources="sources" />
      </div>
    </template>
  </div>
</template>

<style scoped>
.predicate-editor { display: grid; gap: 12px; }
.predicate-child { border-left: 2px solid var(--outline-variant); padding-left: 12px; }
.predicate-value { display: grid; gap: 8px; }
</style>
