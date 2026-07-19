<script setup>
import { computed } from 'vue'
const props = defineProps({ pid: String, meta: Object, type: String })
const emit = defineEmits(['toggle', 'uninstall'])
const oL = { builtin: '内置', user: '用户', third_party: '第三方' }
const originClass = computed(() => props.meta.origin === 'builtin' ? 'chip-origin-builtin' : 'chip-origin-user')
const perms = computed(() => props.meta.permissions || [])
const isEnabled = computed(() => props.meta.enabled !== false)
const canUninstall = computed(() => props.meta.origin === 'user' || props.meta.origin === 'third_party')
</script>

<template>
  <div class="plugin-card" :class="{ disabled: !isEnabled }">
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
      <span v-if="meta._error" class="chip chip-error">{{ String(meta._error).substring(0, 40) }}</span>
    </div>
    <div class="plugin-card-foot">
      <label class="switch"><input type="checkbox" :checked="isEnabled" @change="emit('toggle', pid)">
        <span class="switch-track"><span class="switch-thumb"></span></span><span>{{ isEnabled ? '已启用' : '已禁用' }}</span></label>
      <button v-if="canUninstall" class="icon-btn icon-btn-danger" @click="emit('uninstall', pid)" title="卸载">
        <span class="material-symbols-outlined">delete</span></button>
    </div>
  </div>
</template>
