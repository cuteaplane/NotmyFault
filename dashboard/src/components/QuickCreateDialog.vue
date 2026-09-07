<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { store } from '../lib/store'
import { groupActionKeys, groupTriggerKeys, pluginUnavailableReason } from '../lib/utils'
import BaseDialog from './BaseDialog.vue'

const props = defineProps({ open: Boolean })
const emit = defineEmits(['close', 'create'])
const step = ref('trigger')
const triggerType = ref('')
const actionType = ref('')
const query = ref('')
const searchRef = ref(null)

const schema = computed(() => step.value === 'trigger' ? store.schema.triggers : store.schema.actions)
const storageKey = computed(() => `notmyfault.recent.${step.value}`)
const availableKeys = computed(() => Object.keys(schema.value || {}).filter(key => {
  const meta = schema.value[key]
  return !pluginUnavailableReason(meta)
}))

function recentKeys() {
  try {
    const value = JSON.parse(localStorage.getItem(storageKey.value) || '[]')
    return Array.isArray(value) ? value.filter(key => availableKeys.value.includes(key)).slice(0, 6) : []
  } catch { return [] }
}

const visibleGroups = computed(() => {
  const q = query.value.trim().toLowerCase()
  const recent = recentKeys()
  const ordered = [...recent, ...availableKeys.value.filter(key => !recent.includes(key))]
  const filtered = q ? ordered.filter(key => {
    const meta = schema.value[key] || {}
    return [key, meta.name, meta.description].some(value => String(value || '').toLowerCase().includes(q))
  }) : ordered
  const grouped = step.value === 'trigger' ? groupTriggerKeys(filtered) : groupActionKeys(filtered)
  return recent.length && !q
    ? [['最近使用', filtered.filter(key => recent.includes(key))], ...grouped.map(([name, keys]) => [name, keys.filter(key => !recent.includes(key))]).filter(([, keys]) => keys.length)]
    : grouped
})

const triggerName = computed(() => store.schema.triggers?.[triggerType.value]?.name || '未选择')
const actionName = computed(() => store.schema.actions?.[actionType.value]?.name || '未选择')

function reset() {
  step.value = 'trigger'
  triggerType.value = ''
  actionType.value = ''
  query.value = ''
}

async function focusSearch() {
  await nextTick()
  searchRef.value?.focus()
}

function remember(kind, key) {
  const name = `notmyfault.recent.${kind}`
  try {
    const value = JSON.parse(localStorage.getItem(name) || '[]')
    const recent = [key, ...(Array.isArray(value) ? value : []).filter(item => item !== key)].slice(0, 6)
    localStorage.setItem(name, JSON.stringify(recent))
  } catch {}
}

function choose(key) {
  if (step.value === 'trigger') {
    triggerType.value = key
    remember('trigger', key)
    step.value = 'action'
    query.value = ''
    focusSearch()
    return
  }
  actionType.value = key
  remember('action', key)
}

function back() {
  step.value = 'trigger'
  query.value = ''
  focusSearch()
}

function create() {
  if (!triggerType.value || !actionType.value) return
  emit('create', { triggerType: triggerType.value, actionType: actionType.value })
}

watch(() => props.open, open => {
  if (!open) return
  reset()
  focusSearch()
})
</script>

<template>
  <BaseDialog :open="open" @close="emit('close')">
    <section class="quick-create-dialog" role="dialog" aria-modal="true" aria-labelledby="quick-create-title">
        <header class="quick-create-head">
          <div>
            <small>新建自动化 · 第 {{ step === 'trigger' ? '1' : '2' }} 步，共 2 步</small>
            <h2 id="quick-create-title">{{ step === 'trigger' ? '什么时候开始？' : '接着要做什么？' }}</h2>
            <p>{{ step === 'trigger' ? '先选择一种触发方式。参数可以进入编辑器后再调整。' : '选择第一个动作，之后仍可继续添加更多步骤。' }}</p>
          </div>
          <button class="icon-btn" title="关闭" aria-label="关闭新建自动化" @click="emit('close')"><span class="material-symbols-outlined">close</span></button>
        </header>

        <div class="quick-create-sentence" aria-live="polite">
          <span><i>当</i><b :class="{ placeholder: !triggerType }">{{ triggerName }}</b></span>
          <span class="material-symbols-outlined">arrow_forward</span>
          <span><i>然后</i><b :class="{ placeholder: !actionType }">{{ actionName }}</b></span>
        </div>

        <label class="quick-create-search">
          <span class="material-symbols-outlined">search</span>
          <input ref="searchRef" v-model="query" type="search" :aria-label="step === 'trigger' ? '搜索触发方式' : '搜索动作'"
            autocomplete="off" spellcheck="false" :placeholder="step === 'trigger' ? '搜索触发方式' : '搜索动作'">
        </label>

        <div class="quick-create-list">
          <template v-for="([group, keys]) in visibleGroups" :key="group">
            <h3>{{ group }}</h3>
            <button v-for="key in keys" :key="key" class="quick-create-option"
              :class="{ selected: step === 'action' && actionType === key }"
              :aria-pressed="step === 'action' ? actionType === key : undefined" @click="choose(key)">
              <span class="material-symbols-outlined">{{ step === 'trigger' ? 'bolt' : 'play_arrow' }}</span>
              <span><b>{{ schema[key]?.name || key }}</b><small>{{ schema[key]?.description || key }}</small></span>
              <span v-if="schema[key]?.permissions?.includes('admin')" class="chip chip-admin">管理员</span>
              <span class="material-symbols-outlined quick-create-option-state">{{ step === 'action' && actionType === key ? 'check_circle' : 'arrow_forward' }}</span>
            </button>
          </template>
          <p v-if="!visibleGroups.length" class="quick-create-empty">没有找到匹配的{{ step === 'trigger' ? '触发方式' : '动作' }}。</p>
        </div>

        <footer class="quick-create-foot">
          <button v-if="step === 'action'" class="btn btn-text" @click="back"><span class="material-symbols-outlined">arrow_back</span>上一步</button>
          <span v-else></span>
          <button class="btn btn-filled" :disabled="step !== 'action' || !actionType" @click="create">
            在编辑器中继续<span class="material-symbols-outlined">arrow_forward</span>
          </button>
        </footer>
      </section>
  </BaseDialog>
</template>
