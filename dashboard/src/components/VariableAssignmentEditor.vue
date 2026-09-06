<script setup>
import { computed } from 'vue'
import ParamInput from './ParamInput.vue'
const props = defineProps({ action: Object, sources: { type: Array, default: () => [] } })
const variables = computed(() => props.sources.filter(source => source.value?.$ref?.scope === 'variable' && !source.value.$ref.path.length))
const target = computed(() => variables.value.find(source => source.value.$ref.node === props.action.variable))
</script>
<template>
  <div class="variable-assignment-editor">
    <label class="field"><span class="field-label">赋值给变量</span><select v-model="action.variable" class="select" aria-label="赋值目标变量"><option value="" disabled>选择变量…</option><option v-for="item in variables" :key="item.value.$ref.node" :value="item.value.$ref.node">{{ item.label }}</option></select></label>
    <p v-if="!variables.length" class="inspector-lead">先在“常量与变量”中添加运行变量。</p>
    <p v-else-if="action.variable && !target" class="danger-text">赋值目标已删除，请重新选择变量。</p>
    <ParamInput :def="{ name: 'value', label: '新值', type: 'string', value_type: target?.type || 'any' }" v-model="action.value" allow-binding :binding-sources="sources" />
    <label class="field"><span class="field-label">赋值失败时</span><select :value="action.on_error || 'stop'" class="select" @change="action.on_error = $event.target.value"><option value="stop">停止运行</option><option value="continue">保留原值并继续</option></select></label>
  </div>
</template>
<style scoped>.variable-assignment-editor { display: grid; gap: 12px; }</style>
