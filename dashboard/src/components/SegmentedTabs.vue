<script setup>
defineProps({
  modelValue: { type: String, required: true },
  options: { type: Array, required: true },
  label: { type: String, required: true },
  role: { type: String, default: 'tablist' },
  block: Boolean,
})
const emit = defineEmits(['update:modelValue'])
</script>

<template>
  <div class="seg" :class="{ 'seg-block': block }" :role="role" :aria-label="label">
    <button v-for="option in options" :key="option.value" type="button" class="seg-item" :class="{ active: option.value === modelValue }"
      :role="role === 'tablist' ? 'tab' : undefined"
      :aria-selected="role === 'tablist' ? option.value === modelValue : undefined"
      :aria-pressed="role === 'tablist' ? undefined : option.value === modelValue"
      @click="emit('update:modelValue', option.value)">
      <span v-if="option.icon" class="material-symbols-outlined">{{ option.icon }}</span>{{ option.label }}<span v-if="option.count !== undefined" class="seg-count">{{ option.count }}</span>
    </button>
  </div>
</template>
