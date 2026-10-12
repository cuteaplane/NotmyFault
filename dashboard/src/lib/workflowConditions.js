import { findGraphNode, walkGraphActions } from './workflowGraph.js'
import { actionOutputDefs, collectTriggerLeaves, buildBindingSources, expandBindingSources, referenceLabel, graphBindingSources } from './bindings.js'
import { fieldType } from './valueTypes.js'

export const RUN_STATUS_OPTIONS = [
  ['running', '正在运行'], ['queued', '排队'], ['succeeded', '最近一次成功'],
  ['failed', '最近一次失败'], ['cancelled', '最近一次停止'], ['timed_out', '最近一次超时'],
  ['dropped', '最近一次丢弃'], ['never_run', '尚未运行'],
]
export const PREDICATE_LABELS = { eq: '等于', ne: '不等于', gt: '大于', gte: '大于或等于', lt: '小于', lte: '小于或等于', contains: '包含', is_true: '为真', is_false: '为假', exists: '存在', not_exists: '缺失' }

export function workflowEventChoices(rule, schema) {
  return collectTriggerLeaves(rule || {}).map((trigger, index) => ({
    id: trigger.binding_id, source: trigger,
    label: `触发 ${index + 1} · ${schema?.triggers?.[trigger.type]?.name || trigger.type}`,
  }))
}

export function ifBranches(action) {
  return [
    { key: 'then', label: 'IF 成立', condition: action.condition, actions: action.then || [] },
    ...(action.elif || []).map((branch, index) => ({ key: `elif[${index}]`, label: `ELIF ${index + 1} 成立`, condition: branch.condition, actions: branch.then || [], branch, index })),
    { key: 'else', label: 'ELSE', actions: action.else || [] },
  ]
}
export function walkWorkflowActions(actions, visit, trail = []) {
  ;(actions || []).forEach((action, index) => {
    visit(action, index, trail)
    if (action.type === 'if') for (const branch of ifBranches(action)) {
      walkWorkflowActions(branch.actions, visit, [...trail, { action, field: branch.key, index }])
    }
    walkWorkflowActions(action.failure_actions, visit, [...trail, { action, field: 'failure_actions', index }])
  })
}
export function findWorkflowAction(rule, bindingId) {
  if (rule.workflow) return findGraphNode(rule, bindingId)
  let found = null
  walkWorkflowActions(rule.actions, (action, index, trail) => {
    if (action.binding_id === bindingId) found = { action, index, trail }
  })
  return found
}
export function precedingSources(rule, action, schema) {
  if (rule.workflow) return graphBindingSources(rule, action, schema)
  const hit = findWorkflowAction(rule, action.binding_id)
  if (!hit) return []
  const rootIndex = hit.trail[0]?.index ?? hit.index
  let sources = buildBindingSources(rule, rootIndex, schema)
  for (let depth = 0; depth < hit.trail.length; depth++) {
    const parent = hit.trail[depth]
    const siblings = parent.field === 'failure_actions' ? parent.action.failure_actions : ifBranches(parent.action).find(branch => branch.key === parent.field)?.actions
    const end = hit.trail[depth + 1]?.index ?? hit.index
    sources = appendActionSources(sources, (siblings || []).slice(0, end), rule, schema)
  }
  return sources
}
export function appendActionSources(base, actions, rule, schema) {
  const result = []
  for (const action of actions) for (const output of actionOutputDefs(action, schema, rule)) result.push({
    group: '当前执行路径的前序动作', label: `${schema.actions[action.type]?.name || action.type} · ${output.label || output.name}`,
    type: fieldType(output), optional: output.required === false, sensitive: output.sensitive,
    value: { $ref: { scope: 'step', node: action.binding_id, path: [output.name] } },
  })
  return [...new Map([...base, ...expandBindingSources(result, schema.data_types?.custom)].map(source => [JSON.stringify(source.value.$ref), source])).values()]
}
export function predicateSummaryParts(node, sources = []) {
  const value = expression => expression?.$ref ? (sources.length ? referenceLabel(expression, sources) : expression.$ref.path?.join('.') || '数据') : typeof expression === 'string' ? `“${expression}”` : JSON.stringify(expression)
  return { source: value(node.left) || '选择数据', comparison: `${PREDICATE_LABELS[node.op] || node.op}${['is_true', 'is_false', 'exists', 'not_exists'].includes(node.op) ? '' : ` ${value(node.right)}`}` }
}
export function predicateSummary(node, sources = [], rules = [], triggers = []) {
  if (!node) return '未设置条件'
  if (['all', 'any', 'not'].includes(node.op)) {
    const children = (node.children || []).map(child => predicateSummary(child, sources, rules, triggers))
    return node.op === 'not' ? `NOT (${children.join(' 且 ')})` : `(${children.join(node.op === 'all' ? ' 且 ' : ' 或 ')})`
  }
  if (node.op === 'event_occurred') return `${triggers.find(trigger => trigger.id === node.trigger_id)?.label || node.trigger_id || '选择事件来源'} · 事件${node.expected === false ? '未发生' : '已发生'}`
  if (node.op === 'run_status') return `${rules.find(rule => rule.rule_id === node.rule_id)?.name || node.rule_id || '选择自动化'} ${node.comparison === 'ne' ? '不是' : '是'} ${RUN_STATUS_OPTIONS.find(([value]) => value === node.status)?.[1] || node.status || '选择状态'}`
  const parts = predicateSummaryParts(node, sources)
  return `${parts.source} ${parts.comparison}`
}
