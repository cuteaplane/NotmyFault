import { store } from './store'
import { getVisibleParamDefs, optLabel, optValue } from './utils'
import { isReference } from './bindings'

export const isAdmin = (meta) => !!(meta?.permissions || []).includes('admin')

export const eventName = (event) => store.schema.triggers[event?.type]?.name || event?.type || '未选择触发器'

export const actionName = (action) => action?.type === 'if' ? 'IF 条件分支' : action?.type === 'set_variable' ? '变量赋值' : store.schema.actions[action?.type]?.name || action?.type || '未选择动作'

export function formatParamValue(value, def) {
  if (isReference(value)) return '运行数据'
  if (def?.type === 'password' || def?.type === 'secret') return value ? '已设置' : ''
  if (def?.type === 'select') {
    const option = (def.options || []).find(item => String(optValue(item)) === String(value))
    if (option !== undefined) return optLabel(option)
  }
  if (Array.isArray(value)) return value.join(', ')
  if (value && typeof value === 'object') return JSON.stringify(value)
  if (typeof value === 'boolean') return value ? '是' : '否'
  return String(value ?? '')
}

export function describeItem(item, kind) {
  if (!item) return '需要配置'
  const meta = kind === 'trigger' ? store.schema.triggers[item.type] : store.schema.actions[item.type]
  const params = item.params || {}
  const details = getVisibleParamDefs(meta, params)
    .map(def => [def.label || def.name, formatParamValue(params[def.name], def)])
    .filter(([, value]) => value !== '')
    .slice(0, 2)
    .map(([label, value]) => `${label}: ${value}`)
  const text = details.join(' · ') || '无额外参数'
  return text.length > 54 ? `${text.slice(0, 52)}…` : text
}
