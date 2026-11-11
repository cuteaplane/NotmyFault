import { onActivated, onDeactivated, onMounted, onUnmounted } from 'vue'

// 页面切走后组件仍被 KeepAlive 留在内存里，window 监听只在页面可见时挂着。
export function useWindowListener(type, handler, options) {
  const add = () => window.addEventListener(type, handler, options)
  const remove = () => window.removeEventListener(type, handler, options)
  onMounted(add)
  onActivated(add)
  onDeactivated(remove)
  onUnmounted(remove)
}
