<script setup>
import { computed } from 'vue'
import { useParameterEditor } from '../composables/useParameterEditor'
import ExtensionViewPage from './ExtensionViewPage.vue'

const props = defineProps({
  editor: { type: Object, required: true },
  modelValue: { type: [Object, Array, String, Number, Boolean], default: null },
  sensitive: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue'])
const { busy: opening, error, viewOpen, sessionId, viewId, viewState, launcherRef, view, invokeEditor: openEditor, commit, closeView } = useParameterEditor(props, emit)

const summary = computed(() => {
  if (!props.modelValue || typeof props.modelValue !== 'object') return ''
  return typeof props.modelValue.summary === 'string' ? props.modelValue.summary : ''
})
const buttonText = computed(() => (
  (props.sensitive && summary.value ? '敏感数据已保存' : summary.value)
  || props.editor.ui?.empty_label || props.editor.title || '编辑'
))

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
