<script setup>
import { computed, ref, watch } from 'vue'
import { isLiteralValue, isEncodedValue, typeLabel } from '../lib/valueTypes'
import { typesCompatible, referenceLabel } from '../lib/bindings'
import ParamInput from './ParamInput.vue'
const props = defineProps({ modelValue: null, sources: { type: Array, default: () => [] }, label: String, targetType: { type: String, default: 'any' }, allowLiteral: { type: Boolean, default: true } })
const emit = defineEmits(['update:modelValue'])
const categories = computed(() => [['trigger', '本流程触发器输出'], ['step', '前序动作输出'], ['variable', '运行变量'], ['constant', '规则常量'], ['trigger_config', '触发器配置'], ...(props.allowLiteral ? [['literal', '固定值']] : [])])
const category = ref('')
watch(() => [props.modelValue, props.allowLiteral], ([value]) => { category.value = value?.$ref?.scope || (props.allowLiteral ? 'literal' : '') }, { immediate: true })
const matching = computed(() => props.sources.filter(source => source.value.$ref.scope === category.value && typesCompatible(source.type, props.targetType)))
const referenceKey = source => JSON.stringify({ scope: source.value.$ref.scope, node: source.value.$ref.node, path: source.value.$ref.path })
const selected = computed(() => matching.value.find(source => referenceKey(source) === JSON.stringify(props.modelValue?.$ref && { scope: props.modelValue.$ref.scope, node: props.modelValue.$ref.node, path: props.modelValue.$ref.path })))
const literalType = computed(() => {
  const value = isLiteralValue(props.modelValue) ? props.modelValue.$literal : props.modelValue
  if (isEncodedValue(value)) return value.$nmf_value.type
  return typeof value === 'boolean' ? 'bool' : typeof value === 'number' ? 'number' : Array.isArray(value) ? 'array' : value && typeof value === 'object' ? 'object' : 'string'
})
function sourceValue(source) {
  const value = JSON.parse(JSON.stringify(source.value))
  if (source.optional || source.conditional) value.$ref.on_missing = 'error'
  return value
}
function changeSource() {
  const value = category.value === 'literal' ? (props.targetType === 'bool' ? false : props.targetType === 'number' ? 0 : '') : matching.value[0] ? sourceValue(matching.value[0]) : undefined
  emit('update:modelValue', value === undefined ? { $ref: { scope: category.value, node: '', path: [] } } : JSON.parse(JSON.stringify(value)))
}
function selectField(key) {
  const source = matching.value.find(item => referenceKey(item) === key)
  if (source) emit('update:modelValue', sourceValue(source))
}
function setType(type) { emit('update:modelValue', type === 'number' ? 0 : type === 'bool' ? false : type === 'object' ? {} : type === 'array' ? [] : '') }
</script>
<template>
  <div class="condition-value-input">
    <label class="field"><span class="field-label">{{ label }}来源</span>
      <select v-model="category" class="select" required @change="changeSource"><option v-if="!allowLiteral" value="" disabled>选择要判断的数据来源</option><option v-for="[value, name] in categories" :key="value" :value="value">{{ name }}</option></select>
    </label>
    <template v-if="category && category !== 'literal'">
      <label class="field"><span class="field-label">{{ category === 'variable' || category === 'constant' ? '名称' : '输出字段' }}</span>
        <select class="select" required :value="selected ? referenceKey(selected) : ''" @change="selectField($event.target.value)">
          <option value="" disabled>{{ modelValue?.$ref?.node && !selected ? `${referenceLabel(modelValue, sources)} · 数据不可用或类型不匹配` : `选择${label}` }}</option><option v-for="source in matching" :key="referenceKey(source)" :value="referenceKey(source)">{{ source.label }} · {{ typeLabel(source.type) }}</option>
        </select>
      </label>
      <p v-if="!matching.length" class="inspector-lead">{{ targetType === 'bool' ? '此来源没有可用于布尔判断的数据。判断事件是否发生，请直接连接触发器图块。' : '没有符合当前判断类型的数据。请添加对应的输出、常量或变量。' }}</p>
      <p v-if="category === 'trigger'" class="inspector-lead">读取本次运行收到的触发器数据；未命中的触发器可能没有数据。</p>
    </template>
    <template v-else-if="category === 'literal'">
      <label v-if="targetType === 'any' || !typesCompatible(literalType, targetType)" class="field"><span class="field-label">固定值类型</span><select class="select" :value="literalType" @change="setType($event.target.value)"><option v-if="typesCompatible(literalType, targetType) && !['string', 'number', 'bool', 'object', 'array'].includes(literalType)" :value="literalType">{{ typeLabel(literalType) }}</option><option v-if="targetType === 'any'" value="string">文本</option><option v-if="targetType !== 'bool'" value="number">数字</option><option v-if="targetType !== 'number'" value="bool">布尔值</option><option v-if="targetType === 'any'" value="object">对象</option><option v-if="targetType === 'any'" value="array">数组</option></select></label>
      <p v-if="!typesCompatible(literalType, targetType)" class="danger-text">当前固定值类型不适用于此判断，请选择匹配的类型。</p>
      <label v-if="literalType === 'bool'" class="field"><span class="field-label">{{ label }} · 布尔值</span><select class="select" :value="isLiteralValue(modelValue) ? modelValue.$literal : modelValue" @change="emit('update:modelValue', $event.target.value === 'true')"><option :value="true">真（true）</option><option :value="false">假（false）</option></select></label>
      <ParamInput v-else :def="{ name: label, label, type: literalType, value_type: literalType }" :model-value="modelValue" @update:model-value="emit('update:modelValue', $event)" />
    </template>
    <p v-else class="inspector-lead">选择变量或输出数据进行判断；判断触发事件，请直接连接触发器图块。</p>
  </div>
</template>
<style scoped>
.condition-value-input { display: grid; gap: 10px; }
</style>
