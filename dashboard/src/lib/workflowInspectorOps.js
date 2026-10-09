function reachable(workflow, omitted) {
  const result = new Set()
  function visit(id) {
    if (!id || result.has(id)) return
    result.add(id)
    for (const edge of workflow.edges) if (edge !== omitted && edge.source === id) visit(edge.target)
  }
  visit(workflow.entry)
  return result
}
export function branchRemovalImpact(workflow, group, branch) {
  const edge = workflow.edges.find(edge => edge.source === group.binding_id && edge.port === branch.id)
  const before = reachable(workflow), after = reachable(workflow, edge)
  return { edge, nodes: workflow.nodes.filter(node => before.has(node.binding_id) && !after.has(node.binding_id)) }
}
export function hasExternalRemovalReferences(rule, nodes, branch) {
  const identities = new Set(), excluded = new Set(nodes)
  function collect(value) {
    if (!value || typeof value !== 'object') return
    if (value.binding_id) identities.add(value.binding_id)
    Object.values(value).forEach(value => Array.isArray(value) ? value.forEach(collect) : collect(value))
  }
  nodes.forEach(collect)
  function inspect(value) {
    if (!value || typeof value !== 'object' || excluded.has(value) || value === branch) return false
    if (identities.has(value.$ref?.node) || identities.has(value.trigger_id)) return true
    return Object.values(value).some(value => Array.isArray(value) ? value.some(inspect) : inspect(value))
  }
  return inspect(rule)
}
export function removeWorkflowBranch(workflow, group, branch, { destination, deleteNodes = false } = {}) {
  const impact = branchRemovalImpact(workflow, group, branch), ids = new Set(impact.nodes.map(node => node.binding_id))
  if (impact.nodes.length && !deleteNodes) {
    if (branchMigrationBlock(workflow, group, branch, destination)) return false
    if (!destination || destination === branch.id || !group.branches.some(branch => branch.id === destination)) return false
    const next = workflow.edges.find(edge => edge.source === group.binding_id && edge.port === destination)
    if (!next) return false
    const target = next.target
    next.target = impact.edge.target
    for (const edge of workflow.edges) if (ids.has(edge.source) && !ids.has(edge.target)) edge.target = target
  }
  if (deleteNodes) {
    workflow.nodes = workflow.nodes.filter(node => !ids.has(node.binding_id))
    workflow.edges = workflow.edges.filter(edge => !ids.has(edge.source))
    for (const edge of workflow.edges) if (ids.has(edge.target)) edge.target = null
  }
  group.branches.splice(group.branches.indexOf(branch), 1)
  workflow.edges = workflow.edges.filter(edge => edge.source !== group.binding_id || edge.port !== branch.id)
  return true
}
export function branchMigrationBlock(workflow, group, branch, destination) {
  const impact = branchRemovalImpact(workflow, group, branch)
  const next = workflow.edges.find(edge => edge.source === group.binding_id && edge.port === destination)
  return next?.target && impact.nodes.some(node => node.type === 'end')
    ? '这条路径包含明确结束节点，不能接到已有动作之前。请选择空出口、先移除结束节点，或删除这条独占路径。'
    : ''
}
export function removalReferences(rule, nodes, ignored) {
  const identities = new Set(), excluded = new Set(nodes), result = []
  function collect(value) {
    if (!value || typeof value !== 'object') return
    if (value.binding_id) identities.add(value.binding_id)
    Object.values(value).forEach(value => Array.isArray(value) ? value.forEach(collect) : collect(value))
  }
  nodes.forEach(collect)
  function inspect(value, path = [], owner = null) {
    if (!value || typeof value !== 'object' || excluded.has(value) || value === ignored) return
    owner = value.binding_id || owner
    if (identities.has(value.$ref?.node)) result.push({ owner, node: value.$ref.node, path, kind: 'data' })
    if (identities.has(value.trigger_id)) result.push({ owner, node: value.trigger_id, path, kind: 'event' })
    Object.entries(value).forEach(([key, value]) => Array.isArray(value) ? value.forEach((item, index) => inspect(item, [...path, key, index], owner)) : inspect(value, [...path, key], owner))
  }
  inspect(rule)
  return result
}
export function nodeRemovalImpact(workflow, node, port) {
  const outgoing = workflow.edges.filter(edge => edge.source === node.binding_id)
  const targets = [...new Set(outgoing.map(edge => edge.target))]
  if (port === undefined && targets.length > 1) return null
  const selected = outgoing.find(edge => edge.port === port)
  if (port !== undefined && port !== null && !selected) return null
  const target = port === null ? null : selected?.target ?? (targets.length === 1 ? targets[0] : null)
  const edges = workflow.edges.filter(edge => edge.source !== node.binding_id).map(edge => edge.target === node.binding_id ? { ...edge, target } : { ...edge })
  const entry = workflow.entry === node.binding_id ? target : workflow.entry
  const before = reachable(workflow), after = reachable({ entry, edges })
  const detached = workflow.nodes.filter(item => item !== node && before.has(item.binding_id) && !after.has(item.binding_id))
  const ids = new Set([node.binding_id, ...detached.map(node => node.binding_id)])
  return { node, target, detached, entry, edges: edges.filter(edge => !ids.has(edge.source)), nodes: workflow.nodes.filter(node => !ids.has(node.binding_id)) }
}
function entryActivation(rule, replacement) {
  let graph = rule.workflow === replacement.original ? replacement.graph : rule.workflow
  const seen = new Set()
  while (graph?.entry && !seen.has(graph.entry)) {
    seen.add(graph.entry)
    const node = graph.nodes.find(node => node.binding_id === graph.entry)
    if (node?.type === 'event_group') return node.activation
    if (node?.type !== 'action_group') return null
    graph = node.workflow === replacement.original ? replacement.graph : node.workflow
  }
  return null
}
export function nodeRemovalBlock(rule, workflow, impact) {
  if (!impact) return '请选择保留的出口。'
  if (!rule.condition && entryActivation(rule, {}) && !entryActivation(rule, { original: workflow, graph: impact })) return '这个组提供流程的启动事件。请先在续接的入口事件组配置启动事件，再删除此组。'
  return removalReferences(rule, [impact.node, ...impact.detached]).length ? '其他节点仍引用将被移除的数据或事件，请先调整下面列出的引用。' : ''
}
export function removeWorkflowNode(rule, workflow, node, { port } = {}) {
  const impact = nodeRemovalImpact(workflow, node, port)
  if (!impact || nodeRemovalBlock(rule, workflow, impact)) return false
  workflow.entry = impact.entry
  workflow.nodes = impact.nodes
  workflow.edges = impact.edges
  return true
}
export function removeEventSource(rule, group, value) {
  const references = removalReferences(rule, [value])
  if (references.length) return references
  function prune(node) {
    if (node === value) return null
    if (!node?.children) return node
    const children = node.children.map(prune).filter(Boolean)
    if (children.length === node.children.length && children.every((child, index) => child === node.children[index])) return node
    if (!children.length) return null
    if (children.length === 1 && node.op !== 'not') return children[0]
    node.children = children
    return node
  }
  if (group.activation) {
    const activation = prune(group.activation)
    if (activation) group.activation = activation
    else delete group.activation
  }
  group.triggers = (group.triggers || []).filter(trigger => trigger !== value)
  return []
}
export function removeConditionNode(graph, id) {
  graph.nodes = graph.nodes.filter(node => node.id !== id)
  graph.edges = graph.edges.filter(edge => edge.source !== id && edge.target !== id)
  if (graph.root === id) graph.root = null
  const remaining = new Set()
  function visit(id) {
    if (!id || remaining.has(id)) return
    remaining.add(id)
    graph.edges.filter(edge => edge.target === id).forEach(edge => visit(edge.source))
  }
  visit(graph.root)
  graph.nodes = graph.nodes.filter(node => remaining.has(node.id))
  graph.edges = graph.edges.filter(edge => remaining.has(edge.source) && remaining.has(edge.target))
}
export function connectActivationNode(group, sourceValue, targetValue) {
  const root = group?.activation
  if (!root || !sourceValue || !targetValue) return '没有可连接的触发组合。'
  function locate(value, sought, parent = null) {
    if (value === sought) return { value, parent }
    for (const child of value?.children || []) { const found = locate(child, sought, value); if (found) return found }
    return null
  }
  const source = locate(root, sourceValue), target = locate(root, targetValue)
  if (!source || !target) return '只能连接同一个事件组中的触发条件。'
  if (!['all', 'any', 'not'].includes(targetValue.op)) return '触发条件只能连接到 AND、OR 或 NOT 组合。'
  if (sourceValue === targetValue || locate(sourceValue, targetValue)) return '不能把组合连接到自身或其内部条件。'
  if (source.parent === targetValue) return ''
  if (!source.parent) return '根触发组合不能移入自己的内部条件。'
  if (source.parent.children.length <= 1) return '移动后原组合会没有事件。请先为原组合添加其他事件，或在侧栏明确移除原组合。'
  if (targetValue.op === 'not' && (!sourceValue.type || targetValue.children?.length)) return 'NOT 只能包含一个事件叶，不能接入多个事件或另一个组合。'
  if (!Array.isArray(targetValue.children)) return '目标组合缺少事件列表，请先修正目标组合。'
  source.parent.children.splice(source.parent.children.indexOf(sourceValue), 1)
  targetValue.children.push(sourceValue)
  return ''
}
