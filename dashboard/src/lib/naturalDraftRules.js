import { getParamDefs, isConditionLeaf, normalizeRuleDraft } from './utils.js'

export function normalizeDraftResult(result) {
  if (result?.result_type !== 'rule_draft' || !result.draft) return result
  return { ...result, draft: normalizeRuleDraft(JSON.parse(JSON.stringify(result.draft))) }
}

function conditionNodes(node) {
  if (!node || typeof node !== 'object') return []
  return [node, ...(node.children || []).flatMap(conditionNodes)]
}

function actionNodes(actions) {
  return (actions || []).flatMap(node => [
    node,
    ...['then', 'else', 'failure_actions'].flatMap(field => actionNodes(node[field])),
  ])
}

export function conditionLabel(node, schema) {
  if (!node) return '还没听清什么时候开始'
  if (isConditionLeaf(node)) return schema.triggers?.[node.type]?.name || node.type
  const children = (node.children || []).map(child => conditionLabel(child, schema))
  if (node.op === 'not') return `${children.join('、')} 未发生`
  const label = children.join(node.op === 'all' ? ' 且 ' : ' 或 ')
  return label || '待补充触发条件'
}

export function actionNames(draft, schema) {
  return (draft?.actions || []).map(node => schema.actions?.[node.type]?.name || node.type).filter(Boolean)
}

export function ruleNodeCount(draft) {
  if (!draft) return ''
  return `${conditionNodes(draft.condition).length + actionNodes(draft.actions).length} 个节点`
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
  conditionNodes(draft.condition).filter(isConditionLeaf).forEach(node => {
    push(conditionLabel(node, schema), node, schema.triggers)
  })
  actionNodes(draft.actions).forEach((node, index) => {
    push(`动作 ${index + 1}`, node, schema.actions)
  })
  return rows
}

export function ruleSummary(result, schema) {
  const draft = result?.draft
  if (result?.result_type !== 'rule_draft' || !draft) {
    return String(result?.message || result?.content || result?.error || '我还需要一点信息，才能继续起草。').trim().slice(0, 4000)
  }
  return `已生成规则草稿：当 ${conditionLabel(draft.condition, schema)}，然后 ${actionNames(draft, schema).join('、') || '待补充动作'}。`.slice(0, 4000)
}

export function draftEditsCurrentRule(draft, rule) {
  if (!rule || !draft) return false
  if (draft.rule_id && draft.rule_id === rule.rule_id) return true
  const nodes = value => [...conditionNodes(value.condition), ...actionNodes(value.actions)]
  const known = new Set(nodes(rule).map(node => node.binding_id).filter(Boolean))
  return nodes(draft).some(node => node.binding_id && known.has(node.binding_id))
}

export function hasHighImpactActions(draft, schema) {
  return actionNodes(draft?.actions).flatMap((node, index) => (
    (schema.actions?.[node.type]?.permissions || []).includes('admin')
      ? [`动作 ${index + 1} 需要管理员权限`] : []
  ))
}

export function nodeIdForChange(item, rule) {
  if (!rule || item.op === 'add') return null
  if (item.target === 'action') {
    const node = rule.actions?.[item.index]
    return node?.binding_id ? `action-${node.binding_id}` : null
  }
  if (item.target === 'failure-action') {
    const parent = rule.actions?.[item.parentIndex]
    const node = parent?.failure_actions?.[item.index]
    return node?.binding_id ? `failure-action-${node.binding_id}`
      : parent?.binding_id ? `action-${parent.binding_id}` : null
  }
  if (item.target === 'trigger') {
    return isConditionLeaf(rule.condition)
      ? rule.condition.binding_id ? `trigger-${rule.condition.binding_id}` : null
      : 'condition-root'
  }
  return null
}
