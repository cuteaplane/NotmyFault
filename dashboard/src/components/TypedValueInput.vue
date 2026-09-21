<script setup>
import { computed, ref, watch } from 'vue'
import { formatTypedInput, isLiteralValue, parseTypedInput, typeSpec } from '../lib/valueTypes'
const props = defineProps({ modelValue: {}, valueType: { type: [String, Object], default: 'text' }, label: String })
const emit = defineEmits(['update:modelValue'])
const draft = ref(''), error = ref('')
const control = ref(null)
watch(error, value => control.value?.setCustomValidity(value))
const spec = computed(() => typeSpec(props.valueType))
const complex = computed(() => ['array', 'object', 'any', 'union'].includes(spec.value.type) || spec.value.type.includes('/'))
let emittedValue
watch(() => [props.modelValue, props.valueType], () => {
  if (emittedValue !== undefined && emittedValue === JSON.stringify([props.modelValue, props.valueType])) return
  emittedValue = undefined
  draft.value = formatTypedInput(props.modelValue, props.valueType); error.value = '' }, { immediate: true, deep: true })
function update(raw) {
  draft.value = raw
  try {
    const parsed = parseTypedInput(raw, spec.value)
    error.value = ''
    const value = isLiteralValue(props.modelValue) ? { $literal: parsed } : parsed
    emittedValue = JSON.stringify([value, props.valueType])
    emit('update:modelValue', value)
  } catch (e) { error.value = e instanceof SyntaxError ? '请输入有效的 JSON 值' : e.message }
}
</script>

<template>
  <div class="typed-value-input">
    <select v-if="spec.type === 'bool' && !spec.nullable" ref="control" :value="draft" :aria-label="label" class="select" @change="update($event.target.value)"><option value="false">false</option><option value="true">true</option></select>
    <span v-else-if="spec.type === 'null'">null</span>
    <textarea v-else-if="complex" ref="control" :value="draft" :aria-label="label" :aria-invalid="!!error" class="text-field textarea-field" rows="4" spellcheck="false" @input="update($event.target.value)" />
    <input v-else ref="control" :value="draft" :aria-label="label" :aria-invalid="!!error" class="text-field" :placeholder="spec.type === 'bytes' ? 'Base64' : spec.nullable ? '输入值或 null' : ''" @input="update($event.target.value)">
    <p v-if="error" class="danger-text" role="alert">{{ error }}</p>
  </div>
</template>
