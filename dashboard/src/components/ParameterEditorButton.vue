<script setup>
import { computed, nextTick, ref } from 'vue'
import { invokeExtensionCommand } from '../lib/api'
import { store } from '../lib/store'
import ExtensionViewPage from './ExtensionViewPage.vue'

const props = defineProps({
  editor: { type: Object, required: true },
  modelValue: { type: [Object, Array, String, Number, Boolean], default: null },
})
const emit = defineEmits(['update:modelValue'])
const busy = ref(false)
const error = ref('')
const viewOpen = ref(false)
const sessionId = ref('')
const viewId = ref('')
const viewState = ref(null)
const launcherRef = ref(null)

const view = computed(() => store.extensions.views.find(item => (
  item.plugin_id === props.editor.plugin_id && item.id === viewId.value
)) || null)

async function invokeEditor() {
  if (busy.value) return
  busy.value = true
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
      error.value = response?.error || '插件编辑器没有成功完成操作。'
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
    error.value = reason.message || '插件编辑器没有成功完成操作。'
  } finally {
    busy.value = false
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
  <span class="parameter-editor-button">
    <button ref="launcherRef" type="button" class="btn btn-tonal btn-sm" :disabled="busy"
      :title="editor.ui?.description || editor.title" @click="invokeEditor">
      <span class="material-symbols-outlined">{{ editor.ui?.icon || 'extension' }}</span>
      {{ busy ? (editor.ui?.busy_label || '处理中…') : (editor.ui?.label || editor.title || '编辑') }}
    </button>
    <small v-if="error" class="plugin-data-error">{{ error }}</small>
    <ExtensionViewPage :open="viewOpen" :plugin-id="editor.plugin_id" :view-id="viewId"
      :title="view?.title || editor.title || '插件编辑器'" :session-id="sessionId"
      :initial-state="viewState" :window-controls="view?.window_controls || []"
      @commit="commit" @close="closeView" />
  </span>
</template>
