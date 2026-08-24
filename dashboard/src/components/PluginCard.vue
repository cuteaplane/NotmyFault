<script setup>
import { computed } from 'vue'
const props = defineProps({ pid: String, meta: Object, type: String })
const emit = defineEmits(['toggle', 'uninstall'])
const oL = { builtin: '内置', user: '用户', third_party: '第三方' }
const originClass = computed(() => props.meta.origin === 'builtin' ? 'chip-origin-builtin' : 'chip-origin-user')
const perms = computed(() => props.meta.permissions || [])
const capabilities = computed(() => props.meta.requires_capabilities || [])
const apiVersion = computed(() => props.meta.engines?.notmyfault_api)
const executionMode = computed(() => props.meta.execution_mode || 'in-process')
const entryApi = computed(() => props.type === 'actions'
  ? props.meta.execution_api || 'legacy'
  : props.meta.trigger_api || 'legacy')
const isEnabled = computed(() => props.meta.enabled !== false)
const isCompatible = computed(() => props.meta.platform_compatible !== false)
const availability = computed(() => props.meta.availability || 'available')
const unavailableReasons = computed(() => props.meta.unavailable_reasons || [])
const availabilityLabel = computed(() => {
  if (!isCompatible.value || availability.value === 'unavailable') return '当前系统不可用'
  if (availability.value === 'partial') return '部分可用'
  return ''
})
const canUninstall = computed(() => props.meta.origin === 'user' || props.meta.origin === 'third_party')
</script>

<template>
  <div class="plugin-card" :class="{ disabled: !isEnabled || !isCompatible || availability === 'unavailable' }">
    <div class="plugin-card-head">
      <span class="material-symbols-outlined ico">{{ type === 'actions' ? 'bolt' : 'memory' }}</span>
      <div><div class="plugin-card-name">{{ meta.name || pid }}</div><div class="plugin-card-id">{{ pid }}</div></div>
      <span class="chip" :class="originClass">{{ oL[meta.origin] || meta.origin || '未知' }}</span>
    </div>
    <p class="plugin-card-desc">{{ meta.description || '' }}</p>
    <div class="plugin-card-meta">
      <span class="chip" :title="'versionCode: ' + (meta.version_code || 0)">v{{ meta.version || '?' }}</span>
      <span v-if="perms.includes('admin')" class="chip chip-admin">管理员</span>
      <span v-if="perms.includes('native_api')" class="chip chip-native">原生API</span>
      <span v-if="perms.includes('external_binary')" class="chip chip-external">外部程序</span>
      <span v-for="permission in perms.filter(item => !['admin', 'native_api', 'external_binary'].includes(item))" :key="permission" class="chip">权限 {{ permission }}</span>
      <span v-for="capability in capabilities" :key="capability" class="chip">能力 {{ capability }}</span>
      <span v-if="apiVersion" class="chip">Plugin API v{{ apiVersion }}</span>
      <span class="chip">入口 {{ entryApi }}</span>
      <span class="chip">{{ executionMode === 'isolated' ? '隔离进程' : '引擎进程' }}</span>
      <span v-if="availabilityLabel" class="chip" :class="availability === 'partial' ? 'chip-warn' : 'chip-error'">{{ availabilityLabel }}</span>
      <span v-for="reason in unavailableReasons" :key="reason" class="chip chip-warn" :title="reason">{{ reason }}</span>
      <span v-if="meta._error" class="chip chip-error">{{ String(meta._error).substring(0, 40) }}</span>
    </div>
    <div class="plugin-card-foot">
      <label class="switch"><input type="checkbox" :checked="isEnabled" :disabled="!isCompatible || availability === 'unavailable'" @change="emit('toggle', pid)">
        <span class="switch-track"><span class="switch-thumb"></span></span><span>{{ !isCompatible || availability === 'unavailable' ? '不可用' : isEnabled ? '已启用' : '已禁用' }}</span></label>
      <button v-if="canUninstall" class="icon-btn icon-btn-danger" @click="emit('uninstall', pid)" title="卸载">
        <span class="material-symbols-outlined">delete</span></button>
    </div>
  </div>
</template>
