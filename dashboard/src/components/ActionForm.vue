<script setup>
import { computed, watch } from 'vue'
import { store } from '../lib/store'
import { ensureParams, getVisibleParamDefs } from '../lib/utils'
import { buildBindingSources, buildFailureBindingSources, outputDefs, parameterAllowsBinding } from '../lib/bindings'
import ParamInput from './ParamInput.vue'
import IfActionEditor from './IfActionEditor.vue'
import VariableAssignmentEditor from './VariableAssignmentEditor.vue'
import ActionFailureSettings from './ActionFailureSettings.vue'

const props = defineProps({ action: { type: Object, required: true }, rule: { type: Object, required: true }, index: { type: Number, required: true }, bindingSources: { type: Array, default: null }, failureBindingSources: { type: Function, default: null } })
const emit = defineEmits(['replace', 'add-failure-action', 'replace-failure-action', 'remove-failure-action', 'move-failure-action'])
const meta = computed(() => store.schema.actions[props.action.type])
watch(() => props.action, action => {
  if (!['if', 'set_variable'].includes(action.type)) ensureParams(action)
}, { immediate: true })
const params = computed(() => getVisibleParamDefs(meta.value, props.action.params))
const sources = computed(() => props.bindingSources || buildBindingSources(props.rule, props.index, store.schema))
const outputHint = computed(() => outputDefs(meta.value).map(output => `${props.action.binding_id} · ${output.label || output.name}`).join('　'))
const failureSources = index => props.failureBindingSources ? props.failureBindingSources(index) : buildFailureBindingSources(props.rule, props.index, index, store.schema)
</script>

<template>
  <IfActionEditor v-if="action.type === 'if'" :action="action" :rule="rule" :sources="sources" />
  <VariableAssignmentEditor v-else-if="action.type === 'set_variable'" :action="action" :sources="sources" />
  <template v-else>
    <div class="field field-wide"><span class="field-label">动作类型</span>
      <button class="plugin-type-button" type="button" @click="emit('replace')">
        <span class="material-symbols-outlined">play_arrow</span>
        <span>{{ meta?.name || action.type || '未选择动作' }}</span>
        <span class="material-symbols-outlined">arrow_forward</span>
      </button>
    </div>
    <div class="param-grid"><ParamInput v-for="param in params" :key="param.name" :def="param" :plugin-id="action.type"
      v-model="action.params[param.name]" :allow-binding="parameterAllowsBinding(meta, param.name)" :binding-sources="sources" /></div>
    <div v-if="outputHint" class="workflow-output-hint">后续步骤可引用：<code>{{ outputHint }}</code></div>
    <ActionFailureSettings :action="action" :meta="meta" :schema="store.schema.actions" :binding-sources="failureSources"
      @add-failure-action="emit('add-failure-action')"
      @replace-failure-action="index => emit('replace-failure-action', index)"
      @remove-failure-action="index => emit('remove-failure-action', index)"
      @move-failure-action="(index, offset) => emit('move-failure-action', index, offset)" />
  </template>
</template>
