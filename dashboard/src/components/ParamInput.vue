<script setup>
import { computed, ref, watch } from 'vue'
import { optValue, optLabel } from '../lib/utils'
import { hasBridge } from '../lib/api'
import { store } from '../lib/store'
import { isReference, referenceLabel, typesCompatible } from '../lib/bindings'
import BindingPicker from './BindingPicker.vue'
import PluginDataField from './PluginDataField.vue'
import ParameterEditorButton from './ParameterEditorButton.vue'
import TypedValueInput from './TypedValueInput.vue'
import { isExpression, isLiteralValue, isEncodedValue, fieldType, typeSpec, typeLabel, defaultTypedValue, parseExactJson } from '../lib/valueTypes'

const props = defineProps({
  def: Object,
  pluginId: { type: String, default: '' },
  modelValue: [String, Number, Boolean, Object, Array],
  bindingSources: { type: Array, default: () => [] },
  allowBinding: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue'])
const value = computed({
  get: () => props.modelValue !== undefined ? props.modelValue : (props.def.default ?? ''),
  set: (v) => emit('update:modelValue', v)
})
const type = computed(() => props.def.type || 'string')
const bindingType = computed(() => fieldType(props.def, true))
const bindingOpen = ref(false)
const optionsId = `param-options-${Math.random().toString(36).slice(2)}`
const bound = computed(() => isExpression(props.modelValue))
const advanced = ref(false), expressionText = ref(''), expressionError = ref('')
const expressionControl = ref(null)
watch(expressionError, value => expressionControl.value?.setCustomValidity(value))
watch(() => props.modelValue, value => { expressionText.value = JSON.stringify(value, null, 2) ?? ''; expressionError.value = '' }, { immediate: true, deep: true })
const useTypedEditor = computed(() => !!props.def.value_type && (bindingType.value.nullable || !['text', 'path', 'time', 'any', 'union'].includes(bindingType.value.type)) || !!props.def.value_type && props.modelValue != null && typeof props.modelValue === 'object' && !bound.value || isLiteralValue(props.modelValue) || isEncodedValue(props.modelValue))
function setExpression(text) {
  expressionText.value = text
  try { const value = parseExactJson(text); emit('update:modelValue', value); expressionError.value = '' } catch (e) { expressionError.value = '表达式 JSON 无效' }
}
const boundLabel = computed(() => referenceLabel(props.modelValue, props.bindingSources))
const compatibleSources = computed(() => props.bindingSources.filter(
  source => typesCompatible(source.type, bindingType.value),
))
const parameterEditor = computed(() => store.extensions.parameter_editors.find(editor => (
  editor.plugin_id === props.pluginId
  && editor.parameter === props.def.name
  && editor.data_type === props.def.data_type
)) || null)

function useBinding(binding) {
  emit('update:modelValue', binding)
  bindingOpen.value = false
}

function useFixedValue() {
  if (props.def.value_type) { emit('update:modelValue', defaultTypedValue(bindingType.value)); return }
  const emptyValue = type.value === 'bool'
    ? false
    : type.value === 'plugin_data' ? {} : ''
  emit('update:modelValue', props.def.default ?? emptyValue)
}

async function pickFolder() {
  if (!hasBridge()) return
  try {
    const selected = await window.pywebview.api.select_folder(String(value.value || ''))
    if (selected) value.value = selected
  } catch (e) { /* bridge 不可用时保持手动输入。 */ }
}

</script>

<template>
  <div class="field">
    <span class="field-label">
      {{ def.label || def.name }}
      <span v-if="def.value_type" class="inspector-lead">{{ typeLabel(bindingType) }}</span>
      <button v-if="allowBinding && !bound && type !== 'plugin_data'" type="button" class="field-binding-button"
        :disabled="!bindingSources.length" @click="bindingOpen = !bindingOpen">
        <span class="material-symbols-outlined">data_object</span>使用变量或数据
      </button>
    </span>
    <div v-if="bound && !allowBinding" class="plugin-data-error">
      <span class="material-symbols-outlined">link_off</span>
      <span>这项参数只允许使用固定值。</span>
      <button type="button" class="btn btn-text btn-sm" @click="useFixedValue">改为固定值</button>
    </div>
    <div v-else-if="bound && type === 'plugin_data'" class="plugin-data-error">
      <span class="material-symbols-outlined">link_off</span>
      <span>这项数据由插件自己管理，不能使用运行数据。</span>
      <button type="button" class="btn btn-text btn-sm" @click="useFixedValue">移除绑定</button>
    </div>
    <div v-else-if="bound" class="binding-value">
      <span class="material-symbols-outlined">link</span>
      <span>{{ boundLabel }}</span>
      <button type="button" class="btn btn-text btn-sm" @click="bindingOpen = !bindingOpen">更换</button>
      <button type="button" class="btn btn-text btn-sm" @click="useFixedValue">改为固定值</button>
    </div>
    <BindingPicker v-if="bindingOpen" :sources="bindingSources" :target-type="bindingType"
      @select="useBinding" @cancel="bindingOpen = false" />
    <TypedValueInput v-else-if="!bound && useTypedEditor && type !== 'plugin_data'" :model-value="props.modelValue" :value-type="bindingType" :label="def.label || def.name" @update:model-value="emit('update:modelValue', $event)" />
    <PluginDataField v-else-if="!bound && type === 'plugin_data' && parameterEditor" :editor="parameterEditor"
      :model-value="props.modelValue" :sensitive="props.def.sensitive === true"
      @update:model-value="emit('update:modelValue', $event)" />
    <p v-else-if="!bound && type === 'plugin_data'" class="plugin-data-error">
      自动化引擎运行后可以编辑这项插件数据。
    </p>
    <select v-else-if="!bound && type === 'select'" v-model="value" class="select">
      <option v-for="o in (def.options || [])" :key="optValue(o)" :value="optValue(o)">{{ optLabel(o) }}</option>
    </select>
    <input v-else-if="!bound && type === 'time'" v-model="value" type="time" class="text-field">
    <div v-else-if="!bound && type === 'hotkey'" class="hotkey-input">
      <input v-model="value" class="text-field" :placeholder="def.placeholder || '如 Ctrl+Shift+A'">
      <ParameterEditorButton v-if="parameterEditor" :editor="parameterEditor" :model-value="props.modelValue"
        @update:model-value="emit('update:modelValue', $event)" />
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
    <input v-else-if="!bound" v-model="value" type="text" class="text-field" :placeholder="def.placeholder" :list="def.options?.length ? optionsId : undefined">
    <datalist v-if="def.options?.length && type !== 'select'" :id="optionsId"><option v-for="option in def.options" :key="optValue(option)" :value="optValue(option)">{{ optLabel(option) }}</option></datalist>
    <ParameterEditorButton v-if="!bound && parameterEditor && !['plugin_data', 'hotkey'].includes(type)"
      :editor="parameterEditor" :model-value="props.modelValue"
      @update:model-value="emit('update:modelValue', $event)" />
    <details v-if="allowBinding && type !== 'plugin_data'" class="expression-editor" @toggle="advanced = $event.target.open">
      <summary>表达式 JSON</summary>
      <textarea v-if="advanced" ref="expressionControl" :value="expressionText" class="text-field textarea-field" rows="5" aria-label="参数表达式 JSON" @input="setExpression($event.target.value)" />
      <p v-if="expressionError" class="danger-text">{{ expressionError }}</p>
      <p class="inspector-lead">可使用 $ref、$convert 和 $template；用 { "$literal": 值 } 保存包含表达式符号的原始数据。</p>
    </details>
  </div>
</template>
