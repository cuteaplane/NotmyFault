<script setup>
import { nextTick, ref, watch } from 'vue'

const props = defineProps({
  open: Boolean,
  // false 时禁止点遮罩和按 ESC 关闭，比如确认框和录制中控件选择
  closable: { type: Boolean, default: true },
  // 盖在其他弹窗上面时用，比如桌面录制从插件弹窗里唤起
  layerTop: { type: Boolean, default: false },
})
const emit = defineEmits(['close'])

const backdrop = ref(null)

// 焦点还在弹窗外才移到遮罩，保证 ESC 能冒泡到这里；内容组件自己聚焦了输入框就不抢。
watch(() => props.open, async open => {
  if (!open) return
  await nextTick()
  if (!backdrop.value?.contains(document.activeElement)) backdrop.value?.focus()
})

function tryClose() {
  if (props.closable) emit('close')
}
</script>

<template>
  <Transition name="nmf-dialog">
    <div v-if="open" ref="backdrop" class="nmf-dialog-backdrop" :class="{ 'layer-top': layerTop }"
      tabindex="-1" @pointerdown.self="tryClose" @keydown.esc="tryClose">
      <slot />
    </div>
  </Transition>
</template>
