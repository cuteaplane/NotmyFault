<script setup>
import { useTheme } from '../composables/useTheme'
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
    <div class="nav-brand"><span class="material-symbols-outlined brand-icon">manufacturing</span><span>NotmyFault</span></div>
    <nav class="nav-items">
      <button v-for="it in items" :key="it.page"
        class="nav-item" :class="{ active: current === it.page, 'needs-engine': it.needs }"
        @click="emit('switch', it.page)">
        <span class="material-symbols-outlined nav-icon">{{ it.icon }}</span>
        <span>{{ it.label }}</span>
      </button>
    </nav>
    <div class="nav-foot">
      <button class="nav-item" @click="toggle">
        <span class="material-symbols-outlined nav-icon">{{ isDark ? 'light_mode' : 'dark_mode' }}</span>
        <span>{{ isDark ? '浅色模式' : '深色模式' }}</span>
      </button>
      <div class="nav-ver">NotmyFault {{ appVersion }}</div>
    </div>
  </aside>
</template>
