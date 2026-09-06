<script setup>
import { useTheme } from '../composables/useTheme'
import { appLogoUrl } from '../lib/branding'
import { store } from '../lib/store'
defineProps({ current: String })
const emit = defineEmits(['switch'])
const { isDark, toggle } = useTheme()
const appVersion = __APP_VERSION__
const items = [
  { page: 'home', icon: 'dashboard', label: '首页', needs: false },
  { page: 'rules', icon: 'account_tree', label: '自动化', needs: true },
  { page: 'plugins', icon: 'extension', label: '插件', needs: true },
  { page: 'settings', icon: 'settings', label: '设置', needs: true },
]
</script>

<template>
  <aside class="nav-rail">
    <div class="nav-brand">
      <img class="brand-icon" :src="appLogoUrl" alt="">
      <span class="brand-name">NotmyFault</span>
    </div>
    <nav class="nav-items" aria-label="主要页面">
      <button v-for="it in items" :key="it.page"
        class="nav-item" :class="{ active: current === it.page, 'needs-engine': it.needs }"
        :aria-current="current === it.page ? 'page' : undefined"
        :disabled="it.needs && !store.controllerOnline"
        @click="emit('switch', it.page)">
        <span class="nav-ind"><span class="material-symbols-outlined nav-icon">{{ it.icon }}</span></span>
        <span class="nav-label">{{ it.label }}</span>
      </button>
    </nav>
    <div class="nav-foot">
      <span class="nav-engine-state" :class="{ running: store.engineOnline }" :title="store.engineOnline ? '自动化运行中' : store.controllerOnline ? '自动化已暂停' : '后台服务离线'"><i></i><span>{{ store.engineOnline ? '运行中' : store.controllerOnline ? '已暂停' : '离线' }}</span></span>
      <button class="icon-btn" :aria-label="isDark ? '切换到浅色模式' : '切换到深色模式'" :title="isDark ? '切换到浅色模式' : '切换到深色模式'" @click="toggle">
        <span class="material-symbols-outlined">{{ isDark ? 'light_mode' : 'dark_mode' }}</span>
      </button>
      <div class="nav-ver" :title="'NotmyFault ' + appVersion">{{ appVersion }}</div>
    </div>
  </aside>
</template>
