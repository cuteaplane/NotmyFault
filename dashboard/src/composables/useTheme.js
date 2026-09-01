import { ref } from 'vue'

// App 和 NavRail 共享模块里的 isDark 状态。
const isDark = ref(false)
let initialized = false

export function useTheme() {
  function sync() {
    isDark.value = document.documentElement.getAttribute('data-theme') === 'dark'
  }
  function init() {
    if (initialized) return
    initialized = true
    const root = document.documentElement
    const stored = localStorage.getItem('nmf-theme')
    if (stored) root.setAttribute('data-theme', stored)
    else if (window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches)
      root.setAttribute('data-theme', 'dark')
    sync()
  }
  function toggle() {
    const root = document.documentElement
    const dark = root.getAttribute('data-theme') === 'dark'
    root.setAttribute('data-theme', dark ? 'light' : 'dark')
    localStorage.setItem('nmf-theme', dark ? 'light' : 'dark')
    sync()
  }
  return { isDark, init, toggle }
}
