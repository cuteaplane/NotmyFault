import { walkGraphActions, getGraphTriggers, findGraphNode } from './workflowGraph.js'
import { getParamDefs, validateRuleGraph } from './utils.js'

export function normalizeDraftResult(result) {
  if (result?.result_type !== 'rule_draft' || !result.draft) return result
  return { ...result, draft: validateRuleGraph(JSON.parse(JSON.stringify(result.draft))) }
}

function draftActions(draft) { const result = []; walkGraphActions(draft?.workflow, node => { if (!['trigger', 'logic', 'condition', 'event_group', 'action_group', 'merge', 'end', 'set_variable'].includes(node.type)) result.push(node) }); return result }
function draftTriggers(draft) { return getGraphTriggers(draft || {}) }
export function conditionLabel(node, schema) { return node ? schema.triggers?.[node.type]?.name || node.type : '待连接触发器' }

export function actionNames(draft, schema) {
  return draftActions(draft).map(node => schema.actions?.[node.type]?.name || node.type).filter(Boolean)
}

export function ruleNodeCount(draft) {
  if (!draft) return ''
  return `${draft.workflow?.nodes?.length || 0} 个节点`
}

export function ruleParamRows(draft, schema) {
  if (!draft) return []
  const rows = []
  const push = (label, node, catalog) => {
    const definitions = new Map(getParamDefs(catalog?.[node.type]).map(param => [param.name, param]))
    Object.entries(node.params || {}).forEach(([key, value]) => {
      rows.push({
        node: label, key,
        value: definitions.get(key)?.sensitive ? '***' : typeof value === 'string' ? value : JSON.stringify(value),
      })
    })
  }
  draftTriggers(draft).forEach(node => {
    push(conditionLabel(node, schema), node, schema.triggers)
  })
  draftActions(draft).forEach((node, index) => {
    push(`动作 ${index + 1}`, node, schema.actions)
  })
  return rows
}

export function ruleSummary(result, schema) {
  const draft = result?.draft
  if (result?.result_type !== 'rule_draft' || !draft) {
    return String(result?.message || result?.content || result?.error || '我还需要一点信息，才能继续起草。').trim().slice(0, 4000)
  }
  return `已生成规则草稿：${draftTriggers(draft).map(trigger => conditionLabel(trigger, schema)).join('、') || '待连接触发器'}；动作包括 ${actionNames(draft, schema).join('、') || '待添加动作'}。`.slice(0, 4000)
}

export function draftEditsCurrentRule(draft, rule) {
  if (!rule || !draft) return false
  if (draft.rule_id && draft.rule_id === rule.rule_id) return true
  const nodes = value => [...draftTriggers(value), ...draftActions(value)]
  const known = new Set(nodes(rule).map(node => node.binding_id).filter(Boolean))
  return nodes(draft).some(node => node.binding_id && known.has(node.binding_id))
}

export function hasHighImpactActions(draft, schema) {
  return draftActions(draft).flatMap((node, index) => (
    (schema.actions?.[node.type]?.permissions || []).includes('admin')
      ? [`动作 ${index + 1} 需要管理员权限`] : []
  ))
}

export function nodeIdForChange(item, rule) {
  if (!rule || item.op === 'add') return null
  if (item.node_id) return `action-${item.node_id}`
  if (item.target === 'action') {
    const node = rule.workflow?.nodes?.[item.index]
    return node?.binding_id ? `action-${node.binding_id}` : null
  }
  if (item.target === 'trigger') {
    const node = rule.workflow.nodes.find(node => node.type === 'trigger')
    return node ? `action-${node.binding_id}` : null
  }
  return null
}
