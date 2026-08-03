<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { store } from '../lib/store'

const props = defineProps({
  open: Boolean,
  kind: { type: String, default: 'action' },
  keys: { type: Array, default: () => [] },
  groups: { type: Array, default: () => [] },
  title: { type: String, default: '' },
})
const emit = defineEmits(['close', 'select'])

const query = ref('')
const category = ref('全部')
const searchRef = ref(null)
const schema = computed(() => props.kind === 'trigger' ? store.schema.triggers : store.schema.actions)
const recentStorageKey = computed(() => `notmyfault.recent.${props.kind}`)

function loadRecent() {
  try {
    const value = JSON.parse(localStorage.getItem(recentStorageKey.value) || '[]')
    return Array.isArray(value) ? value.filter(key => props.keys.includes(key)).slice(0, 6) : []
  } catch { return [] }
}

const recentKeys = ref([])
const normalizedGroups = computed(() => {
  const result = props.groups.length ? props.groups : [['全部', props.keys]]
  return result.map(([name, keys]) => [name, keys.filter(key => props.keys.includes(key))])
    .filter(([, keys]) => keys.length)
})
const categories = computed(() => ['全部', ...(recentKeys.value.length ? ['最近使用'] : []), ...normalizedGroups.value.map(([name]) => name)])
const visibleGroups = computed(() => {
  const q = query.value.trim().toLowerCase()
  let groups = category.value === '最近使用'
    ? [['最近使用', recentKeys.value]]
    : category.value === '全部'
      ? normalizedGroups.value
      : normalizedGroups.value.filter(([name]) => name === category.value)
  if (!q) return groups
  return groups.map(([name, keys]) => [name, keys.filter(key => {
    const meta = schema.value[key] || {}
    return [meta.name, meta.description, key].some(value => String(value || '').toLowerCase().includes(q))
  })]).filter(([, keys]) => keys.length)
})

watch(() => props.open, async open => {
  if (!open) return
  query.value = ''
  category.value = '全部'
  recentKeys.value = loadRecent()
  await nextTick()
  searchRef.value?.focus()
})

function choose(key) {
  const recent = [key, ...loadRecent().filter(item => item !== key)].slice(0, 6)
  try { localStorage.setItem(recentStorageKey.value, JSON.stringify(recent)) } catch {}
  recentKeys.value = recent
  emit('select', key)
}
</script>

<template>
  <Transition name="picker-surface">
  <div v-if="open" class="plugin-picker-backdrop" @pointerdown.self="emit('close')" @keydown.esc="emit('close')">
    <section class="plugin-picker-dialog" role="dialog" aria-modal="true" :aria-label="title || '选择插件'">
      <header class="plugin-picker-head">
        <div><small>{{ kind === 'trigger' ? '触发方式' : kind === 'precondition' ? '开始前确认' : '执行动作' }}</small><h2>{{ title || '选择插件' }}</h2></div>
        <button class="icon-btn" title="关闭" @click="emit('close')"><span class="material-symbols-outlined">close</span></button>
      </header>
      <label class="plugin-picker-search">
        <span class="material-symbols-outlined">search</span>
        <input ref="searchRef" v-model="query" type="search" role="searchbox" aria-label="搜索插件"
          autocomplete="off" spellcheck="false" placeholder="搜索名称、说明或插件 ID">
      </label>
      <nav class="plugin-picker-categories" aria-label="插件分类">
        <button v-for="name in categories" :key="name" :class="{ active: category === name }" @click="category = name">{{ name }}</button>
      </nav>
      <div class="plugin-picker-list">
        <template v-for="([group, items]) in visibleGroups" :key="group">
          <h3>{{ group }}</h3>
          <button v-for="key in items" :key="key" class="plugin-picker-item" @click="choose(key)">
            <span class="material-symbols-outlined">{{ kind === 'trigger' ? 'bolt' : kind === 'precondition' ? 'verified_user' : 'play_arrow' }}</span>
            <span><b>{{ schema[key]?.name || key }}</b><small>{{ schema[key]?.description || key }}</small></span>
            <span v-if="schema[key]?.permissions?.includes('admin')" class="chip chip-admin">管理员</span>
            <span class="material-symbols-outlined plugin-picker-arrow">arrow_forward</span>
          </button>
        </template>
        <div v-if="!visibleGroups.length" class="plugin-picker-empty">没有找到匹配的插件。</div>
      </div>
    </section>
  </div>
  </Transition>
</template>
