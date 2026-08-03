<script setup>
import { computed, ref } from 'vue'
import { typesCompatible } from '../lib/bindings'

const props = defineProps({
  sources: { type: Array, default: () => [] },
  targetType: { type: String, default: 'any' },
})
const emit = defineEmits(['select', 'cancel'])
const selected = ref('')
const groups = computed(() => {
  const grouped = new Map()
  props.sources
    .filter(source => typesCompatible(source.type, props.targetType))
    .forEach(source => {
      if (!grouped.has(source.group)) grouped.set(source.group, [])
      grouped.get(source.group).push(source)
    })
  return [...grouped]
})
const selectedSource = computed(() => (
  props.sources.find(item => JSON.stringify(item.value) === selected.value) || null
))

function apply() {
  if (selectedSource.value) emit('select', JSON.parse(JSON.stringify(selectedSource.value.value)))
}
</script>

<template>
  <div class="binding-picker">
    <select v-model="selected" class="select">
      <option value="" disabled>选择本次运行产生的数据…</option>
      <optgroup v-for="([group, items]) in groups" :key="group" :label="group">
        <option v-for="item in items" :key="JSON.stringify(item.value)" :value="JSON.stringify(item.value)">
          {{ item.sensitive ? '【敏感】' : '' }}{{ item.label }}
        </option>
      </optgroup>
    </select>
    <!-- 插件把剪贴板等输出标为 sensitive，绑定后会把内容传给动作。 -->
    <p v-if="selectedSource?.sensitive" class="flex items-start gap-1.5 text-label-m text-warn">
      <span class="material-symbols-outlined text-[16px] leading-5">warning</span>
      <span>此数据被插件标记为敏感，可能包含隐私内容。绑定后，规则每次触发都会把它传给该动作使用。</span>
    </p>
    <div class="binding-picker-actions">
      <button type="button" class="btn btn-tonal btn-sm" :disabled="!selected" @click="apply">使用</button>
      <button type="button" class="btn btn-text btn-sm" @click="$emit('cancel')">取消</button>
    </div>
  </div>
</template>
