export function recentPlugins(kind, keys) {
  try {
    const value = JSON.parse(localStorage.getItem(`notmyfault.recent.${kind}`) || '[]')
    return Array.isArray(value) ? value.filter(key => !keys || keys.includes(key)).slice(0, 6) : []
  } catch { return [] }
}

export function rememberPlugin(kind, key) {
  const recent = [key, ...recentPlugins(kind).filter(item => item !== key)].slice(0, 6)
  try { localStorage.setItem(`notmyfault.recent.${kind}`, JSON.stringify(recent)) } catch {}
  return recent
}
