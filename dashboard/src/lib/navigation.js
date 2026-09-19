import { defineAsyncComponent } from 'vue'
import HomeView from '../components/views/HomeView.vue'
export const navigationItems = [
  { page: 'home', icon: 'dashboard', label: '首页', needs: false, component: HomeView },
  { page: 'rules', icon: 'account_tree', label: '自动化', needs: true, component: defineAsyncComponent(() => import('../components/views/RulesView.vue')) },
  { page: 'plugins', icon: 'extension', label: '插件', needs: true, component: defineAsyncComponent(() => import('../components/views/PluginsView.vue')) },
  { page: 'settings', icon: 'settings', label: '设置', needs: false, component: defineAsyncComponent(() => import('../components/views/SettingsView.vue')) },
]
export const views = Object.fromEntries(navigationItems.map(item => [item.page, item.component]))
