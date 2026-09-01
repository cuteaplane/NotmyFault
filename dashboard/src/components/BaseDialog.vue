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
let previousFocus = null

const focusableSelector = [
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  'a[href]',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

// 焦点还在弹窗外才移到遮罩，保证 ESC 能冒泡到这里；内容组件自己聚焦了输入框就不抢。
watch(() => props.open, async open => {
  if (!open) {
    await nextTick()
    previousFocus?.isConnected && previousFocus.focus?.({ preventScroll: true })
    previousFocus = null
    return
  }
  previousFocus = document.activeElement
  await nextTick()
  if (!backdrop.value?.contains(document.activeElement)) {
    const target = backdrop.value?.querySelector('[autofocus]')
      || backdrop.value?.querySelector(focusableSelector)
      || backdrop.value
    target?.focus({ preventScroll: true })
  }
})

function tryClose() {
  if (props.closable) emit('close')
}

function handleKeydown(event) {
  if (event.key === 'Escape') {
    tryClose()
    return
  }
  if (event.key !== 'Tab' || !backdrop.value) return
  const items = [...backdrop.value.querySelectorAll(focusableSelector)]
    .filter(item => !item.hidden && item.getAttribute('aria-hidden') !== 'true')
  if (!items.length) {
    event.preventDefault()
    backdrop.value.focus()
    return
  }
  const first = items[0]
  const last = items.at(-1)
  if (event.shiftKey && (document.activeElement === first || !backdrop.value.contains(document.activeElement))) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}
</script>

<template>
  <Transition name="nmf-dialog">
    <div v-if="open" ref="backdrop" class="nmf-dialog-backdrop" :class="{ 'layer-top': layerTop }"
      tabindex="-1" @pointerdown.self="tryClose" @keydown="handleKeydown">
      <slot />
    </div>
  </Transition>
</template>
