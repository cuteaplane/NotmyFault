<script setup>
import { computed, nextTick, ref } from 'vue'
import { invokeExtensionCommand } from '../lib/api'
import { store } from '../lib/store'
import ExtensionViewPage from './ExtensionViewPage.vue'

const props = defineProps({
  editor: { type: Object, required: true },
  modelValue: { type: [Object, Array, String, Number, Boolean], default: null },
  sensitive: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue'])
const opening = ref(false)
const error = ref('')
const viewOpen = ref(false)
const sessionId = ref('')
const viewId = ref('')
const viewState = ref(null)
const launcherRef = ref(null)

const summary = computed(() => {
  if (!props.modelValue || typeof props.modelValue !== 'object') return ''
  return typeof props.modelValue.summary === 'string' ? props.modelValue.summary : ''
})
const view = computed(() => store.extensions.views.find(item => (
  item.plugin_id === props.editor.plugin_id && item.id === viewId.value
)) || null)
const buttonText = computed(() => (
  (props.sensitive && summary.value ? '敏感数据已保存' : summary.value)
  || props.editor.ui?.empty_label || props.editor.title || '编辑'
))

async function openEditor() {
  if (opening.value) return
  opening.value = true
  error.value = ''
  try {
    const response = await invokeExtensionCommand(
      props.editor.plugin_id,
      props.editor.command,
      {
        sourceKind: 'parameter_editors',
        sourceId: props.editor.id,
        currentValue: props.modelValue,
      },
    )
    if (!response?.ok) {
      error.value = response?.error || '插件编辑器没有成功打开。'
      return
    }
    if (response.value !== undefined) emit('update:modelValue', response.value)
    if (response.view) {
      sessionId.value = response.session_id || ''
      viewId.value = response.view
      viewState.value = response.state ?? null
      viewOpen.value = true
    }
  } catch (reason) {
    error.value = reason.message || '插件编辑器没有成功打开。'
  } finally {
    opening.value = false
  }
}

function commit(value) {
  emit('update:modelValue', value)
  error.value = ''
}

async function closeView() {
  viewOpen.value = false
  await nextTick()
  launcherRef.value?.focus()
}
</script>

<template>
  <div class="plugin-data-field">
    <button ref="launcherRef" type="button" class="plugin-data-control" :disabled="opening" @click="openEditor">
      <span class="plugin-data-icon material-symbols-outlined">{{ opening ? 'hourglass_empty' : (editor.ui?.icon || 'extension') }}</span>
      <span class="plugin-data-copy">
        <b>{{ opening ? '正在打开…' : buttonText }}</b>
        <small>{{ editor.ui?.description || (summary ? '打开插件编辑器查看或修改' : '由插件获取和管理这项数据') }}</small>
      </span>
      <span class="material-symbols-outlined">arrow_forward</span>
    </button>
    <p v-if="error" class="plugin-data-error"><span class="material-symbols-outlined">error</span>{{ error }}</p>
    <ExtensionViewPage :open="viewOpen" :plugin-id="editor.plugin_id" :view-id="viewId"
      :title="view?.title || editor.title || '插件编辑器'" :session-id="sessionId"
      :initial-state="viewState" :window-controls="view?.window_controls || []"
      @commit="commit" @close="closeView" />
  </div>
</template>
