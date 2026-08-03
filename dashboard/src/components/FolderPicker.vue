<script setup>
import { computed, nextTick, ref, watch } from 'vue'

const props = defineProps({
  open: Boolean,
  folders: { type: Array, default: () => [] },
  current: { type: String, default: '' },
})
const emit = defineEmits(['close', 'select'])

const query = ref('')
const inputRef = ref(null)
const normalizedFolders = computed(() => [...new Set(
  props.folders.map(name => String(name || '').trim()).filter(Boolean),
)])
const visibleFolders = computed(() => {
  const needle = query.value.trim().toLocaleLowerCase('zh-CN')
  if (!needle) return normalizedFolders.value
  return normalizedFolders.value.filter(name => name.toLocaleLowerCase('zh-CN').includes(needle))
})
const canCreate = computed(() => {
  const name = query.value.trim()
  return !!name && !normalizedFolders.value.some(folder => folder === name)
})

watch(() => props.open, async open => {
  if (!open) return
  query.value = ''
  await nextTick()
  inputRef.value?.focus()
})

function createAndSelect() {
  const name = query.value.trim()
  if (name) emit('select', name)
}
</script>

<template>
  <Transition name="picker-surface">
    <div v-if="open" class="plugin-picker-backdrop" @pointerdown.self="emit('close')" @keydown.esc="emit('close')">
      <section class="plugin-picker-dialog folder-picker-dialog" role="dialog" aria-modal="true" aria-label="管理规则文件夹">
        <header class="plugin-picker-head">
          <div><small>规则归档</small><h2>管理文件夹</h2></div>
          <button class="icon-btn" title="关闭" @click="emit('close')"><span class="material-symbols-outlined">close</span></button>
        </header>

        <div class="folder-picker-create">
          <label class="plugin-picker-search">
            <span class="material-symbols-outlined">create_new_folder</span>
            <input ref="inputRef" v-model="query" autocomplete="off" spellcheck="false"
              placeholder="搜索文件夹，或输入新名称" @keydown.enter.prevent="createAndSelect">
          </label>
          <button v-if="canCreate" class="btn btn-filled" @click="createAndSelect">
            <span class="material-symbols-outlined">add</span>新建并使用
          </button>
        </div>

        <div class="plugin-picker-list folder-picker-list">
          <h3>放到</h3>
          <button class="plugin-picker-item folder-picker-item" @click="emit('select', '')">
            <span class="material-symbols-outlined">folder_off</span>
            <span><b>未分类</b><small>从文件夹中移出，仍保留这条规则</small></span>
            <span v-if="!current" class="chip">当前</span>
            <span class="material-symbols-outlined plugin-picker-arrow">{{ !current ? 'check' : 'arrow_forward' }}</span>
          </button>
          <button v-for="folder in visibleFolders" :key="folder" class="plugin-picker-item folder-picker-item"
            @click="emit('select', folder)">
            <span class="material-symbols-outlined">folder</span>
            <span><b>{{ folder }}</b><small>将这条规则移动到此文件夹</small></span>
            <span v-if="current === folder" class="chip">当前</span>
            <span class="material-symbols-outlined plugin-picker-arrow">{{ current === folder ? 'check' : 'arrow_forward' }}</span>
          </button>
          <div v-if="query && !visibleFolders.length && !canCreate" class="plugin-picker-empty">没有找到其他文件夹。</div>
        </div>
      </section>
    </div>
  </Transition>
</template>
