<script setup>
import { computed, watch } from 'vue'
import { store } from '../lib/store'
import { ensureParams, getVisibleParamDefs } from '../lib/utils'
import ParamInput from './ParamInput.vue'

const props = defineProps({
  node: { type: Object, required: true },
  rule: { type: Object, default: null },
  sources: { type: Array, default: () => [] },
})

const meta = computed(() => store.schema.triggers.rule_finished)
watch(() => props.node, ensureParams, { immediate: true })

watch(
  () => props.rule?.rule_id,
  (ruleId) => {
    if (!props.node?.params || typeof props.node.params !== 'object') return
    const next = String(ruleId || '')
    if (props.node.params.owner_rule_id !== next) {
      props.node.params.owner_rule_id = next
    }
  },
  { immediate: true },
)

const includeSelf = computed(() => props.node.params?.include_self === true)
const currentRuleId = computed(() => String(props.rule?.rule_id || ''))

const ruleOptions = computed(() => {
  const rules = Array.isArray(store.configData?.rules) ? store.configData.rules : []
  return rules
    .filter(item => item && typeof item === 'object' && item.rule_id)
    .filter(item => includeSelf.value || item.rule_id !== currentRuleId.value)
    .map(item => ({
      value: item.rule_id,
      label: item.name ? `${item.name}（${item.rule_id}）` : item.rule_id,
    }))
})

const otherParams = computed(() => getVisibleParamDefs(meta.value, props.node.params)
  .filter(param => param.name !== 'rule_id' && param.name !== 'owner_rule_id'))

const selectedLabel = computed(() => {
  const selected = ruleOptions.value.find(option => option.value === props.node.params?.rule_id)
  if (selected) return selected.label
  const raw = String(props.node.params?.rule_id || '')
  return raw || '还没有选择规则'
})

watch(includeSelf, (allowed) => {
  if (allowed) return
  if (props.node.params?.rule_id && props.node.params.rule_id === currentRuleId.value) {
    props.node.params.rule_id = ''
  }
})
</script>

<template>
  <div class="rule-finished-editor">
    <p class="inspector-lead">当所选规则的一次运行结束时触发。默认不会监视本规则自己，以免反复启动。</p>
    <div class="field">
      <span class="field-label">监视的规则</span>
      <select v-model="node.params.rule_id" class="select" aria-label="监视的规则">
        <option disabled value="">请选择规则</option>
        <option v-for="option in ruleOptions" :key="option.value" :value="option.value">
          {{ option.label }}
        </option>
      </select>
      <p class="workflow-output-hint">当前：{{ selectedLabel }}</p>
    </div>
    <div class="param-grid">
      <ParamInput v-for="param in otherParams" :key="param.name" :def="param" plugin-id="rule_finished"
        v-model="node.params[param.name]" :allow-binding="false" :binding-sources="sources" />
    </div>
  </div>
</template>

<style scoped>
.rule-finished-editor { display: grid; gap: 12px; }
.rule-finished-editor .select { width: 100%; min-width: 0; }
</style>
