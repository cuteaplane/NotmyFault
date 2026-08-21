import { getParamDefs } from './utils'

function comparable(value) {
  if (Array.isArray(value)) return value.map(comparable)
  if (!value || typeof value !== 'object') return value
  return Object.fromEntries(
    Object.entries(value)
      .filter(([key]) => key !== 'binding_id' && key !== 'rule_id')
      .map(([key, item]) => [key, comparable(item)]),
  )
}

function equal(left, right) {
  return JSON.stringify(comparable(left)) === JSON.stringify(comparable(right))
}

function describeList(before, after, noun) {
  const previous = Array.isArray(before) ? before : []
  const current = Array.isArray(after) ? after : []
  const previousById = new Map(previous.map((item, index) => [item?.binding_id || `index:${index}`, item]))
  const currentById = new Map(current.map((item, index) => [item?.binding_id || `index:${index}`, item]))
  const added = [...currentById.keys()].filter(id => !previousById.has(id)).length
  const removed = [...previousById.keys()].filter(id => !currentById.has(id)).length
  const changed = [...currentById].filter(([id, item]) => previousById.has(id) && !equal(previousById.get(id), item)).length
  const sameIds = previous.length === current.length
    && [...previousById.keys()].every(id => currentById.has(id))
  const reordered = sameIds
    && previous.map(item => item?.binding_id).join('|') !== current.map(item => item?.binding_id).join('|')
  const result = []
  if (added) result.push(`新增 ${added} 个${noun}`)
  if (removed) result.push(`删除 ${removed} 个${noun}`)
  if (changed) result.push(`修改 ${changed} 个${noun}`)
  if (reordered) result.push(`调整${noun}顺序`)
  return result
}

export function summarizeRuleChanges(before, after) {
  if (!before || !after) return []
  const changes = []
  if (String(before.name || '') !== String(after.name || '')) changes.push('名称已改')
  if (String(before.folder || '') !== String(after.folder || '')) changes.push('文件夹已改')
  if (!equal(before.event || before.condition, after.event || after.condition)) changes.push('触发条件已改')
  changes.push(...describeList(before.preconditions, after.preconditions, '确认'))
  changes.push(...describeList(before.actions, after.actions, '动作'))
  if (!changes.length && !equal(before, after)) changes.push('规则设置已改')
  return changes
}

function describeParamChanges(beforeParams, afterParams, defs) {
  const result = []
  const allKeys = new Set([
    ...Object.keys(beforeParams || {}),
    ...Object.keys(afterParams || {}),
  ])
  for (const key of allKeys) {
    const def = (defs || []).find(d => d.name === key)
    const label = def?.label || def?.name || key
    const oldVal = beforeParams?.[key]
    const newVal = afterParams?.[key]
    if (!equal(oldVal, newVal)) {
      const fmt = v => {
        if (v === undefined || v === null) return '—'
        if (typeof v === 'boolean') return v ? '是' : '否'
        if (Array.isArray(v)) return v.join(', ')
        return String(v)
      }
      result.push({ label, before: fmt(oldVal), after: fmt(newVal) })
    }
  }
  return result
}

export function computeChangeSet(before, after, schema) {
  if (!after) return []
  const items = []
  if (before && String(before.name || '') !== String(after.name || '')) {
    items.push({ op: 'modify', target: 'name', label: '规则名称', detail: before.name + ' → ' + (after.name || '未命名') })
  }
  if (before && !equal(before.event || before.condition, after.event || after.condition)) {
    items.push({ op: 'modify', target: 'trigger', label: '触发条件', detail: '已变更' })
  }
  const diffList = (beforeArr, afterArr, noun, kind) => {
    const prev = Array.isArray(beforeArr) ? beforeArr : []
    const curr = Array.isArray(afterArr) ? afterArr : []
    const prevMap = new Map(prev.map((item, i) => [item?.binding_id || 'idx:' + i, { item, index: i }]))
    const currMap = new Map(curr.map((item, i) => [item?.binding_id || 'idx:' + i, { item, index: i }]))
    for (const [id, { item, index }] of currMap) {
      if (!prevMap.has(id)) {
        const name = schema?.actions?.[item.type]?.name || item.type || noun
        items.push({ op: 'add', target: kind, label: name, index })
      }
    }
    for (const [id, { item, index }] of prevMap) {
      if (!currMap.has(id)) {
        const name = schema?.actions?.[item.type]?.name || item.type || noun
        items.push({ op: 'delete', target: kind, label: name, index })
      }
    }
    for (const [id, { item: afterItem }] of currMap) {
      const prevEntry = prevMap.get(id)
      if (!prevEntry) continue
      const beforeItem = prevEntry.item
      if (equal(beforeItem, afterItem)) continue
      const name = schema?.actions?.[afterItem.type]?.name || afterItem.type || noun
      const paramDefs = getParamDefs(schema?.actions?.[afterItem.type])
      const paramChanges = describeParamChanges(beforeItem.params, afterItem.params, paramDefs)
      if (paramChanges.length) {
        for (const pc of paramChanges) {
          items.push({ op: 'modify', target: kind, label: name, detail: pc.label + ': ' + pc.before + ' → ' + pc.after, index: prevEntry.index })
        }
      } else {
        items.push({ op: 'modify', target: kind, label: name, index: prevEntry.index })
      }
    }
  }
  diffList(before?.preconditions, after.preconditions, '确认', 'precondition')
  diffList(before?.actions, after.actions, '动作', 'action')
  if (!items.length && !equal(before, after)) {
    items.push({ op: 'modify', target: 'rule', label: '规则设置', detail: '已变更' })
  }
  return items
}
