<script setup>
import { computed, ref } from 'vue'
import { optValue, optLabel } from '../lib/utils'
import { hasBridge, invokeComponent } from '../lib/api'
import { store } from '../lib/store'
import { isReference, referenceLabel, typesCompatible } from '../lib/bindings'
import BindingPicker from './BindingPicker.vue'
import PluginDataField from './PluginDataField.vue'

const props = defineProps({
  def: Object,
  pluginId: { type: String, default: '' },
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
const bindingType = computed(() => props.def.value_type || type.value)
const recording = ref(false)
const bindingOpen = ref(false)
const selectingElement = ref(false)
const checkingElement = ref(false)
const elementCountdown = ref(0)
const elementStatus = ref('')
const bound = computed(() => isReference(props.modelValue))
const boundLabel = computed(() => referenceLabel(props.modelValue, props.bindingSources))
const compatibleSources = computed(() => props.bindingSources.filter(
  source => typesCompatible(source.type, bindingType.value),
))
// 参数类型 → 采集组件，由插件声明 param_types 驱动，不写死任何插件。
const captureComponent = computed(() => {
  if (!type.value) return null
  return store.components.find(component => (
    component.available
    && component.plugin_id === props.pluginId
    && (component.param_types || []).includes(type.value)
  )) || null
})
const dataEditor = computed(() => store.extensions.parameter_editors.find(editor => (
  editor.plugin_id === props.pluginId
  && editor.parameter === props.def.name
  && editor.data_type === props.def.data_type
)) || null)

function useBinding(binding) {
  emit('update:modelValue', binding)
  bindingOpen.value = false
}

function useFixedValue() {
  const emptyValue = type.value === 'bool'
    ? false
    : ['uia_selector', 'plugin_data'].includes(type.value) ? {} : ''
  emit('update:modelValue', props.def.default ?? emptyValue)
}

const elementSelected = computed(() => type.value === 'uia_selector'
  && props.modelValue
  && typeof props.modelValue === 'object'
  && props.modelValue.version === 1)
const elementDisplay = computed(() => {
  const selector = props.modelValue || {}
  const display = selector.display || {}
  const target = selector.target || {}
  const windowInfo = selector.window || {}
  return {
    control: display.control || target.name || '未命名控件',
    controlType: display.control_type || '屏幕控件',
    window: display.window || windowInfo.name || '未命名窗口',
    app: display.app || windowInfo.process || '未知程序',
  }
})

async function recordHotkey() {
  const component = captureComponent.value
  if (!component || recording.value) return
  recording.value = true
  try {
    const result = await invokeComponent(
      component.plugin_id, component.id, 'capture', { timeout_seconds: 15 },
    )
    const data = result?.data?.data || {}
    if (result?.ok && data.hotkey) {
      value.value = data.hotkey
    } else if (result?.ok && data.cancelled) {
      elementStatus.value = '已取消录制。'
    } else if (result?.ok && data.timed_out) {
      elementStatus.value = '没有等到按键，请再试一次。'
    } else {
      elementStatus.value = result?.error || '录制热键失败。'
    }
  } catch (error) {
    elementStatus.value = error.message || '录制热键失败。'
  } finally {
    recording.value = false
  }
}

async function pickFolder() {
  if (!hasBridge()) return
  try {
    const selected = await window.pywebview.api.select_folder(String(value.value || ''))
    if (selected) value.value = selected
  } catch (e) { /* bridge 不可用时保持手动输入。 */ }
}

async function pickDesktopElement() {
  const component = captureComponent.value
  if (!component || selectingElement.value) return
  selectingElement.value = true
  elementCountdown.value = 3
  elementStatus.value = '把鼠标移到目标控件上，不需要点击。'
  const timer = window.setInterval(() => {
    elementCountdown.value = Math.max(0, elementCountdown.value - 1)
  }, 1000)
  try {
    const result = await invokeComponent(
      component.plugin_id, component.id, 'capture', { delay_seconds: 3 },
    )
    const selector = result?.data?.data?.selector
    if (!result?.ok || !selector) {
      elementStatus.value = result?.error || '没有读到屏幕控件，请再试一次。'
      return
    }
    value.value = selector
    elementStatus.value = '已读取控件；保存前可以检查一次。'
  } catch (error) {
    elementStatus.value = error.message || '读取屏幕控件失败。'
  } finally {
    window.clearInterval(timer)
    elementCountdown.value = 0
    selectingElement.value = false
  }
}

async function verifyDesktopElement() {
  const component = captureComponent.value
  if (!component || !elementSelected.value || checkingElement.value) return
  checkingElement.value = true
  elementStatus.value = '正在重新查找这个控件…'
  try {
    const result = await invokeComponent(
      component.plugin_id, component.id, 'check', { selector: props.modelValue },
    )
    const data = result?.data?.data || {}
    elementStatus.value = result?.ok && data.ok
      ? '检查通过，现在仍能找到这个控件。'
      : (result?.error || data.error || '现在找不到这个控件，请重新选择。')
  } catch (error) {
    elementStatus.value = error.message || '检查屏幕控件失败。'
  } finally {
    checkingElement.value = false
  }
}

function clearDesktopElement() {
  value.value = {}
  elementStatus.value = ''
}
</script>

<template>
  <div class="field">
    <span class="field-label">
      {{ def.label || def.name }}
      <button v-if="allowBinding && !bound && type !== 'plugin_data'" type="button" class="field-binding-button"
        :disabled="!compatibleSources.length" @click="bindingOpen = !bindingOpen">
        <span class="material-symbols-outlined">data_object</span>使用运行数据
      </button>
    </span>
    <div v-if="bound && type === 'plugin_data'" class="plugin-data-error">
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
    <PluginDataField v-else-if="!bound && dataEditor" :editor="dataEditor"
      :model-value="props.modelValue" @update:model-value="emit('update:modelValue', $event)" />
    <p v-else-if="!bound && type === 'plugin_data'" class="plugin-data-error">
      自动化引擎运行后可以编辑这项插件数据。
    </p>
    <select v-else-if="!bound && type === 'select'" v-model="value" class="select">
      <option v-for="o in (def.options || [])" :key="optValue(o)" :value="optValue(o)">{{ optLabel(o) }}</option>
    </select>
    <input v-else-if="!bound && type === 'time'" v-model="value" type="time" class="text-field">
    <div v-else-if="!bound && type === 'hotkey'" class="hotkey-input">
      <input v-model="value" class="text-field" :placeholder="def.placeholder || '如 Ctrl+Shift+A'">
      <button v-if="captureComponent" type="button" class="btn btn-tonal btn-sm"
        :disabled="recording" @click="recordHotkey">
        <span class="material-symbols-outlined">keyboard</span>{{ recording ? '请按快捷键…' : '录制' }}
      </button>
    </div>
    <div v-else-if="!bound && type === 'path'" class="path-input">
      <input v-model="value" type="text" class="text-field" :placeholder="def.placeholder || '选择或输入文件夹路径'">
      <button v-if="hasBridge()" type="button" class="btn btn-tonal btn-sm" @click="pickFolder">
        <span class="material-symbols-outlined">folder_open</span>选择
      </button>
    </div>
    <div v-else-if="!bound && type === 'uia_selector'" class="uia-selector-field">
      <div v-if="elementSelected" class="uia-selector-card">
        <span class="uia-selector-mark material-symbols-outlined">ads_click</span>
        <span class="uia-selector-copy">
          <b>{{ elementDisplay.control }}</b>
          <small>{{ elementDisplay.controlType }} · {{ elementDisplay.app }}</small>
          <small>{{ elementDisplay.window }}</small>
        </span>
        <button v-if="captureComponent?.vue" type="button" class="btn btn-text btn-sm" @click="componentPageOpen = true">
          <span class="material-symbols-outlined">open_in_new</span>查看录制详情
        </button>
        <button v-if="captureComponent" type="button" class="btn btn-text btn-sm" :disabled="checkingElement" @click="verifyDesktopElement">
          {{ checkingElement ? '检查中' : '检查' }}
        </button>
      </div>
      <div v-else class="uia-selector-empty">
        <span class="material-symbols-outlined">select_window</span>
        <span><b>还没选择控件</b><small>NotmyFault 会保存控件和窗口特征，不会保存鼠标坐标。</small></span>
      </div>
      <div class="uia-selector-actions">
        <button v-if="captureComponent" type="button" class="btn btn-tonal btn-sm" :disabled="selectingElement" @click="pickDesktopElement">
          <span class="material-symbols-outlined">center_focus_strong</span>
          {{ selectingElement ? `${elementCountdown || '正在'} 秒后读取` : (elementSelected ? '重新选择' : '选择屏幕上的控件') }}
        </button>
        <button v-if="elementSelected" type="button" class="btn btn-text btn-sm danger-text" @click="clearDesktopElement">清除</button>
      </div>
      <p v-if="elementStatus" class="uia-selector-status">{{ elementStatus }}</p>
      <p v-else-if="!captureComponent" class="uia-selector-status">自动化引擎运行后可以选择屏幕控件。</p>
    </div>
    <textarea v-else-if="!bound && type === 'textarea'" v-model="value" class="text-field textarea-field" :placeholder="def.placeholder" :rows="def.rows || 5" />
    <input v-else-if="!bound && type === 'number'" v-model.number="value" type="number" class="text-field" :placeholder="def.placeholder" :min="def.min" :max="def.max" :step="def.step">
    <input v-else-if="!bound && type === 'bool'" type="checkbox" v-model="value">
    <input v-else-if="!bound" v-model="value" type="text" class="text-field" :placeholder="def.placeholder">
  </div>
</template>
