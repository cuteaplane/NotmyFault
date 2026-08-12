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
