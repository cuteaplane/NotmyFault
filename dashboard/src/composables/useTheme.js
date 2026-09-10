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
    applyMode(localStorage.getItem('nmf-theme') || 'system')
    window.matchMedia?.('(prefers-color-scheme: dark)').addEventListener('change', () => {
      if (!localStorage.getItem('nmf-theme')) applyMode('system')
    })
  }
  function applyMode(mode) {
    const dark = mode === 'dark' || (
      mode === 'system' && window.matchMedia?.('(prefers-color-scheme: dark)').matches
    )
    document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light')
    sync()
  }
  function setMode(mode) {
    if (!['system', 'light', 'dark'].includes(mode)) throw new Error('未知的主题选项')
    if (mode === 'system') localStorage.removeItem('nmf-theme')
    else localStorage.setItem('nmf-theme', mode)
    applyMode(mode)
  }
  function toggle() {
    setMode(isDark.value ? 'light' : 'dark')
  }
  return { isDark, init, toggle, setMode }
}
