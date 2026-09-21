<script setup>
import { nextTick, ref, watch } from 'vue'

const props = defineProps({
  open: Boolean,
  closable: { type: Boolean, default: true },
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

// 内容组件可以先聚焦自己的输入框。
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
      || [...(backdrop.value?.querySelectorAll(focusableSelector) || [])].find(isVisibleControl)
      || backdrop.value
    target?.focus({ preventScroll: true })
  }
})

function isVisibleControl(item) {
  if (item.closest('[hidden], [inert], [aria-hidden="true"]')) return false
  for (let element = item; element && element !== backdrop.value; element = element.parentElement) {
    const style = getComputedStyle(element)
    if (style.display === 'none' || style.visibility === 'hidden') return false
    if (element.tagName === 'DETAILS' && !element.open && !element.querySelector('summary')?.contains(item)) return false
  }
  return true
}

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
    .filter(isVisibleControl)
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
      tabindex="-1" @pointerdown.self="tryClose" @keydown.stop="handleKeydown">
      <slot />
    </div>
  </Transition>
</template>
