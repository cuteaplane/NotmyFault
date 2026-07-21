<script setup>
import { computed } from 'vue'
import { optValue, optLabel } from '../lib/utils'

const props = defineProps({ def: Object, modelValue: [String, Number, Boolean] })
const emit = defineEmits(['update:modelValue'])
const value = computed({
  get: () => props.modelValue != null ? props.modelValue : (props.def.default ?? ''),
  set: (v) => emit('update:modelValue', v)
})
const type = computed(() => props.def.type || 'string')
</script>

<template>
  <label class="field">
    <span class="field-label">{{ def.label || def.name }}</span>
    <select v-if="type === 'select'" v-model="value" class="select">
      <option v-for="o in (def.options || [])" :key="optValue(o)" :value="optValue(o)">{{ optLabel(o) }}</option>
    </select>
    <input v-else-if="type === 'number'" v-model="value" type="number" class="text-field" :placeholder="def.placeholder">
    <input v-else-if="type === 'bool'" type="checkbox" v-model="value">
    <input v-else v-model="value" type="text" class="text-field" :placeholder="def.placeholder">
  </label>
</template>
