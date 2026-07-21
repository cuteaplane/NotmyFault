// 插件参数定义工具（规则编辑器使用）

export function getParamDefs(m) {
  if (!m) return []
  if (Array.isArray(m.params)) return m.params
  if (Array.isArray(m.required_params))
    return m.required_params.map(p => ({ name: p, label: p, type: 'string', default: '' }))
  return []
}

export function buildDefaultParams(m) {
  const o = {}
  getParamDefs(m).forEach(p => { o[p.name] = p.default ?? '' })
  return o
}

// options 统一提取 value（兼容字符串和 {value,label} 对象）
export function optValue(o) {
  return typeof o === 'object' && o !== null ? o.value : o
}

// options 统一提取 label（兼容字符串和 {value,label} 对象）
export function optLabel(o) {
  if (typeof o === 'object' && o !== null) return o.label || o.value || ''
  return o
}

// 判断参数是否应该显示（基于 visible_when 条件）
// visible_when 格式: {"param_name": ["value1", "value2"]}，多条件为 AND
export function isVisible(paramDef, currentParams) {
  const vw = paramDef.visible_when
  if (!vw) return true
  for (const [key, vals] of Object.entries(vw)) {
    const cur = String(currentParams?.[key] ?? '')
    if (!vals.map(String).includes(cur)) return false
  }
  return true
}

// 返回当前应该显示的参数定义（过滤掉 visible_when 不满足的）
export function getVisibleParamDefs(meta, currentParams) {
  return getParamDefs(meta).filter(p => isVisible(p, currentParams))
}
