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
