<script setup>
import { computed, watch } from 'vue'
import { store } from '../lib/store'
import { ensureParams, getVisibleParamDefs } from '../lib/utils'
import ParamInput from './ParamInput.vue'
import RuleFinishedTriggerEditor from './RuleFinishedTriggerEditor.vue'

const props = defineProps({
  node: { type: Object, required: true },
  rule: { type: Object, default: null },
  sources: { type: Array, default: () => [] },
})
const emit = defineEmits(['replace'])
const meta = computed(() => store.schema.triggers[props.node.type])
watch(() => props.node, ensureParams, { immediate: true })
const params = computed(() => getVisibleParamDefs(meta.value, props.node.params))
</script>

<template>
  <div class="field field-wide"><span class="field-label">触发方式</span>
    <button class="plugin-type-button" type="button" @click="emit('replace')">
      <span class="material-symbols-outlined">bolt</span>
      <span>{{ meta?.name || node.type || '未选择触发器' }}</span>
      <span class="material-symbols-outlined">arrow_forward</span>
    </button>
  </div>
  <RuleFinishedTriggerEditor v-if="node.type === 'rule_finished'" :node="node" :rule="rule" :sources="sources" />
  <div v-else class="param-grid"><ParamInput v-for="param in params" :key="param.name" :def="param" :plugin-id="node.type"
    v-model="node.params[param.name]" :allow-binding="param.type !== 'plugin_data'" :binding-sources="sources" /></div>
</template>
