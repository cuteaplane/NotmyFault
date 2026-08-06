<script setup>
import { nextTick, ref, watch } from 'vue'
import { dialogState, closeDialog } from '../lib/dialog'

const toneIcons = { error: 'error', warn: 'warning', info: 'info' }
const backdrop = ref(null)

// 打开后把焦点移到遮罩，ESC 的 keydown 才能冒泡到这里。
watch(() => dialogState.open, async open => {
  if (!open) return
  await nextTick()
  backdrop.value?.focus()
})
</script>

<template>
  <Transition name="picker-surface">
  <div ref="backdrop" v-if="dialogState.open" class="plugin-picker-backdrop" tabindex="-1"
    @pointerdown.self="closeDialog(dialogState.kind !== 'confirm')"
    @keydown.esc="closeDialog(dialogState.kind !== 'confirm')">
    <section class="app-dialog" role="dialog" aria-modal="true" :aria-label="dialogState.title">
      <span class="material-symbols-outlined app-dialog-ico" :class="'tone-' + dialogState.tone">{{ toneIcons[dialogState.tone] || 'info' }}</span>
      <h3 class="dialog-title">{{ dialogState.title }}</h3>
      <p class="dialog-sub">{{ dialogState.message }}</p>
      <div class="dialog-actions">
        <button v-if="dialogState.kind === 'confirm'" class="btn btn-text" @click="closeDialog(false)">取消</button>
        <button class="btn btn-filled" @click="closeDialog(true)">{{ dialogState.confirmLabel }}</button>
      </div>
    </section>
  </div>
  </Transition>
</template>
