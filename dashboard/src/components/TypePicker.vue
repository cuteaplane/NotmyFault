<script setup>
import { computed, ref, watch } from 'vue'
import { store } from '../lib/store'
import { typeLabels, typeSpec } from '../lib/valueTypes'
const props = defineProps({ modelValue: { type: [String, Object], default: 'text' } })
const emit = defineEmits(['update:modelValue'])
const spec = computed(() => typeSpec(props.modelValue))
const custom = computed(() => Object.entries(store.schema.data_types?.custom || {}).filter(([, type]) => type.binding === 'shared'))
const schemaText = ref(''), error = ref('')
const schemaControl = ref(null)
watch(error, value => schemaControl.value?.setCustomValidity(value))
watch(() => props.modelValue, value => { schemaText.value = JSON.stringify(typeSpec(value), null, 2); error.value = '' }, { immediate: true, deep: true })
function choose(type) {
  const presets = { timestamp: { type, unit: 'seconds' }, duration: { type, unit: 'seconds' }, array: { type, items: 'text' }, union: { type, variants: ['text', 'null'] } }
  emit('update:modelValue', presets[type] || type)
}
function editSchema(text) {
  schemaText.value = text
  try { const value = JSON.parse(text); if (!value || typeof value !== 'object' || typeof value.type !== 'string' || !value.type) throw new Error(); emit('update:modelValue', value); error.value = '' }
  catch { error.value = '请输入包含 type 的 JSON 类型声明' }
}
function setOption(key, value) { emit('update:modelValue', { ...spec.value, [key]: value }) }
</script>
<template>
  <div class="type-picker">
    <select :value="spec.type" class="select" aria-label="数据类型" @change="choose($event.target.value)">
      <option v-for="(label, id) in typeLabels" :key="id" :value="id">{{ label }}</option>
      <option v-for="([id, type]) in custom" :key="id" :value="id" :disabled="!type.available">{{ type.label }} · {{ id }}{{ type.available ? '' : '（不可用）' }}</option>
      <option v-if="!typeLabels[spec.type] && !custom.some(([id]) => id === spec.type)" :value="spec.type">{{ spec.type }}（未安装）</option>
    </select>
    <select v-if="['timestamp', 'duration'].includes(spec.type)" :value="spec.unit || 'seconds'" class="select" aria-label="时间单位" @change="setOption('unit', $event.target.value)"><option value="seconds">秒</option><option value="milliseconds">毫秒</option></select>
    <label><input type="checkbox" :checked="spec.nullable" @change="setOption('nullable', $event.target.checked)">允许空值</label>
    <details><summary>类型结构</summary><textarea ref="schemaControl" :value="schemaText" class="text-field textarea-field" rows="4" aria-label="类型结构 JSON" @input="editSchema($event.target.value)" /><p v-if="error" class="danger-text">{{ error }}</p></details>
  </div>
</template>
<style scoped>
.type-picker { display: grid; gap: 8px; }
summary { cursor: pointer; font-size: 12px; color: var(--md-on-surface-variant); }
</style>
