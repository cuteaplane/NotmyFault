<script setup>
import { computed, ref } from 'vue'
import { optValue, optLabel } from '../lib/utils'
import { hasBridge } from '../lib/api'

const props = defineProps({ def: Object, modelValue: [String, Number, Boolean] })
const emit = defineEmits(['update:modelValue'])
const value = computed({
  get: () => props.modelValue != null ? props.modelValue : (props.def.default ?? ''),
  set: (v) => emit('update:modelValue', v)
})
const type = computed(() => props.def.type || 'string')
const recording = ref(false)

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
  <label class="field">
    <span class="field-label">{{ def.label || def.name }}</span>
    <select v-if="type === 'select'" v-model="value" class="select">
      <option v-for="o in (def.options || [])" :key="optValue(o)" :value="optValue(o)">{{ optLabel(o) }}</option>
    </select>
    <input v-else-if="type === 'time'" v-model="value" type="time" class="text-field">
    <div v-else-if="type === 'hotkey'" class="hotkey-input">
      <input :value="value" class="text-field" readonly
        :placeholder="recording ? '请按下组合键…' : (def.placeholder || '点击后按下组合键')"
        @focus="recording = true" @keydown.prevent.stop="captureHotkey">
      <button type="button" class="btn btn-tonal btn-sm" @click="recording = true">
        <span class="material-symbols-outlined">keyboard</span>{{ recording ? '正在录制' : '录制' }}
      </button>
    </div>
    <div v-else-if="type === 'path'" class="path-input">
      <input v-model="value" type="text" class="text-field" :placeholder="def.placeholder || '选择或输入文件夹路径'">
      <button v-if="hasBridge()" type="button" class="btn btn-tonal btn-sm" @click="pickFolder">
        <span class="material-symbols-outlined">folder_open</span>选择
      </button>
    </div>
    <textarea v-else-if="type === 'textarea'" v-model="value" class="text-field textarea-field" :placeholder="def.placeholder" :rows="def.rows || 5" />
    <input v-else-if="type === 'number'" v-model.number="value" type="number" class="text-field" :placeholder="def.placeholder" :min="def.min" :max="def.max" :step="def.step">
    <input v-else-if="type === 'bool'" type="checkbox" v-model="value">
    <input v-else v-model="value" type="text" class="text-field" :placeholder="def.placeholder">
  </label>
</template>
