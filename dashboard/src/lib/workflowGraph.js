const clone = value => JSON.parse(JSON.stringify(value))
export const graphId = (prefix = 'a') => `${prefix}_${globalThis.crypto?.randomUUID?.().replaceAll('-', '').slice(0, 12) || Math.random().toString(16).slice(2).padEnd(12, '0')}`
export function predicateToGraph(predicate = { op: 'event_occurred', trigger_id: '', expected: true }) {
  const graph = { root: null, nodes: [], edges: [] }
  function visit(value) {
    const id = graphId('p')
    if (['all', 'any', 'not'].includes(value.op)) {
      graph.nodes.push({ id, op: value.op })
      ;(value.children || []).forEach((child, order) => graph.edges.push({ source: visit(child), target: id, order }))
    } else graph.nodes.push({ id, predicate: clone(value) })
    return id
  }
  graph.root = visit(predicate)
  return graph
}
export function graphPredicate(graph, id = graph?.root, seen = new Set()) {
  if (!graph || seen.has(id)) return { op: 'is_true', left: false }
  const node = graph.nodes.find(node => node.id === id)
  if (!node) return { op: 'is_true', left: false }
  if (node.predicate) return node.predicate
  return { op: node.op, children: graph.edges.filter(edge => edge.target === id).sort((a, b) => a.order - b.order).map(edge => graphPredicate(graph, edge.source, new Set([...seen, id]))) }
}
export function walkGraphActions(workflow, visit, trail = []) {
  for (const [index, action] of (workflow?.nodes || []).entries()) {
    visit(action, index, trail, workflow)
    if (action.workflow) walkGraphActions(action.workflow, visit, [...trail, { action, index, field: 'workflow' }])
    for (const [failureIndex, failure] of (action.failure_actions || []).entries()) visit(failure, failureIndex, [...trail, { action, index, field: 'failure_actions' }], null)
  }
}
export function findGraphNode(rule, bindingId) {
  let result
  walkGraphActions(rule.workflow, (action, index, trail, workflow) => { if (action.binding_id === bindingId) result = { action, index, trail, workflow } })
  return result
}
export function graphExecutionActions(rule, { startStepId = '', endStepId = '' } = {}) {
  const hit = startStepId ? findGraphNode(rule, startStepId) : { action: { binding_id: rule.workflow?.entry }, workflow: rule.workflow }
  if (!hit?.workflow) return []
  const result = [], seen = new Set()
  function visit(workflow, id) {
    if (!id || seen.has(id)) return
    const action = workflow.nodes.find(node => node.binding_id === id)
    if (!action) return
    seen.add(id); result.push(action)
    if (action.workflow) visit(action.workflow, action.workflow.entry)
    for (const failure of action.failure_actions || []) { if (!seen.has(failure.binding_id)) { seen.add(failure.binding_id); result.push(failure) } }
    if (id !== endStepId) workflow.edges.filter(edge => edge.source === id).forEach(edge => visit(workflow, edge.target))
  }
  visit(hit.workflow, hit.action.binding_id)
  return result
}
export function getGraphTriggers(rule) {
  const leaves = new Map()
  function condition(node) {
    if (!node) return
    if (node.type) leaves.set(node.binding_id, node)
    else (node.children || []).forEach(condition)
  }
  condition(rule.condition)
  walkGraphActions(rule.workflow, action => { condition(action.activation); (action.triggers || []).forEach(condition) })
  return [...leaves.values()]
}
export function normalizeWorkflow(rule) {
  if (rule.workflow) return rule.workflow
  function sequence(actions) {
    const workflow = { entry: null, nodes: [], edges: [] }
    function add(items, next) {
      let target = next
      for (const item of [...items].reverse()) {
        if (item.type === 'if') {
          const branches = [{ condition: item.condition, items: item.then || [] }, ...(item.elif || []).map(branch => ({ condition: branch.condition, items: branch.then || [] })), { else: true, items: item.else || [] }]
          const group = { binding_id: item.binding_id, type: 'event_group', mode: 'immediate', triggers: [], branches: [] }
          for (const branch of branches) {
            const id = graphId('b')
            group.branches.push(branch.else ? { id, else: true } : { id, condition_graph: predicateToGraph(branch.condition) })
            workflow.edges.push({ source: group.binding_id, port: id, target: add(branch.items, target) })
          }
          workflow.nodes.push(group)
        } else {
          workflow.nodes.push(item)
          workflow.edges.push({ source: item.binding_id, port: 'next', target })
        }
        target = item.binding_id
      }
      return target
    }
    workflow.entry = add(actions, null)
    workflow.nodes.reverse()
    return workflow
  }
  const workflow = sequence(rule.actions || [])
  if (rule.condition) {
    const branch = { id: graphId('b'), else: true, label: '执行' }
    const group = { binding_id: graphId(), type: 'event_group', mode: 'immediate', activation: rule.condition, triggers: [], branches: [branch] }
    workflow.nodes.unshift(group)
    workflow.edges.push({ source: group.binding_id, port: branch.id, target: workflow.entry })
    workflow.entry = group.binding_id
    delete rule.condition
  }
  rule.workflow = workflow
  delete rule.actions
  return workflow
}
export function createGraphAction(type, params = {}) {
  const base = { type, binding_id: graphId() }
  if (type === 'event_group') return { ...base, mode: 'immediate', triggers: [], branches: [{ id: graphId('b'), condition_graph: predicateToGraph() }, { id: graphId('b'), else: true }] }
  if (type === 'action_group') return { ...base, workflow: { entry: null, nodes: [], edges: [] }, exports: {} }
  if (type === 'set_variable') return { ...base, variable: '', value: '' }
  return { ...base, params }
}
export function createTriggerStep(trigger, mode = 'wait') {
  return { binding_id: graphId(), type: 'event_group', mode, activation: trigger, triggers: [], branches: [{ id: graphId('b'), else: true, label: '执行' }] }
}
export function standaloneTrigger(node, startup = false) {
  return node.type === 'event_group' && (startup || node.mode === 'wait') && !node.name && node.activation?.type && !node.triggers?.length && node.branches?.length === 1 && node.branches[0].else && !node.branches[0].condition_graph && !node.branches[0].condition ? node.activation : null
}
export function insertGraphNode(workflow, node, source = null, port = 'next') {
  workflow.nodes.push(node)
  const edge = workflow.edges.find(edge => edge.source === source && edge.port === port)
  const target = edge?.target ?? (source === null ? workflow.entry : null)
  if (edge) edge.target = node.binding_id
  else if (source) workflow.edges.push({ source, port, target: node.binding_id })
  else workflow.entry = node.binding_id
  for (const outlet of node.type === 'event_group' ? node.branches.map(branch => branch.id) : ['next']) workflow.edges.push({ source: node.binding_id, port: outlet, target })
  return node
}
export function removeGraphNode(workflow, node) {
  const outgoing = workflow.edges.filter(edge => edge.source === node.binding_id)
  const targets = [...new Set(outgoing.map(edge => edge.target))]
  const next = targets.length === 1 ? targets[0] : null
  workflow.edges = workflow.edges.filter(edge => edge.source !== node.binding_id)
  for (const edge of workflow.edges) if (edge.target === node.binding_id) edge.target = next
  if (workflow.entry === node.binding_id) workflow.entry = next
  workflow.nodes.splice(workflow.nodes.indexOf(node), 1)
}
export function connectGraph(workflow, source, port, target) {
  if (source === target || !workflow.nodes.some(node => node.binding_id === target)) return false
  const reachable = new Set()
  function visit(id) { if (!id || reachable.has(id)) return; reachable.add(id); workflow.edges.filter(edge => edge.source === id).forEach(edge => visit(edge.target)) }
  visit(target)
  if (reachable.has(source)) return false
  const edge = workflow.edges.find(edge => edge.source === source && edge.port === port)
  if (edge) edge.target = target
  else workflow.edges.push({ source, port, target })
  return true
}
export function connectConditionGraph(graph, source, target) {
  const from = graph.nodes.find(node => node.id === source), to = graph.nodes.find(node => node.id === target)
  if (!from || !to?.op) return '条件只能连接到同一分支的条件组'
  if (source === target) return '条件不能连接到自身'
  if (graph.edges.some(edge => edge.source === source && edge.target === target)) return ''
  const reachable = new Set()
  function visit(id) { if (reachable.has(id)) return; reachable.add(id); graph.edges.filter(edge => edge.source === id).forEach(edge => visit(edge.target)) }
  visit(target)
  if (reachable.has(source)) return '条件连接不能形成循环'
  if (to.op === 'not' && graph.edges.some(edge => edge.target === target)) return 'NOT 只能包含一个条件'
  const previous = graph.edges.filter(edge => edge.source === source)
  if (previous.some(edge => graph.edges.filter(item => item.target === edge.target).length === 1)) return '移动后原条件组为空，请先为原组添加其他条件'
  const edges = graph.edges.filter(edge => edge.source !== source)
  edges.push({ source, target, order: edges.filter(edge => edge.target === target).length })
  const roots = graph.nodes.filter(node => !edges.some(edge => edge.source === node.id))
  if (roots.length !== 1) return '连接后必须有一个完整的条件结果'
  for (const node of graph.nodes) edges.filter(edge => edge.target === node.id).sort((a, b) => a.order - b.order).forEach((edge, order) => { edge.order = order })
  graph.edges = edges; graph.root = roots[0].id
  return ''
}
export function connectTriggerCondition(graph, triggerId, target) {
  const to = graph.nodes.find(node => node.id === target)
  if (!['all', 'any', 'not'].includes(to?.op)) return '事件只能连接到 AND、OR 或 NOT 条件组'
  const existing = graph.nodes.find(node => node.predicate?.op === 'event_occurred' && node.predicate.trigger_id === triggerId && node.predicate.expected !== false && graph.edges.some(edge => edge.source === node.id && edge.target === target))
  if (existing) return ''
  const pending = graph.nodes.find(node => node.predicate?.op === 'event_occurred' && !node.predicate.trigger_id && graph.edges.some(edge => edge.source === node.id && edge.target === target) && graph.edges.filter(edge => edge.source === node.id).length === 1)
  if (pending) { pending.predicate.trigger_id = triggerId; pending.predicate.expected = true; return '' }
  if (to.op === 'not' && graph.edges.some(edge => edge.target === target)) return 'NOT 只能包含一个条件'
  const id = graphId('p')
  graph.nodes.push({ id, predicate: { op: 'event_occurred', trigger_id: triggerId, expected: true } })
  graph.edges.push({ source: id, target, order: 1 + Math.max(-1, ...graph.edges.filter(edge => edge.target === target).map(edge => edge.order ?? 0)) })
  return ''
}
export function graphPredecessors(workflow, id) {
  const result = new Set()
  function visit(target) { for (const edge of workflow?.edges || []) if (edge.target === target && !result.has(edge.source)) { result.add(edge.source); visit(edge.source) } }
  visit(id)
  return [...result]
}
export function copyGraphNode(node) {
  const copy = clone(node), replacements = new Map()
  function visit(value) {
    if (!value || typeof value !== 'object') return
    for (const key of ['binding_id', 'id']) if (typeof value[key] === 'string') { const old = value[key]; const id = graphId(old.split('_')[0]); replacements.set(old, id); value[key] = id }
    Object.values(value).forEach(value => { if (Array.isArray(value)) value.forEach(visit); else visit(value) })
  }
  visit(copy)
  function rewrite(value) {
    if (!value || typeof value !== 'object') return
    for (const key of ['source', 'target', 'entry', 'root', 'port', 'node', 'trigger_id']) if (replacements.has(value[key])) value[key] = replacements.get(value[key])
    Object.values(value).forEach(value => { if (Array.isArray(value)) value.forEach(rewrite); else rewrite(value) })
  }
  rewrite(copy)
  return copy
}
export function buildWorkflowGraph(rule, expanded, schema, presentation, layoutCache = new Map()) {
  const nodes = [], edges = [], inspectionNodes = []
  const width = 228, gap = 100, row = 164
  const cardHeight = node => {
    const ports = node.outputs || node.outputAnchors || []
    const outletRows = node.source?.type === 'event_group' || node.outputAnchors?.some(port => port.label)
    const bodyHeight = outletRows ? 90 + ports.length * 28 : 116
    return Math.max(bodyHeight, presentation.nodeHeight?.(node) || 0, ...ports.map(port => port.y + 15))
  }
  const { eventName, actionName, describeItem, predicateSummary } = presentation
  const branchLabel = (source, branch) => branch.label || (branch.else ? 'ELSE' : source.branches.indexOf(branch) ? `ELIF ${source.branches.indexOf(branch)}` : 'IF')
  function eventContent(source) {
    if (layoutCache.has(source)) return layoutCache.get(source)
    const innerNodes = [], innerEdges = [], inspectors = []
    const triggerList = getGraphTriggers({ workflow: { nodes: [source] } })
    let entry = rule.workflow.nodes.find(node => node.binding_id === rule.workflow.entry)
    while (entry?.type === 'action_group') entry = entry.workflow.nodes.find(node => node.binding_id === entry.workflow.entry)
    const activationLabel = source.mode === 'wait' ? '等待事件' : entry === source ? '启动事件' : '事件来源'
    const eventSources = new Map(triggerList.map(trigger => [trigger.binding_id, `trigger-${trigger.binding_id}`]))
    const referenced = new Set(source.branches.flatMap(branch => (branch.condition_graph?.nodes || []).map(node => node.predicate?.trigger_id).filter(Boolean)))
    const external = getGraphTriggers(rule).filter(trigger => referenced.has(trigger.binding_id) && !eventSources.has(trigger.binding_id))
    for (const trigger of external) eventSources.set(trigger.binding_id, `event-source-${source.binding_id}-${trigger.binding_id}`)
    for (const trigger of [...triggerList, ...external]) innerNodes.push({ id: eventSources.get(trigger.binding_id), source: trigger, kind: 'trigger', label: eventName(trigger), kicker: '事件来源', meta: describeItem(trigger, 'trigger'), icon: 'bolt', hasInput: false, hasOutput: true, outputs: [{ id: 'event', label: '事件', y: 58 }], readOnly: external.includes(trigger), ...(external.includes(trigger) ? { dataInputs: [], dataOutputs: [] } : {}) })
    function activationNode(value, path = []) {
      if (value.type) {
        const id = `trigger-${value.binding_id}`
        Object.assign(innerNodes.find(node => node.id === id), { activationValue: value, activationOwner: source, kicker: activationLabel, hasOutput: true, outputs: [{ id: 'activation', label: '事件', y: 58 }] })
        return id
      }
      const id = `activation-${source.binding_id}-${path.join('-') || 'root'}`
      const children = (value.children || []).map((child, index) => activationNode(child, [...path, index]))
      innerNodes.push({ id, source: value, activationNode: true, activationValue: value, activationOwner: source, kind: 'activation', label: value.op === 'all' ? '全部事件 · AND' : value.op === 'any' ? '任一事件 · OR' : '事件未发生 · NOT', kicker: '触发组合', meta: value.op === 'all' ? `时间窗口 ${value.within_seconds || 0} 秒` : value.op === 'not' ? `等待 ${value.within_seconds || 0} 秒` : value.pick === 'latest' ? '使用最新事件' : '使用首个事件', icon: 'account_tree', hasInput: true, hasOutput: true, outputs: [{ id: 'activation', label: '事件组合', y: 58 }] })
      for (const child of children) innerEdges.push({ id: `activation:${child}:${id}`, from: child, to: id, activationOwner: source, activationTarget: value, kind: 'activation', channel: 'control', label: value.op === 'all' ? '并且' : value.op === 'any' ? '或者' : '未发生' })
      return id
    }
    const activationRoot = source.activation ? activationNode(source.activation) : null
    const branchRoots = new Map()
    for (const branch of source.branches) {
      if (!branch.condition_graph) continue
      const conditionGraph = branch.condition_graph
      const conditionId = condition => `predicate-${source.binding_id}-${condition.id}`
      const eventSource = condition => condition?.predicate?.op === 'event_occurred' && eventSources.get(condition.predicate.trigger_id)
      const displayId = id => { const condition = conditionGraph.nodes.find(node => node.id === id); return eventSource(condition) || (condition && conditionId(condition)) }
      for (const condition of conditionGraph.nodes) {
        const descriptor = { id: conditionId(condition), source: condition.predicate || condition, graphCondition: condition, conditionGraph, branch, kind: condition.predicate ? 'predicate' : 'condition-expression', label: condition.predicate ? '条件判断' : condition.op === 'all' ? '全部满足 · AND' : condition.op === 'any' ? '满足任一 · OR' : '取反 · NOT', kicker: condition.predicate ? condition.predicate.op === 'event_occurred' ? '事件判断' : condition.predicate.op === 'run_status' ? '自动化状态' : '数据判断' : '条件组', meta: condition.predicate ? predicateSummary(condition.predicate, source) : '组合子条件', icon: condition.predicate ? 'rule' : 'account_tree', hasInput: true, hasOutput: true, dataInputs: condition.predicate ? ['left', 'right'].filter(name => Object.hasOwn(condition.predicate, name)).map((name, index) => ({ id: name, index, name, label: name === 'left' ? '判断数据' : '比较目标', type: 'any' })) : [], dataOutputs: [] }
        if (eventSource(condition)) inspectors.push(descriptor)
        else innerNodes.push(descriptor)
      }
      branchRoots.set(branch.id, displayId(conditionGraph.root))
      for (const edge of [...conditionGraph.edges].sort((a, b) => a.order - b.order)) {
        const condition = conditionGraph.nodes.find(node => node.id === edge.source)
        innerEdges.push({ id: `condition:${source.binding_id}:${branch.id}:${edge.source}:${edge.target}`, from: displayId(edge.source), to: displayId(edge.target), channel: 'control', kind: 'condition', conditionGraph, rawEdge: edge, inspectionId: eventSource(condition) ? conditionId(condition) : null, label: eventSource(condition) ? condition.predicate.expected === false ? '事件未发生' : '事件已发生' : '' })
      }
    }
    if (activationRoot && !source.branches.some(branch => branch.condition_graph)) branchRoots.set(source.branches[0]?.id, activationRoot)
    else if (activationRoot) {
      const root = innerNodes.find(node => node.id === activationRoot)
      root.meta = `${root.meta} · ${source.mode === 'wait' ? '等待条件成立后判断分支' : entry === source ? '启动后判断分支' : '进入本组时立即判断分支'}`
    }
    innerNodes.forEach(node => { node.fixedHeight = cardHeight(node) })
    const byId = new Map(innerNodes.map(node => [node.id, node])), incoming = new Map()
    for (const edge of innerEdges) if (byId.has(edge.from)) {
      if (!incoming.has(edge.to)) incoming.set(edge.to, [])
      incoming.get(edge.to).push(edge.from)
    }
    const ranks = new Map(), visiting = new Set()
    function rank(id) {
      if (ranks.has(id)) return ranks.get(id)
      if (visiting.has(id)) return 0
      visiting.add(id)
      const children = incoming.get(id) || []
      const value = children.length ? 1 + Math.max(...children.map(rank)) : 0
      visiting.delete(id); ranks.set(id, value)
      return value
    }
    innerNodes.forEach(node => rank(node.id))
    let leaf = 0
    const positions = new Map()
    function position(id) {
      if (positions.has(id)) return positions.get(id)
      const children = incoming.get(id) || []
      positions.set(id, 80)
      const value = children.length ? children.reduce((sum, child) => sum + position(child), 0) / children.length : 80 + leaf++ * 164
      positions.set(id, value)
      return value
    }
    innerNodes.forEach(node => position(node.id))
    for (const layer of [...new Set(ranks.values())].sort((a, b) => a - b)) {
      let bottom = 80
      for (const node of innerNodes.filter(node => ranks.get(node.id) === layer).sort((a, b) => positions.get(a.id) - positions.get(b.id))) {
        const children = incoming.get(node.id) || []
        const desired = children.length ? children.reduce((sum, child) => sum + positions.get(child), 0) / children.length : positions.get(node.id)
        node.x = 32 + layer * (width + 100); node.y = Math.max(bottom, desired)
        positions.set(node.id, node.y); bottom = node.y + (node.fixedHeight || 116) + 32
      }
    }
    let outletY = 80
    const outputs = source.branches.map(branch => {
      const root = byId.get(branchRoots.get(branch.id))
      const y = Math.max(outletY, root ? root.y + 58 : outletY)
      outletY = y + 48
      return { id: branch.id, label: branchLabel(source, branch), y }
    })
    const eventOutlet = source.branches.some(branch => branch.condition_graph?.nodes.find(node => node.id === branch.condition_graph.root)?.predicate?.op === 'event_occurred')
    const w = Math.max(360, ...innerNodes.map(node => node.x + width + (eventOutlet ? 160 : 104)))
    const h = Math.max(144, outletY - 20, ...innerNodes.map(node => node.y + (node.fixedHeight || 116))) + 28
    for (const branch of source.branches) {
      const from = branchRoots.get(branch.id)
      if (!from) continue
      const condition = branch.condition_graph?.nodes.find(node => node.id === branch.condition_graph.root)
      const event = condition?.predicate?.op === 'event_occurred'
      innerEdges.push({ id: `branch:${source.binding_id}:${branch.id}`, from, to: `action-${source.binding_id}`, targetPort: branch.id, kind: branch.condition_graph ? 'condition' : 'activation', channel: 'control', conditionGraph: branch.condition_graph, inspectionId: event ? `predicate-${source.binding_id}-${condition.id}` : null, ...(branch.condition_graph ? {} : { activationOwner: source, activationTarget: source.activation }), label: event ? condition.predicate.expected === false ? '事件未发生' : '事件已发生' : '' })
    }
    const content = { nodes: innerNodes, inspectionNodes: inspectors, edges: innerEdges, w, h, inputY: outputs[0]?.y ?? 80, outputs }
    layoutCache.set(source, content)
    return content
  }
  function graphLayout(workflow, x, y, ancestry = []) {
    const triggerFor = node => standaloneTrigger(node, workflow.entry === node.binding_id && ancestry.every(parent => parent.workflow.entry === parent.source.binding_id))
    const byId = new Map(workflow.nodes.map(node => [node.binding_id, node]))
    const ranks = new Map(), lanes = new Map(); let lane = 0
    function rank(id, depth, branchLane) {
      if (!id || depth > workflow.nodes.length || !byId.has(id)) return
      const previous = ranks.get(id)
      if (previous !== undefined && previous >= depth) return
      ranks.set(id, depth); lanes.set(id, lanes.get(id) ?? branchLane)
      const branches = byId.get(id).branches
      workflow.edges.filter(edge => edge.source === id && edge.target).sort((a, b) => branches ? branches.findIndex(branch => branch.id === a.port) - branches.findIndex(branch => branch.id === b.port) : 0).forEach((edge, index) => rank(edge.target, depth + 1, index ? ++lane : branchLane))
    }
    rank(workflow.entry, 0, 0)
    for (const node of workflow.nodes) if (!ranks.has(node.binding_id)) { ranks.set(node.binding_id, 0); lanes.set(node.binding_id, ++lane) }
    const columnWidths = new Map(), sizes = new Map()
    for (const source of workflow.nodes) {
      const trigger = triggerFor(source), open = !trigger && expanded.has(source.binding_id)
      let w = width, h = cardHeight({ id: `action-${source.binding_id}`, source: trigger || source, kind: trigger ? 'trigger' : source.type, outputs: trigger ? [{ y: 58 }] : source.type === 'event_group' ? source.branches.map((branch, i) => ({ y: 103 + i * 28 })) : [] })
      if (open && source.type === 'action_group') {
        const preview = layoutCache.get(source.workflow) || buildWorkflowGraph({ workflow: source.workflow }, expanded, schema, presentation, layoutCache)
        layoutCache.set(source.workflow, preview)
        const byId = new Map(preview.nodes.map(node => [node.id, node]))
        const exits = source.workflow.edges.filter(edge => edge.target === null).map(edge => ({ node: byId.get(`action-${edge.source}`), port: edge.port }))
        for (const child of source.workflow.nodes) if (!source.workflow.edges.some(edge => edge.source === child.binding_id)) exits.push({ node: byId.get(`action-${child.binding_id}`), port: 'next' })
        const completionX = preview.nodes.reduce((max, node) => Math.max(max, node.x + (node.width || width)), 52) - 52 + 128
        const completionY = exits.length ? 80 + exits.reduce((sum, exit) => sum + exit.node.y - 118 + (exit.node.outputs?.find(port => port.id === exit.port)?.y ?? 58), 0) / exits.length - 58 : 80
        w = Math.max(480, completionX + width + 28)
        h = Math.max(172, preview.nodes.reduce((max, node) => Math.max(max, node.y - 118 + (node.fixedHeight || 116)), 0) + 108, completionY + cardHeight({ id: `completion-${source.binding_id}` }) + 28)
        const entry = preview.nodes.find(node => node.id === `action-${source.workflow.entry}`) || preview.nodes[0]
        sizes.set(source.binding_id, { completionX, completionY, inputY: 80 + (entry?.y || 118) - 118 + (entry?.inputY ?? 58), outputY: completionY + 58 })
      } else if (open && source.type === 'event_group') {
        const content = eventContent(source)
        w = content.w; h = content.h
        sizes.set(source.binding_id, { content, inputY: content.inputY })
      }
      sizes.set(source.binding_id, { ...sizes.get(source.binding_id), w, h, alignmentY: source.type === 'event_group' && !trigger ? sizes.get(source.binding_id)?.content?.outputs[0]?.y ?? 103 : sizes.get(source.binding_id)?.inputY ?? 58 }); columnWidths.set(ranks.get(source.binding_id), Math.max(columnWidths.get(ranks.get(source.binding_id)) || 0, w))
    }
    const columnX = depth => x + [...columnWidths].filter(([rank]) => rank < depth).sort(([a], [b]) => a - b).reduce((sum, [, w]) => sum + w + gap, 0)
    const laneHeights = new Map(), laneAnchors = new Map(), laneBelow = new Map()
    for (const source of workflow.nodes) {
      const lane = lanes.get(source.binding_id), size = sizes.get(source.binding_id), anchor = size.alignmentY
      laneAnchors.set(lane, Math.max(laneAnchors.get(lane) || 0, anchor))
      laneBelow.set(lane, Math.max(laneBelow.get(lane) || 0, size.h - anchor))
    }
    for (const [lane, anchor] of laneAnchors) laneHeights.set(lane, Math.max(row, anchor + laneBelow.get(lane) + 48))
    const laneY = lane => y + [...laneHeights].filter(([value]) => value < lane).reduce((sum, [, h]) => sum + h, 0) + laneAnchors.get(lane)
    for (const [index, source] of workflow.nodes.entries()) {
      const trigger = triggerFor(source)
      const { w, h } = sizes.get(source.binding_id), open = !trigger && expanded.has(source.binding_id)
      const group = ['event_group', 'action_group'].includes(source.type)
      const node = { id: `action-${source.binding_id}`, source, workflow, ancestry, index, kind: group ? source.type : 'action', width: w, fixedHeight: h, x: columnX(ranks.get(source.binding_id)), y: laneY(lanes.get(source.binding_id)) - sizes.get(source.binding_id).alignmentY, inputY: sizes.get(source.binding_id).inputY ?? 58, label: source.name || (source.type === 'event_group' ? '事件组' : source.type === 'action_group' ? '动作组' : actionName(source)), kicker: workflow.entry === source.binding_id && !ancestry.length ? '流程入口' : group ? '流程组' : `动作 ${index + 1}`, meta: group ? source.type === 'event_group' ? '按 IF、ELIF 顺序选择一个出口' : `${source.workflow.nodes.length} 个内部节点` : describeItem(source, 'action'), icon: source.type === 'event_group' ? 'call_split' : source.type === 'action_group' ? 'account_tree' : source.type === 'set_variable' ? 'edit_note' : source.type === 'wait' ? 'hourglass_empty' : 'play_arrow', hasInput: !(workflow.entry === source.binding_id && !ancestry.length), hasOutput: true, open, frame: group && open, availableStepIds: [...ancestry.flatMap(parent => graphPredecessors(parent.workflow, parent.source.binding_id)), ...graphPredecessors(workflow, source.binding_id)], outputs: source.type === 'event_group' ? source.branches.map((branch, i) => ({ id: branch.id, label: branch.label || (branch.else ? 'ELSE' : i ? `ELIF ${i}` : 'IF'), y: 103 + i * 28 })) : [{ id: 'next', label: '完成', y: sizes.get(source.binding_id).inputY ?? 58 }] }
      if (sizes.get(source.binding_id).content) node.outputs = sizes.get(source.binding_id).content.outputs
      if (sizes.get(source.binding_id).outputY !== undefined) node.outputs[0].y = sizes.get(source.binding_id).outputY
      if (trigger) Object.assign(node, { source: trigger, groupOwner: source, standaloneTrigger: true, kind: 'trigger', label: eventName(trigger), kicker: source.mode === 'wait' ? '等待触发器' : '触发器', meta: describeItem(trigger, 'trigger'), icon: 'bolt', outputs: [{ id: source.branches[0].id, label: '执行', y: 58 }], open: false, frame: false })
      nodes.push(node)
      if (open && source.type === 'action_group') {
        graphLayout(source.workflow, node.x + 28, node.y + 80, [...ancestry, node])
        const id = `completion-${source.binding_id}`, size = sizes.get(source.binding_id)
        nodes.push({ id, source, groupOwner: source, workflow, ancestry, activationNode: true, kind: 'activation', label: '返回父流程', kicker: '动作组完成', meta: '从完成出口继续后续动作', icon: 'done_all', x: node.x + size.completionX, y: node.y + size.completionY, width, fixedHeight: cardHeight({ id }), outputAnchors: [{ id: 'next', y: 58 }], hasInput: false, hasOutput: false, dataInputs: [], dataOutputs: [] })
        const entryId = source.workflow.entry ? `action-${source.workflow.entry}` : `empty-${source.binding_id}`
        edges.push({ id: `group-entry:${source.binding_id}`, from: node.id, to: entryId, sourceSide: 'left', sourceInput: true, kind: 'pipeline', channel: 'control', label: '' })
        if (!source.workflow.entry) edges.push({ id: `group-empty:${source.binding_id}`, from: entryId, to: id, kind: 'pipeline', channel: 'control', label: '' })
        for (const edge of source.workflow.edges.filter(edge => edge.target === null)) edges.push({ id: `group-return:${source.binding_id}:${edge.source}:${edge.port}`, from: `action-${edge.source}`, to: id, sourcePort: edge.port, kind: 'pipeline', channel: 'control', label: '' })
        for (const child of source.workflow.nodes) if (!source.workflow.edges.some(edge => edge.source === child.binding_id)) edges.push({ id: `group-return:${source.binding_id}:${child.binding_id}`, from: `action-${child.binding_id}`, to: id, kind: 'pipeline', channel: 'control', label: '' })
        edges.push({ id: `group-completed:${source.binding_id}`, from: id, to: node.id, sourcePort: 'next', targetPort: 'next', kind: 'pipeline', channel: 'control', label: '' })
      }
      if (open && source.type === 'event_group') {
        const content = sizes.get(source.binding_id).content
        nodes.push(...content.nodes.map(inner => ({ ...inner, groupOwner: source, workflow, ancestry, width, fixedHeight: inner.fixedHeight, x: node.x + inner.x, y: node.y + inner.y, availableStepIds: node.availableStepIds })))
        inspectionNodes.push(...content.inspectionNodes.map(inner => ({ ...inner, groupOwner: source, workflow, ancestry, availableStepIds: node.availableStepIds })))
        edges.push(...content.edges)
      }
    }
    let failureY = y + [...laneHeights.values()].reduce((sum, height) => sum + height, 0)
    for (const owner of workflow.nodes) {
      const sourceNode = nodes.find(node => node.source === owner || node.standaloneTrigger && node.groupOwner === owner)
      let previous = sourceNode?.id
      for (const [index, failure] of (owner.failure_actions || []).entries()) {
        const id = `failure-action-${failure.binding_id}`
        nodes.push({ id, source: failure, owner, index, workflow, ancestry, kind: 'failure-action', width, x: sourceNode.x + (index + 1) * (width + gap), y: failureY, icon: 'build', label: actionName(failure), kicker: `补救 ${index + 1}`, meta: describeItem(failure, 'action'), hasInput: true, hasOutput: true, availableStepIds: [...sourceNode.availableStepIds, ...owner.failure_actions.slice(0, index).map(node => node.binding_id)] })
        edges.push({ id: `failure:${previous}:${id}`, from: previous, to: id, kind: 'failure', channel: 'control', label: index ? '然后' : '失败时' }); previous = id
      }
      if (owner.failure_actions?.length) failureY += row
    }
    for (const edge of workflow.edges) if (edge.target) edges.push({ id: `control:${edge.source}:${edge.port}`, from: `action-${edge.source}`, to: `action-${edge.target}`, kind: 'pipeline', channel: 'control', sourcePort: edge.port, rawEdge: edge, workflow, label: '' })
    if (!workflow.entry) nodes.push({ id: `empty-${ancestry.at(-1)?.source.binding_id || 'root'}`, workflow, kind: 'empty', ancestry, x, y, width, fixedHeight: 120, icon: 'start', kicker: '空流程', label: '选择起始操作', meta: '点击选择触发器、动作或事件组', hasInput: false, hasOutput: true })
  }
  graphLayout(rule.workflow, 52, 118)
  return { nodes, edges, inspectionNodes }
}
