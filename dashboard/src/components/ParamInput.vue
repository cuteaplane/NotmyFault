<script setup>
import { computed, ref } from 'vue'
import { optValue, optLabel } from '../lib/utils'
import { hasBridge } from '../lib/api'
import { isReference, referenceLabel, typesCompatible } from '../lib/bindings'
import BindingPicker from './BindingPicker.vue'

const props = defineProps({
  def: Object,
  modelValue: [String, Number, Boolean, Object, Array],
  bindingSources: { type: Array, default: () => [] },
  allowBinding: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue'])
const value = computed({
  get: () => props.modelValue != null ? props.modelValue : (props.def.default ?? ''),
  set: (v) => emit('update:modelValue', v)
})
const type = computed(() => props.def.type || 'string')
const recording = ref(false)
const bindingOpen = ref(false)
const bound = computed(() => isReference(props.modelValue))
const boundLabel = computed(() => referenceLabel(props.modelValue, props.bindingSources))
const compatibleSources = computed(() => props.bindingSources.filter(
  source => typesCompatible(source.type, type.value),
))

function useBinding(binding) {
  emit('update:modelValue', binding)
  bindingOpen.value = false
}

function useFixedValue() {
  emit('update:modelValue', props.def.default ?? (type.value === 'bool' ? false : ''))
}

function captureHotkey(event) {
  const ignored = ['Control', 'Shift', 'Alt', 'Meta']
  if (ignored.includes(event.key)) return
  const key = event.key === ' ' ? 'Space' : (event.key.length === 1 ? event.key.toUpperCase() : event.key)
  const parts = []
  if (event.ctrlKey) parts.push('Ctrl')
  if (event.altKey) parts.push('Alt')
  if (event.shiftKey) parts.push('Shift')
  if (event.metaKey) parts.push('Win')
  parts.push(key)
  value.value = parts.join('+')
  recording.value = false
}

async function pickFolder() {
  if (!hasBridge()) return
  try {
    const selected = await window.pywebview.api.select_folder(String(value.value || ''))
    if (selected) value.value = selected
  } catch (e) { /* bridge 不可用时保留手动输入 */ }
}
</script>

<template>
  <div class="field">
    <span class="field-label">
      {{ def.label || def.name }}
      <button v-if="allowBinding && !bound" type="button" class="field-binding-button"
        :disabled="!compatibleSources.length" @click="bindingOpen = !bindingOpen">
        <span class="material-symbols-outlined">data_object</span>使用运行数据
      </button>
    </span>
    <div v-if="bound" class="binding-value">
      <span class="material-symbols-outlined">link</span>
      <span>{{ boundLabel }}</span>
      <button type="button" class="btn btn-text btn-sm" @click="bindingOpen = !bindingOpen">更换</button>
      <button type="button" class="btn btn-text btn-sm" @click="useFixedValue">改为固定值</button>
    </div>
    <BindingPicker v-if="bindingOpen" :sources="bindingSources" :target-type="type"
      @select="useBinding" @cancel="bindingOpen = false" />
    <select v-else-if="!bound && type === 'select'" v-model="value" class="select">
      <option v-for="o in (def.options || [])" :key="optValue(o)" :value="optValue(o)">{{ optLabel(o) }}</option>
    </select>
    <input v-else-if="!bound && type === 'time'" v-model="value" type="time" class="text-field">
    <div v-else-if="!bound && type === 'hotkey'" class="hotkey-input">
      <input :value="value" class="text-field" readonly
        :placeholder="recording ? '请按下组合键…' : (def.placeholder || '点击后按下组合键')"
        @focus="recording = true" @keydown.prevent.stop="captureHotkey">
      <button type="button" class="btn btn-tonal btn-sm" @click="recording = true">
        <span class="material-symbols-outlined">keyboard</span>{{ recording ? '正在录制' : '录制' }}
      </button>
    </div>
    <div v-else-if="!bound && type === 'path'" class="path-input">
      <input v-model="value" type="text" class="text-field" :placeholder="def.placeholder || '选择或输入文件夹路径'">
      <button v-if="hasBridge()" type="button" class="btn btn-tonal btn-sm" @click="pickFolder">
        <span class="material-symbols-outlined">folder_open</span>选择
      </button>
    </div>
    <textarea v-else-if="!bound && type === 'textarea'" v-model="value" class="text-field textarea-field" :placeholder="def.placeholder" :rows="def.rows || 5" />
    <input v-else-if="!bound && type === 'number'" v-model.number="value" type="number" class="text-field" :placeholder="def.placeholder" :min="def.min" :max="def.max" :step="def.step">
    <input v-else-if="!bound && type === 'bool'" type="checkbox" v-model="value">
    <input v-else-if="!bound" v-model="value" type="text" class="text-field" :placeholder="def.placeholder">
  </div>
</template>
