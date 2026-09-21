<script setup>
import { useParameterEditor } from '../composables/useParameterEditor'
import ExtensionViewPage from './ExtensionViewPage.vue'

const props = defineProps({
  editor: { type: Object, required: true },
  modelValue: { type: [Object, Array, String, Number, Boolean], default: null },
})
const emit = defineEmits(['update:modelValue'])
const { busy, error, viewOpen, sessionId, viewId, viewState, launcherRef, view, invokeEditor, commit, closeView } = useParameterEditor(props, emit)
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
