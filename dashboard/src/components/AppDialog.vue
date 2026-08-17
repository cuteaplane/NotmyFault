<script setup>
import BaseDialog from './BaseDialog.vue'
import { dialogState, closeDialog } from '../lib/dialog'

const toneIcons = { error: 'error', warn: 'warning', info: 'info' }
</script>

<template>
  <BaseDialog :open="dialogState.open" :closable="dialogState.kind === 'alert'" @close="closeDialog(true)">
    <section class="app-dialog" role="dialog" aria-modal="true" :aria-label="dialogState.title">
      <span class="material-symbols-outlined app-dialog-ico" :class="'tone-' + dialogState.tone">{{ dialogState.kind === 'password' ? 'encrypted' : (toneIcons[dialogState.tone] || 'info') }}</span>
      <h3 class="dialog-title">{{ dialogState.title }}</h3>
      <p class="dialog-sub">{{ dialogState.message }}</p>
      <input v-if="dialogState.kind === 'password'" v-model="dialogState.inputValue"
        class="text-field app-dialog-password" type="password" autocomplete="current-password"
        placeholder="输入签名私钥密码" autofocus @keydown.enter="dialogState.inputValue && closeDialog(dialogState.inputValue)">
      <span v-if="dialogState.kind === 'password' && dialogState.inputError" class="test-field-error">
        <span class="material-symbols-outlined">error</span>{{ dialogState.inputError }}
      </span>
      <div class="dialog-actions">
        <button v-if="dialogState.kind !== 'alert'" class="btn btn-text" @click="closeDialog(false)">取消</button>
        <button v-if="dialogState.kind === 'password'" class="btn btn-filled"
          :disabled="!dialogState.inputValue" @click="closeDialog(dialogState.inputValue)">{{ dialogState.confirmLabel }}</button>
        <button v-else class="btn btn-filled" @click="closeDialog(true)">{{ dialogState.confirmLabel }}</button>
      </div>
    </section>
  </BaseDialog>
</template>
