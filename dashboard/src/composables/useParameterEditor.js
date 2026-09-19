import { computed, nextTick, ref } from 'vue'
import { invokeExtensionCommand } from '../lib/api'
import { store } from '../lib/store'

export function useParameterEditor(props, emit) {
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
        currentValue: props.modelValue === '' ? null : props.modelValue,
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

return { busy, error, viewOpen, sessionId, viewId, viewState, launcherRef, view, invokeEditor, commit, closeView }
}
