<script setup>
import { useTheme } from '../composables/useTheme'
import { appLogoUrl } from '../lib/branding'
defineProps({ current: String })
const emit = defineEmits(['switch'])
const { isDark, toggle } = useTheme()
const appVersion = __APP_VERSION__
const items = [
  { page: 'home', icon: 'dashboard', label: '首页', needs: false },
  { page: 'plugins', icon: 'extension', label: '插件', needs: true },
  { page: 'security', icon: 'shield', label: '安全', needs: true },
  { page: 'rules', icon: 'rule', label: '规则', needs: true },
  { page: 'logs', icon: 'article', label: '日志', needs: true },
  { page: 'about', icon: 'info', label: '关于', needs: false },
]
</script>

<template>
  <aside class="nav-rail">
    <div class="nav-brand">
      <img class="brand-icon" :src="appLogoUrl" alt="">
      <span class="brand-name">NotmyFault</span>
    </div>
    <nav class="nav-items">
      <button v-for="it in items" :key="it.page"
        class="nav-item" :class="{ active: current === it.page, 'needs-engine': it.needs }"
        @click="emit('switch', it.page)">
        <span class="nav-ind"><span class="material-symbols-outlined nav-icon">{{ it.icon }}</span></span>
        <span class="nav-label">{{ it.label }}</span>
      </button>
    </nav>
    <div class="nav-foot">
      <button class="icon-btn" :title="isDark ? '切换到浅色模式' : '切换到深色模式'" @click="toggle">
        <span class="material-symbols-outlined">{{ isDark ? 'light_mode' : 'dark_mode' }}</span>
      </button>
      <div class="nav-ver" :title="'NotmyFault ' + appVersion">{{ appVersion }}</div>
    </div>
  </aside>
</template>
