const clone = value => JSON.parse(JSON.stringify(value))
export const graphId = (prefix = 'a') => `${prefix}_${globalThis.crypto?.randomUUID?.().replaceAll('-', '').slice(0, 12) || Math.random().toString(16).slice(2).padEnd(12, '0')}`
export const isGroup = node => ['event_group', 'action_group'].includes(node?.type)
export const isCondition = node => ['trigger', 'condition', 'logic', 'event_group'].includes(node?.type)
export const groupTypeLabel = type => ({ if: 'IF', all: 'AND', any: 'OR', not: 'NOT' })[type] || type
export function outputPort(node) { return node.group_type === 'if' ? node.branches[0].id : node.type === 'trigger' ? 'event' : ['logic', 'condition', 'event_group'].includes(node.type) ? 'result' : 'next' }
export function insertionParent(workflow, context) {
  if (context.parent !== undefined) return context.parent
  const edge = context.edge?.rawEdge
  if (edge) {
    const source = workflow.nodes.find(node => node.binding_id === edge.source)
    const target = workflow.nodes.find(node => node.binding_id === edge.target)
    return edge.channel === 'condition' ? target?.type === 'event_group' ? target.binding_id : target?.parent || null : edge.port === 'entry' ? source.binding_id : source?.parent || null
  }
  return context.node?.entryGate ? context.node.raw.binding_id : context.kind === 'background' ? null : context.node?.raw?.parent || null
}

export function normalizeWorkflow(rule) {
  if (['condition', 'event', 'trigger', 'actions', 'preconditions', '_matcher'].some(key => Object.hasOwn(rule, key))) throw new Error('旧条件模型已移除，请使用流程图规则')
  if (!rule.workflow || !Array.isArray(rule.workflow.nodes) || !Array.isArray(rule.workflow.edges)) throw new Error('规则必须提供 workflow 图')
  if (rule.workflow.nodes.some(node => node.type === 'if' || ['activation', 'condition_graph', 'workflow', 'then', 'elif', 'else', 'triggers', 'failure_actions'].some(key => Object.hasOwn(node, key)))) throw new Error('旧条件模型已移除，请重新设计事件组')
  const graph = rule.workflow, nodes = new Map(), seenIds = new Set()
  if (graph.entry != null) throw new Error('平面流程图的入口由连线确定，entry 必须为 null')
  for (const node of graph.nodes) {
    if (!node || typeof node !== 'object' || Array.isArray(node) || !node.type) throw new Error('图块必须提供类型')
    if (node.binding_id) {
      if (!/^a_[a-z0-9_]{6,64}$/.test(node.binding_id) || seenIds.has(node.binding_id)) throw new Error('图块身份无效或重复')
      seenIds.add(node.binding_id); nodes.set(node.binding_id, node)
    }
    if (node.type === 'trigger') {
      if (!node.trigger || typeof node.trigger !== 'object' || !node.trigger.type) throw new Error('触发器图块必须提供插件触发器')
      const id = node.trigger.binding_id
      if (id && (!/^t_[a-z0-9_]{6,64}$/.test(id) || seenIds.has(id))) throw new Error('触发器身份无效或重复')
      if (id) seenIds.add(id)
    }
    if (node.type === 'condition' && (!node.predicate || 'children' in node.predicate || !['eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'contains', 'is_true', 'is_false', 'exists', 'not_exists', 'run_status'].includes(node.predicate.op))) throw new Error('条件图块只接受数据比较或自动化状态判断')
    if (node.type === 'logic' && !['all', 'any', 'not'].includes(node.op) || node.type === 'event_group' && !['if', 'all', 'any', 'not'].includes(node.group_type)) throw new Error('逻辑节点或事件组的类型无效')
    for (const key of ['within_seconds', 'duration_seconds', 'timeout_seconds']) if (Object.hasOwn(node, key) && (!Number.isFinite(node[key]) || node[key] <= 0 || node[key] > 86400)) throw new Error('时间范围必须是大于零且不超过 86400 的有限秒数')
    if (node.position && !['x', 'y'].every(key => Number.isFinite(node.position[key]))) throw new Error('图块位置必须是有限数字坐标')
    if (node.type === 'event_group' && node.group_type === 'if') {
      const branches = node.branches || [], ids = new Set()
      if (branches.length < 2 || branches.filter(branch => branch.else).length !== 1 || !branches.at(-1)?.else) throw new Error('IF 事件组必须有 IF 和末尾唯一 ELSE')
      for (const branch of branches) {
        if (!/^b_[a-z0-9_]+$/.test(branch.id || '') || ids.has(branch.id) || ['condition', 'condition_graph', 'then'].some(key => Object.hasOwn(branch, key))) throw new Error('事件组分支无效')
        ids.add(branch.id)
      }
    }
  }
  for (const node of graph.nodes) {
    let parent = node.parent
    const visited = new Set([node.binding_id])
    while (parent) {
      if (visited.has(parent)) throw new Error('组不能包含自身或祖先组')
      visited.add(parent)
      if (!isGroup(nodes.get(parent))) throw new Error('图块所属组不存在')
      parent = nodes.get(parent).parent
    }
  }
  const handlers = new Set(graph.edges.filter(edge => edge.channel === 'control' && edge.port === 'failure').map(edge => edge.target))
  const outlets = new Set(), pairs = new Set(), counts = new Map(), adjacency = new Map()
  for (const edge of graph.edges) {
    const source = nodes.get(edge.source), target = nodes.get(edge.target), input = edge.target_port || 'input'
    if (!source || !target || !['control', 'condition'].includes(edge.channel)) throw new Error('连线引用的节点或类型无效')
    const ports = source.group_type === 'if' ? source.branches.map(branch => branch.id) : source.type === 'trigger' ? ['event'] : ['condition', 'logic', 'event_group'].includes(source.type) ? ['result'] : source.type === 'action_group' ? ['next', 'entry'] : source.type === 'end' ? [] : source.type === 'merge' ? ['next'] : ['next', 'failure']
    if (!ports.includes(edge.port)) throw new Error('连线来源端口不存在')
    const pair = JSON.stringify([edge.source, edge.port, edge.target, input, edge.channel])
    if (pairs.has(pair)) throw new Error('连线重复')
    pairs.add(pair)
    if (edge.channel === 'condition') {
      if (!isCondition(source) || !['logic', 'event_group'].includes(target.type)) throw new Error('条件端口类型不匹配')
      if (target.group_type === 'if') {
        const branch = target.branches.find(branch => branch.id === input)
        if (!branch || branch.else) throw new Error('ELSE 不接受触发器或条件')
      } else if (input !== 'input') throw new Error('目标条件端口不存在')
      const key = `${edge.target}:${input}`, count = (counts.get(key) || 0) + 1
      counts.set(key, count)
      if (count > 1 && (target.group_type === 'if' || (target.op || target.group_type) === 'not')) throw new Error('IF／ELIF／NOT 只接一个条件')
    } else {
      const key = `${edge.source}:${edge.port}`
      if (outlets.has(key)) throw new Error('同一控制出口只能接一个目标')
      outlets.add(key)
      if (input !== 'input' || ['logic', 'condition'].includes(target.type)) throw new Error('控制流只能连接执行入口')
      if (edge.port === 'entry') {
        if (source.type !== 'action_group' || actionGroupScope(graph, edge.target) !== edge.source) throw new Error('动作组入口只能连接组内节点')
      } else if (actionGroupScope(graph, edge.source) !== actionGroupScope(graph, edge.target)) throw new Error('控制流不能跨越动作组边界')
      if (edge.port === 'failure' && target.type !== 'action_group') throw new Error('失败出口只能连接动作组')
      if (handlers.has(edge.target) && edge.port !== 'failure') throw new Error('补救动作组不能连接主流程入口')
      if (handlers.has(edge.source) && edge.port === 'next') throw new Error('补救动作组不能连接外部后继')
    }
    if (!adjacency.has(edge.source)) adjacency.set(edge.source, [])
    adjacency.get(edge.source).push(edge.target)
  }
  for (const node of graph.nodes) if (node.group_type === 'if') for (const branch of node.branches) {
    if (!branch.else && graph.edges.some(edge => edge.source === node.binding_id && edge.port === branch.id && edge.channel === 'control') && !counts.get(`${node.binding_id}:${branch.id}`)) throw new Error('已使用的 IF／ELIF 分支必须连接条件')
  }
  const complete = new Set(), visiting = new Set()
  function checkCycle(id) {
    if (visiting.has(id)) throw new Error('连线不能形成循环')
    if (complete.has(id)) return
    visiting.add(id)
    for (const target of adjacency.get(id) || []) checkCycle(target)
    visiting.delete(id); complete.add(id)
  }
  for (const id of nodes.keys()) checkCycle(id)
  return rule.workflow
}
export function walkGraphActions(workflow, visit) {
  const nodes = workflow?.nodes || [], byId = new Map(nodes.map(node => [node.binding_id, node]))
  nodes.forEach((node, index) => {
    const trail = [], seen = new Set([node.binding_id])
    let parent = byId.get(node.parent)
    while (parent && !seen.has(parent.binding_id)) {
      seen.add(parent.binding_id)
      trail.unshift({ action: parent, index: nodes.indexOf(parent), field: 'workflow' })
      parent = byId.get(parent.parent)
    }
    visit(node, index, trail, workflow)
  })
}
export function findGraphNode(rule, identity) {
  let result
  walkGraphActions(rule.workflow, (action, index, trail, workflow) => { if (action.binding_id === identity) result = { action, index, trail, workflow } })
  return result
}
export function findRecoveryGroup(workflow, action) {
  const identity = typeof action === 'string' ? action : action?.binding_id
  const edge = workflow.edges.find(edge => edge.source === identity && edge.port === 'failure' && edge.channel === 'control')
  return edge && workflow.nodes.find(node => node.binding_id === edge.target && node.type === 'action_group')
}
export function createRecoveryGroup(workflow, action) {
  const existing = findRecoveryGroup(workflow, action)
  if (existing) return existing
  const group = createGraphAction('action_group')
  group.name = '失败补救'
  if (action.parent) group.parent = action.parent
  group.position = { x: (action.position?.x || 64) + 340, y: (action.position?.y || 118) + 240 }
  workflow.nodes.push(group)
  const error = connectGraph(workflow, action.binding_id, 'failure', group.binding_id, 'input', 'control')
  if (error) { workflow.nodes.pop(); throw new Error(error) }
  return group
}
export function getGraphTriggers(rule) { return (rule.workflow?.nodes || []).filter(node => node.type === 'trigger').map(node => node.trigger) }
export function graphPredecessors(workflow, identity) {
  const found = new Set(), visited = new Set(), byId = new Map((workflow?.nodes || []).map(node => [node.binding_id, node]))
  function visit(target) {
    if (visited.has(target)) return
    visited.add(target)
    for (const edge of workflow?.edges || []) if (edge.target === target && edge.channel === 'control') {
      visit(edge.source)
      if (!['entry', 'failure'].includes(edge.port)) found.add(edge.source)
      if (edge.port === 'next') {
        const handler = findRecoveryGroup(workflow, edge.source)
        if (handler && byId.get(edge.source)?.on_error === 'continue') found.add(handler.binding_id)
      }
    }
    const parent = byId.get(target)?.parent
    if (parent) visit(parent)
  }
  visit(identity)
  found.delete(identity)
  return [...found]
}
export function conditionPredecessors(workflow, identity) {
  const found = new Set(graphPredecessors(workflow, identity)), visited = new Set([identity]), pending = [identity]
  while (pending.length) {
    const current = pending.pop()
    for (const edge of workflow.edges) if (edge.channel === 'condition' && edge.source === current && !visited.has(edge.target)) {
      visited.add(edge.target); pending.push(edge.target)
      graphPredecessors(workflow, edge.target).forEach(source => found.add(source))
    }
  }
  found.delete(identity)
  return [...found]
}
export function actionGroupScope(workflow, identity) {
  const byId = new Map(workflow.nodes.map(node => [node.binding_id, node]))
  let parent = byId.get(identity)?.parent
  const seen = new Set()
  while (parent && !seen.has(parent)) {
    seen.add(parent)
    const group = byId.get(parent)
    if (group?.type === 'action_group') return parent
    parent = group?.parent
  }
  return null
}
export function graphExecutionActions(rule, { startStepId = '', endStepId = '' } = {}) {
  const nodes = rule.workflow?.nodes || [], found = new Set(), result = []
  function visit(identity) {
    if (!identity || found.has(identity)) return
    const node = nodes.find(node => node.binding_id === identity)
    if (!node) return
    found.add(identity); result.push(node)
    if (identity !== endStepId) rule.workflow.edges.filter(edge => edge.source === identity && edge.channel === 'control').forEach(edge => visit(edge.target))
  }
  if (startStepId) visit(startStepId)
  else {
    function hasTrigger(identity, visited = new Set()) {
      if (visited.has(identity)) return false
      visited.add(identity)
      const node = nodes.find(node => node.binding_id === identity)
      if (node?.type === 'trigger') return true
      const inputs = rule.workflow.edges.filter(edge => node?.type === 'action_group' ? edge.source === identity && edge.port === 'entry' : edge.target === identity && edge.channel === 'condition')
      return inputs.some(edge => hasTrigger(node?.type === 'action_group' ? edge.target : edge.source, visited))
    }
    nodes.filter(node => ['trigger', 'logic', 'event_group', 'action_group'].includes(node.type) && !actionGroupScope(rule.workflow, node.binding_id) && rule.workflow.edges.some(edge => edge.source === node.binding_id && edge.channel === 'control') && !rule.workflow.edges.some(edge => edge.target === node.binding_id && edge.channel === 'control') && hasTrigger(node.binding_id)).forEach(node => visit(node.binding_id))
  }
  return result
}

export function makeGroupRoom(workflow, before, after, identity) {
  const boxes = new Map(after.filter(node => !node.gate).map(node => [node.raw.binding_id, { ...node }]))
  const previous = new Map(before.filter(node => !node.gate).map(node => [node.raw.binding_id, node]))
  const byId = new Map(workflow.nodes.map(node => [node.binding_id, node]))
  function shift(id, dx, dy) {
    const ids = new Set([id])
    for (const node of workflow.nodes) if (ids.has(node.parent)) ids.add(node.binding_id)
    let changed
    do { changed = false; for (const node of workflow.nodes) if (ids.has(node.parent) && !ids.has(node.binding_id)) { ids.add(node.binding_id); changed = true } } while (changed)
    for (const node of workflow.nodes) if (ids.has(node.binding_id)) {
      const box = boxes.get(node.binding_id), origin = node.position || box
      if (origin) node.position = { x: origin.x + dx, y: origin.y + dy }
      if (box) { box.x += dx; box.y += dy }
    }
  }
  let current = identity
  while (current) {
    const expanded = boxes.get(current), old = previous.get(current), owner = byId.get(current)
    if (expanded && (!old || expanded.width > old.width || expanded.height > old.height)) {
      const placed = [expanded]
      for (const node of workflow.nodes.filter(node => node.binding_id !== current && (node.parent || null) === (owner.parent || null)).sort((a, b) => (boxes.get(a.binding_id)?.x || 0) - (boxes.get(b.binding_id)?.x || 0))) {
        const box = boxes.get(node.binding_id)
        if (!box) continue
        for (const obstacle of placed) {
          if (box.x >= obstacle.x + obstacle.width + 32 || box.x + box.width + 32 <= obstacle.x || box.y >= obstacle.y + obstacle.height + 32 || box.y + box.height + 32 <= obstacle.y) continue
          if (box.x >= (old ? old.x + old.width : expanded.x) - 1 || box.x > obstacle.x + obstacle.width / 2) shift(node.binding_id, obstacle.x + obstacle.width + 44 - box.x, 0)
          else shift(node.binding_id, 0, obstacle.y + obstacle.height + 44 - box.y)
        }
        placed.push(box)
      }
    }
    current = owner?.parent
  }
}

export function arrangeWorkflow(workflow, expanded = new Set(), heights = new Map()) {
  const byId = new Map(workflow.nodes.map(node => [node.binding_id, node])), memo = new Map()
  function contents(parent) {
    const siblings = workflow.nodes.filter(node => (node.parent || null) === parent)
    const identities = new Set(siblings.map(node => node.binding_id)), levels = new Map(), visiting = new Set()
    function level(id) {
      if (levels.has(id)) return levels.get(id)
      if (visiting.has(id)) return 0
      visiting.add(id)
      const inputs = workflow.edges.filter(edge => edge.target === id && identities.has(edge.source))
      const value = inputs.length ? 1 + Math.max(...inputs.map(edge => level(edge.source))) : 0
      visiting.delete(id); levels.set(id, value); return value
    }
    siblings.forEach(node => level(node.binding_id))
    const columns = new Map()
    siblings.forEach(node => { const column = levels.get(node.binding_id); if (!columns.has(column)) columns.set(column, []); columns.get(column).push(node) })
    const placements = [], columnSizes = []
    for (const [column, members] of [...columns].sort(([a], [b]) => a - b)) {
      const branchOrder = node => {
        const incoming = workflow.edges.filter(edge => edge.target === node.binding_id && edge.channel === 'control')
        const orders = incoming.flatMap(edge => { const source = byId.get(edge.source); return source?.group_type === 'if' ? [source.branches.findIndex(branch => branch.id === edge.port)] : [] })
        return orders.length ? Math.min(...orders) : Number.MAX_SAFE_INTEGER
      }
      members.sort((a, b) => branchOrder(a) - branchOrder(b))
      const sizes = members.map(size)
      columnSizes.push({ members, sizes, width: Math.max(...sizes.map(item => item.width)), height: sizes.reduce((sum, item) => sum + item.height, 0) + (members.length - 1) * 44 })
    }
    const height = Math.max(0, ...columnSizes.map(item => item.height))
    let x = parent && byId.get(parent).type === 'action_group' ? 284 : 0
    for (const column of columnSizes) {
      let y = (height - column.height) / 2
      column.members.forEach((node, index) => { const dimensions = column.sizes[index]; placements.push({ node, x, y }); y += dimensions.height + 44 })
      x += column.width + 88
    }
    return { placements, width: Math.max(0, x - 88), height }
  }
  function size(node) {
    if (memo.has(node.binding_id)) return memo.get(node.binding_id)
    let result = { width: 228, height: heights.get(node.binding_id) || 146 }
    if (isGroup(node)) {
      const inner = contents(node.binding_id)
      if (expanded.has(node.binding_id)) result = { width: Math.max(284, inner.width + 56 + (node.type === 'event_group' ? node.group_type === 'if' ? 96 : 218 : 0)), height: Math.max(inner.height + 106, node.type === 'event_group' ? node.group_type === 'if' ? 158 + (visibleBranches(workflow, node, true).length - 1) * 52 : 210 : 210) }
      result.inner = inner
    }
    memo.set(node.binding_id, result); return result
  }
  function place(part, origin) {
    part.placements.forEach(({ node, x, y }) => {
      node.position = { x: origin.x + x, y: origin.y + y }
      if (isGroup(node)) place(size(node).inner, { x: node.position.x + 28, y: node.position.y + 78 })
    })
  }
  place(contents(null), { x: 64, y: 118 })
}
export function createGraphAction(type, params = {}) {
  const node = { type, binding_id: graphId() }
  if (type === 'event_group') return { ...node, group_type: 'if', branches: [{ id: graphId('b') }, { id: graphId('b') }, { id: graphId('b'), else: true }] }
  if (type === 'action_group') return { ...node, exports: {} }
  if (type === 'logic') return { ...node, op: params.op || 'all' }
  if (type === 'condition') return { ...node, predicate: { op: 'eq', left: { $ref: { scope: 'step', node: '', path: [] } }, right: '' } }
  if (type === 'set_variable') return { ...node, variable: '', value: '' }
  return { ...node, params }
}
export function createTriggerStep(trigger) { return { type: 'trigger', binding_id: graphId(), trigger, occurrence: 'occurred', absence_scope: 'run', duration_seconds: 10 } }
export function branchUsed(workflow, group, branch) {
  return workflow.edges.some(edge => edge.source === group.binding_id && edge.port === branch.id || edge.target === group.binding_id && edge.target_port === branch.id)
}
export function visibleBranches(workflow, group, open) {
  const branches = group.branches || []
  const firstSpare = branches.find((branch, index) => index > 0 && !branch.else && !branchUsed(workflow, group, branch))
  return branches.filter((branch, index) => index === 0 || branchUsed(workflow, group, branch) || open && (branch.else || branch === firstSpare))
}
export function ensureSpareBranch(workflow, group) {
  if (group.group_type !== 'if') return
  if (!group.branches.some((branch, index) => index > 0 && !branch.else && !branchUsed(workflow, group, branch))) group.branches.splice(group.branches.length - 1, 0, { id: graphId('b') })
}
export function switchGroupType(workflow, group, type) {
  if (group.group_type === type) return
  workflow.edges = workflow.edges.filter(edge => edge.source !== group.binding_id && edge.target !== group.binding_id)
  group.group_type = type
  group.branches = type === 'if' ? [{ id: graphId('b') }, { id: graphId('b') }, { id: graphId('b'), else: true }] : []
}
export function insertGraphNode(workflow, node, source = null, port = 'next', edge = null) {
  workflow.nodes.push(node)
  if (!source) return node
  if (edge?.channel === 'condition') {
    if (!['logic', 'event_group'].includes(node.type)) return node
    const first = node.group_type === 'if' ? node.branches[0].id : 'input'
    const input = { ...edge, target: node.binding_id, target_port: first }
    const output = { ...edge, source: node.binding_id, port: node.group_type === 'if' ? node.branches[0].id : 'result' }
    workflow.edges.splice(workflow.edges.indexOf(edge), 1, input, output)
    return node
  }
  const old = workflow.edges.find(edge => edge.source === source && edge.port === port && edge.channel === 'control')
  const target = old?.target
  if (old) old.target = node.binding_id
  else workflow.edges.push({ source, port, target: node.binding_id, channel: 'control' })
  if (target) workflow.edges.push({ source: node.binding_id, port: outputPort(node), target, channel: 'control' })
  return node
}
export function removeGraphNode(workflow, node) {
  if (!node?.binding_id || !workflow.nodes.some(item => item.binding_id === node.binding_id)) return false
  const removed = new Set([node.binding_id])
  let changed
  do { changed = false; for (const child of workflow.nodes) if (removed.has(child.parent) && !removed.has(child.binding_id)) { removed.add(child.binding_id); changed = true } } while (changed)
  workflow.nodes = workflow.nodes.filter(item => !removed.has(item.binding_id))
  workflow.edges = workflow.edges.filter(edge => !removed.has(edge.source) && !removed.has(edge.target))
}
export function connectGraph(workflow, source, port, target, targetPort = 'input', channel = null) {
  const from = workflow.nodes.find(node => node.binding_id === source), to = workflow.nodes.find(node => node.binding_id === target)
  if (!from || !to || source === target) return '连接目标无效'
  channel ||= isCondition(from) && (to.type === 'logic' || to.type === 'event_group' && (to.group_type !== 'if' || targetPort !== 'input')) ? 'condition' : 'control'
  if (workflow.edges.some(edge => edge.source === source && edge.port === port && edge.target === target && (edge.target_port || 'input') === targetPort && edge.channel === channel)) return ''
  if (channel === 'condition') {
    if (!isCondition(from) || !['logic', 'event_group'].includes(to.type)) return '条件端口类型不匹配'
    const input = workflow.edges.filter(edge => edge.target === target && edge.channel === 'condition' && (to.type !== 'event_group' || to.group_type !== 'if' || edge.target_port === targetPort))
    if (to.type === 'event_group' && to.group_type === 'if') {
      const branch = to.branches.find(branch => branch.id === targetPort)
      if (!branch || branch.else) return 'ELSE 不接受触发器或条件'
      if (input.length) return 'IF／ELIF 只接一个条件，请先连接 AND 或 OR'
    } else if ((to.op || to.group_type) === 'not' && input.length) return 'NOT 只接一个条件'
  }
  if (channel === 'control') {
    if (['logic', 'condition'].includes(to.type)) return '控制流不能接入条件节点'
    if (targetPort !== 'input') return '控制流只能连接执行入口'
    if (port === 'entry') {
      if (from.type !== 'action_group' || actionGroupScope(workflow, target) !== source) return '动作组入口只能连接组内节点'
    } else if (actionGroupScope(workflow, source) !== actionGroupScope(workflow, target)) return '控制流不能跨越动作组边界'
    const handler = workflow.edges.some(edge => edge.target === target && edge.port === 'failure' && edge.channel === 'control')
    if (port === 'failure') {
      if (isCondition(from) || ['action_group', 'merge', 'end'].includes(from.type) || to.type !== 'action_group') return '失败出口只能连接动作组'
      if (actionGroupScope(workflow, source) !== actionGroupScope(workflow, target)) return '补救动作组必须与原动作处于同一执行范围'
      if (workflow.edges.some(edge => edge.target === target && edge.channel === 'control' && edge.port !== 'failure') || workflow.edges.some(edge => edge.source === target && edge.port === 'next' && edge.channel === 'control')) return '补救动作组不能连接主流程入口或外部后继'
    } else {
      if (handler) return '补救动作组不能连接主流程入口'
      if (port === 'next' && workflow.edges.some(edge => edge.target === source && edge.port === 'failure' && edge.channel === 'control')) return '补救动作组按停止／继续设置返回，不能连接外部后继'
    }
  }
  const pending = [target], visited = new Set()
  while (pending.length) {
    const current = pending.pop()
    if (current === source) return '连接不能形成循环'
    if (visited.has(current)) continue
    visited.add(current); workflow.edges.filter(edge => edge.source === current).forEach(edge => pending.push(edge.target))
  }
  if (channel === 'control') workflow.edges = workflow.edges.filter(edge => edge.source !== source || edge.port !== port || edge.channel !== 'control')
  if (workflow.edges.some(edge => edge.source === source && edge.port === port && edge.target === target && edge.target_port === targetPort && edge.channel === channel)) return ''
  workflow.edges.push({ source, port, target, target_port: targetPort, channel })
  if (to.type === 'event_group') ensureSpareBranch(workflow, to)
  return ''
}
export function moveGraphNodes(workflow, identities, parent = null) {
  const selected = new Set(identities), all = new Set(identities)
  let changed
  do { changed = false; for (const node of workflow.nodes) if (all.has(node.parent) && !all.has(node.binding_id)) { all.add(node.binding_id); changed = true } } while (changed)
  if (parent && all.has(parent)) return '不能把组拖入自身或其内部组'
  const target = parent && workflow.nodes.find(node => node.binding_id === parent)
  if (parent && !isGroup(target)) return '目标不是事件组或动作组'
  const moving = workflow.nodes.filter(node => selected.has(node.binding_id) && !selected.has(node.parent) && (node.parent || null) !== parent)
  if (!moving.length) return ''
  workflow.edges = workflow.edges.filter(edge => all.has(edge.source) === all.has(edge.target))
  const movedSources = new Set(workflow.nodes.filter(node => all.has(node.binding_id)).flatMap(node => [node.binding_id, node.trigger?.binding_id]).filter(Boolean))
  function clearBindings(value, inside) {
    if (!value || typeof value !== 'object' || value.$literal || value.$nmf_value) return
    for (const [key, child] of Object.entries(value)) {
      if (child?.$ref && ['trigger', 'step'].includes(child.$ref.scope) && movedSources.has(child.$ref.node) !== inside) value[key] = ''
      else clearBindings(child, inside)
    }
  }
  workflow.nodes.forEach(node => clearBindings(node, all.has(node.binding_id)))
  moving.forEach(node => { if (parent) node.parent = parent; else delete node.parent })
  return ''
}
export function groupGraphNodes(workflow, identities, type = 'if') {
  const byId = new Map(workflow.nodes.map(node => [node.binding_id, node])), ids = new Set(identities)
  const parents = node => { const result = []; let parent = node.parent; while (parent && !result.includes(parent)) { result.push(parent); parent = byId.get(parent)?.parent } return [...result, null] }
  const selected = workflow.nodes.filter(node => ids.has(node.binding_id) && !parents(node).some(parent => ids.has(parent)))
  if (!selected.length) return null
  const group = createGraphAction('event_group')
  group.group_type = type; if (type !== 'if') group.branches = []
  group.parent = parents(selected[0]).find(parent => selected.every(node => parents(node).includes(parent))) || undefined
  group.position = { x: Math.min(...selected.map(node => node.position?.x ?? 80)) - 32, y: Math.min(...selected.map(node => node.position?.y ?? 120)) - 80 }
  workflow.nodes.push(group)
  moveGraphNodes(workflow, selected.map(node => node.binding_id), group.binding_id)
  return group
}
export function copyGraphNode(node, workflow) {
  const originals = [node], selected = new Set([node.binding_id]), ids = new Map()
  if (workflow && !isGroup(node)) { const handler = findRecoveryGroup(workflow, node); if (handler) { originals.push(handler); selected.add(handler.binding_id) } }
  if (workflow && originals.some(isGroup)) for (let i = 0; i < originals.length; i++) for (const child of workflow.nodes) if (child.parent === originals[i].binding_id) { originals.push(child); selected.add(child.binding_id) }
  function duplicate(original) {
    const copy = clone(original)
    copy.binding_id = graphId(); ids.set(original.binding_id, copy.binding_id)
    if (copy.trigger) { copy.trigger.binding_id = graphId('t'); ids.set(original.trigger.binding_id, copy.trigger.binding_id) }
    if (copy.branches) copy.branches = original.branches.map(branch => { const id = graphId('b'); ids.set(branch.id, id); return { ...branch, id } })
    return copy
  }
  function rebind(value) {
    if (!value || typeof value !== 'object' || value.$literal || value.$nmf_value) return
    if (value.$ref && ids.has(value.$ref.node)) value.$ref.node = ids.get(value.$ref.node)
    for (const child of Object.values(value)) rebind(child)
  }
  const copies = originals.map(duplicate)
  copies.forEach(copy => { if (ids.has(copy.parent)) copy.parent = ids.get(copy.parent); rebind(copy) })
  const copy = copies[0]
  if (workflow && originals.length > 1) {
    copy._copyNodes = copies.slice(1)
    copy._copyEdges = workflow.edges.filter(edge => selected.has(edge.source) && selected.has(edge.target)).map(edge => ({ ...edge, source: ids.get(edge.source), target: ids.get(edge.target), port: ids.get(edge.port) || edge.port, target_port: ids.get(edge.target_port) || edge.target_port }))
  }
  return copy
}

export function buildWorkflowGraph(rule, expanded, schema, presentation) {
  const workflow = rule.workflow, raw = workflow?.nodes || [], byId = new Map(raw.map(node => [node.binding_id, node]))
  const nodes = [], edges = [], inspectionNodes = [], width = 228
  const ancestors = node => { const found = []; let current = byId.get(node.parent); while (current && !found.includes(current)) { found.push(current); current = byId.get(current.parent) } return found }
  const hiddenBy = node => ancestors(node).filter(parent => !expanded.has(parent.binding_id)).at(-1)
  const positions = new Map(raw.map((node, index) => [node.binding_id, node.position || { x: 64 + index % 4 * 340, y: 118 + Math.floor(index / 4) * 240 }]))
  const groupBoxes = new Map()
  function ifPorts(group, y, open = true) {
    let previous = 78
    return visibleBranches(workflow, group, open).map((branch, index) => {
      const wire = workflow.edges.find(edge => edge.target === group.binding_id && edge.target_port === branch.id && edge.channel === 'condition')
      const source = wire && byId.get(wire.source)
      let desired = 130 + index * 52
      if (source?.parent === group.binding_id) {
        const sourceBox = isGroup(source) && expanded.has(source.binding_id) ? box(source) : null
        desired = sourceBox ? sourceBox.y + sourceBox.height / 2 - y : positions.get(source.binding_id).y + 58 - y
      }
      previous = open ? Math.max(index ? previous + 52 : 100, desired) : 103 + index * 28
      return { id: branch.id, label: branch.label || (branch.else ? 'ELSE' : group.branches.indexOf(branch) === 0 ? 'IF' : `ELIF ${group.branches.indexOf(branch)}`), y: previous, branch }
    })
  }
  function box(group) {
    if (presentation.frozenGroups?.has(group.binding_id)) return presentation.frozenGroups.get(group.binding_id)
    if (groupBoxes.has(group.binding_id)) return groupBoxes.get(group.binding_id)
    const position = positions.get(group.binding_id), children = raw.filter(node => node.parent === group.binding_id)
    let x = position.x, y = position.y, right = x + 28, bottom = y + 78
    for (const child of children) {
      const childHeight = (presentation.nodeHeight?.({ id: `action-${child.binding_id}` }) || 170)
      const childBox = isGroup(child) && expanded.has(child.binding_id) ? box(child) : { ...positions.get(child.binding_id), width, height: childHeight }
      x = Math.min(x, childBox.x - 28); y = Math.min(y, childBox.y - 78)
      right = Math.max(right, childBox.x + childBox.width + 28); bottom = Math.max(bottom, childBox.y + childBox.height + 28)
    }
    const value = { x, y, width: Math.max(264, right - x + (group.type === 'event_group' ? group.group_type === 'if' ? 96 : 218 : 0)), height: Math.max(bottom - y, group.type === 'event_group' ? group.group_type === 'if' ? (ifPorts(group, y).at(-1)?.y || 130) + 28 : 210 : 206) }
    groupBoxes.set(group.binding_id, value); return value
  }
  const ancestry = node => ancestors(node).map(parent => ({ id: `action-${parent.binding_id}`, source: parent, workflow }))
  const labelFor = group => group.group_type === 'if' ? 'IF / ELIF / ELSE' : groupTypeLabel(group.group_type) || '动作组'
  for (const [index, source] of raw.entries()) {
    if (hiddenBy(source)) continue
    const group = isGroup(source), open = group && expanded.has(source.binding_id)
    const geometry = open ? box(source) : { ...positions.get(source.binding_id), width, height: 146 }
    const kind = source.type === 'trigger' ? 'trigger' : source.type === 'condition' ? 'predicate' : source.type === 'logic' ? 'condition-expression' : group ? source.type : 'action'
    const outputs = source.type === 'event_group' && source.group_type === 'if' ? ifPorts(source, geometry.y, open) : [{ id: source.type === 'trigger' ? 'event' : ['logic', 'condition', 'event_group'].includes(source.type) ? 'result' : 'next', label: '', y: 58 }]
    if (!['trigger', 'condition', 'logic', 'event_group', 'action_group', 'merge', 'end'].includes(source.type)) outputs.push({ id: 'failure', label: '失败', y: 90 })
    if (source.type === 'end') outputs.splice(0)
    const visibleSource = source.type === 'trigger' ? source.trigger : source.type === 'condition' ? source.predicate : source
    const node = { ...geometry, id: `action-${source.binding_id}`, raw: source, source: visibleSource, workflow, ancestry: ancestry(source), index, kind, frame: open, open, fixedHeight: open ? geometry.height : Math.max(116, 90 + outputs.length * 28), hasInput: source.type !== 'trigger', hasOutput: true, inputY: 58, outputs, label: source.name || (source.type === 'trigger' ? presentation.eventName(source.trigger) : source.type === 'condition' ? '条件判断' : source.type === 'logic' ? `${groupTypeLabel(source.op)}${source.negated ? ' · 取反' : ''}` : group ? labelFor(source) : presentation.actionName(source)), kicker: source.type === 'trigger' ? '触发器' : source.type === 'condition' ? '数据判断' : source.type === 'logic' ? '逻辑节点' : source.type === 'event_group' ? '事件组' : source.type === 'action_group' ? '动作组' : `动作 ${index + 1}`, meta: source.type === 'trigger' ? `${source.occurrence === 'absent' ? '事件未发生 · ' : ''}${presentation.describeItem(source.trigger, 'trigger')}` : source.type === 'condition' ? presentation.predicateSummary(source.predicate, source) : source.type === 'logic' ? '连接触发器、条件或事件组' : group ? `${raw.filter(node => node.parent === source.binding_id).length} 个内部图块` : presentation.describeItem(source, 'action'), icon: source.type === 'trigger' ? 'bolt' : source.type === 'condition' ? 'rule' : source.type === 'logic' ? 'account_tree' : source.type === 'event_group' ? 'call_split' : source.type === 'action_group' ? 'account_tree' : source.type === 'set_variable' ? 'edit_note' : source.type === 'wait' ? 'hourglass_empty' : 'play_arrow', availableStepIds: isCondition(source) ? conditionPredecessors(workflow, source.binding_id) : graphPredecessors(workflow, source.binding_id) }
    if (source.type === 'event_group' && source.group_type === 'if') {
      node.inputs = outputs.filter(port => !port.branch.else).map(port => ({ id: port.id, label: `${port.label} 条件`, y: port.y, x: open ? geometry.width - 72 : 0 }))
      for (const port of outputs) inspectionNodes.push({ id: `branch-${source.binding_id}-${port.id}`, raw: source, source: port.branch, branch: port.branch, gate: true, groupOwner: source, open, kind: 'branch', icon: 'call_split', label: port.label, kicker: '分支设置', x: geometry.x + geometry.width - 72, y: geometry.y + port.y, width: 72, height: 28 })
    }

    if (kind === 'predicate') node.dataInputs = ['left', 'right'].filter(key => Object.hasOwn(source.predicate, key)).map((name, i) => ({ id: name, name, index: i, label: name === 'left' ? '判断数据' : '比较目标', type: 'any' }))
    nodes.push(node)
    if (open && source.type === 'action_group') {
      nodes.push({ id: `entry-${source.binding_id}`, raw: source, source, workflow, kind: 'branch', gate: true, entryGate: true, x: geometry.x + 28, y: geometry.y + 78, width: 180, fixedHeight: 104, hasInput: false, hasOutput: true, outputs: [{ id: 'entry', label: '', y: 52 }], label: '动作组入口', kicker: '入口', meta: '连接组内起始动作', icon: 'play_arrow' })
    }
    if (open && source.type === 'event_group' && source.group_type !== 'if') {
      const branches = source.group_type === 'if' ? visibleBranches(workflow, source, true) : [{ id: 'input', label: groupTypeLabel(source.group_type) }]
      branches.forEach((branch, i) => nodes.push({ id: `gate-${source.binding_id}-${branch.id}`, raw: source, source: branch, workflow, kind: 'branch', branch, groupOwner: source, gate: true, x: geometry.x + geometry.width - 218, y: geometry.y + (source.group_type === 'if' ? 78 + i * 136 : Math.max(78, (geometry.height - 104) / 2)), width: 180, fixedHeight: 104, hasInput: !branch.else, hasOutput: true, inputY: 52, outputs: [{ id: source.group_type === 'if' ? branch.id : 'result', label: '', y: 52 }], label: branch.label || (branch.else ? 'ELSE' : source.group_type === 'if' ? source.branches.indexOf(branch) === 0 ? 'IF' : `ELIF ${source.branches.indexOf(branch)}` : groupTypeLabel(source.group_type)), kicker: source.group_type === 'if' ? '分支' : '逻辑', meta: branch.else ? '全部条件不成立时执行' : source.group_type === 'all' ? '全部条件成立' : source.group_type === 'any' ? '任一条件成立' : source.group_type === 'not' ? '对输入条件取反' : '从触发器或条件节点连接', icon: 'call_split', targetPort: branch.id }))
      if (source.group_type !== 'if') node.outputs[0].y = Math.max(130, geometry.height / 2)
      for (const gate of nodes.filter(candidate => candidate.gate && candidate.raw === source)) edges.push({ id: `gate-out:${gate.id}`, from: gate.id, to: node.id, channel: 'control', kind: 'pipeline', sourcePort: gate.outputs[0].id, targetPort: gate.outputs[0].id, label: '' })
    }
  }
  const proxy = identity => { const source = byId.get(identity); return source ? hiddenBy(source)?.binding_id || identity : identity }
  for (const edge of workflow.edges) {
    const source = proxy(edge.source), target = proxy(edge.target)
    if (source === target) continue
    const destination = byId.get(edge.target)
    const targetId = edge.channel === 'condition' && expanded.has(target) && destination?.type === 'event_group' && destination.group_type !== 'if' ? `gate-${target}-${edge.target_port || 'input'}` : `action-${target}`
    edges.push({ id: `wire:${edge.source}:${edge.port}:${edge.target}:${edge.target_port || ''}`, from: edge.port === 'entry' && expanded.has(source) ? `entry-${source}` : `action-${source}`, to: targetId, channel: 'control', kind: edge.port === 'failure' ? 'failure' : edge.channel === 'condition' ? 'condition' : 'pipeline', sourcePort: edge.port, inputPort: targetId.startsWith('gate-') ? undefined : edge.target_port, rawEdge: edge, workflow, label: edge.port === 'failure' ? '失败补救' : '' })
  }
  return { nodes, edges, inspectionNodes }
}
