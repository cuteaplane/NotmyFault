import { getParamDefs } from './utils.js'

function comparable(value) {
  if (Array.isArray(value)) return value.map(comparable)
  if (!value || typeof value !== 'object') return value
  return Object.fromEntries(Object.entries(value).filter(([key]) => !['binding_id', 'rule_id'].includes(key)).sort(([a], [b]) => a.localeCompare(b)).map(([key, child]) => [key, comparable(child)]))
}
function equal(a, b) { return JSON.stringify(comparable(a)) === JSON.stringify(comparable(b)) }
function withoutFailureActions(node) { const { failure_actions, ...rest } = node; return rest }
function nodeLabel(node, schema = {}) { return node.name || (node.type === 'trigger' ? schema.triggers?.[node.trigger.type]?.name || node.trigger.type : schema.actions?.[node.type]?.name || node.type) }
function listSummary(before = [], after = [], noun, transform = value => value) {
  const previous = new Map(before.map(node => [node.binding_id, node])), current = new Map(after.map(node => [node.binding_id, node]))
  const added = after.filter(node => !previous.has(node.binding_id)).length
  const removed = before.filter(node => !current.has(node.binding_id)).length
  const changed = after.filter(node => previous.has(node.binding_id) && !equal(transform(previous.get(node.binding_id)), transform(node))).length
  return [...(added ? [`新增 ${added} 个${noun}`] : []), ...(removed ? [`删除 ${removed} 个${noun}`] : []), ...(changed ? [`修改 ${changed} 个${noun}`] : [])]
}
export function summarizeRuleChanges(before, after) {
  if (!before || !after) return []
  const changes = []
  if (before.name !== after.name) changes.push('名称已改')
  if (before.folder !== after.folder) changes.push('文件夹已改')
  const previous = new Map((before.workflow?.nodes || []).map(node => [node.binding_id, node]))
  changes.push(...listSummary(before.workflow?.nodes, after.workflow?.nodes, '图块', withoutFailureActions))
  for (const node of after.workflow?.nodes || []) if (previous.has(node.binding_id)) changes.push(...listSummary(previous.get(node.binding_id).failure_actions, node.failure_actions, `${nodeLabel(node)} 的补救动作`))
  if (!equal(before.workflow?.edges, after.workflow?.edges)) changes.push('流程连接已改')
  if (!changes.length && !equal(before, after)) changes.push('规则设置已改')
  return changes
}
function describeParamChanges(before, after, defs) {
  function format(value, def) {
    if (value === undefined || value === null) return '—'
    if (def?.sensitive) return '已隐藏'
    if (typeof value === 'boolean') return value ? '是' : '否'
    return typeof value === 'object' ? JSON.stringify(value) : String(value)
  }
  return [...new Set([...Object.keys(before || {}), ...Object.keys(after || {})])].filter(key => !equal(before?.[key], after?.[key])).map(key => {
    const def = defs.find(item => item.name === key)
    return `${def?.label || key}: ${format(before?.[key], def)} → ${format(after?.[key], def)}`
  })
}
export function computeChangeSet(before, after, schema = {}) {
  if (!after) return []
  const items = []
  if (before && before.name !== after.name) items.push({ op: 'modify', target: 'name', label: '规则名称', detail: `${before.name} → ${after.name || '未命名'}` })
  function list(previous = [], current = [], target, options = {}) {
    const old = new Map(previous.map(node => [node.binding_id, node])), now = new Map(current.map(node => [node.binding_id, node]))
    for (const [index, node] of current.entries()) {
      const original = old.get(node.binding_id)
      const common = { target, node_id: node.binding_id, index, ...options, label: nodeLabel(node, schema) }
      if (!original) { items.push({ ...common, op: 'add', detail: '新增节点' }); continue }
      if (equal(withoutFailureActions(original), withoutFailureActions(node))) continue
      const plugin = node.type === 'trigger' ? node.trigger : node
      const previousPlugin = original.type === 'trigger' ? original.trigger : original
      const meta = node.type === 'trigger' ? schema.triggers?.[plugin.type] : schema.actions?.[plugin.type]
      const details = describeParamChanges(previousPlugin.params, plugin.params, getParamDefs(meta))
      if (details.length) for (const detail of details) items.push({ ...common, op: 'modify', detail })
      else items.push({ ...common, op: 'modify', detail: '节点内容已变更' })
    }
    for (const [index, node] of previous.entries()) if (!now.has(node.binding_id)) items.push({ target, node_id: node.binding_id, index, ...options, op: 'delete', label: nodeLabel(node, schema) })
    if (target === 'failure-action' && old.size === now.size && [...old.keys()].every(id => now.has(id)) && [...old.keys()].some((id, index) => id !== [...now.keys()][index])) items.push({ target, ...options, op: 'modify', label: '调整补救动作顺序' })
  }
  const previousNodes = before?.workflow?.nodes || [], nodes = after.workflow?.nodes || []
  list(previousNodes, nodes, 'graph-node')
  const old = new Map(previousNodes.map(node => [node.binding_id, node]))
  nodes.forEach((node, parentIndex) => { if (old.has(node.binding_id)) list(old.get(node.binding_id).failure_actions, node.failure_actions, 'failure-action', { parentIndex }) })
  if (!equal(before?.workflow?.edges, after.workflow?.edges)) items.push({ op: 'modify', target: 'rule', label: '流程连接', detail: '条件或执行路径已变更' })
  if (!items.length && !equal(before, after)) items.push({ op: 'modify', target: 'rule', label: '规则设置', detail: '已变更' })
  return items
}
