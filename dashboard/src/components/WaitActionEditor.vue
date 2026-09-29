<script setup>
import { computed, watch } from 'vue'
import { store } from '../lib/store'
import { ensureParams, getVisibleParamDefs } from '../lib/utils'
import { parameterAllowsBinding } from '../lib/bindings'
import ParamInput from './ParamInput.vue'

const props = defineProps({ action: { type: Object, required: true }, sources: { type: Array, default: () => [] } })
const meta = computed(() => store.schema.actions.wait)
watch(() => props.action, action => ensureParams(action), { immediate: true })
const params = computed(() => getVisibleParamDefs(meta.value, props.action.params))
const summary = computed(() => {
  const seconds = Number(props.action.params?.seconds ?? 0) || 0
  const milliseconds = Number(props.action.params?.milliseconds ?? 0) || 0
  const total = seconds + milliseconds / 1000
  if (!(total > 0)) return '时长为 0 时会立即继续'
  if (milliseconds > 0 && seconds > 0) return `将等待 ${seconds} 秒 + ${milliseconds} 毫秒`
  if (milliseconds > 0) return `将等待 ${milliseconds} 毫秒`
  return `将等待 ${seconds} 秒`
})
</script>

<template>
  <div class="wait-action-editor">
    <p class="inspector-lead">暂停一段时间后再继续后续动作，规则停止时可以中途取消。</p>
    <div class="param-grid">
      <ParamInput v-for="param in params" :key="param.name" :def="param" plugin-id="wait"
        v-model="action.params[param.name]" :allow-binding="parameterAllowsBinding(meta, param.name)"
        :binding-sources="sources" />
    </div>
    <p class="workflow-output-hint">{{ summary }}</p>
  </div>
</template>

<style scoped>
.wait-action-editor { display: grid; gap: 12px; }
</style>
