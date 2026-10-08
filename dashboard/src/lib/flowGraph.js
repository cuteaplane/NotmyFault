import { ifBranches, predicateSummary, precedingSources, workflowEventChoices } from './workflowConditions.js'
export const FLOW_NODE_WIDTH = 228
export const FLOW_NODE_PORT_Y = 58
export const FLOW_NODE_BASE_HEIGHT = 116
export const FLOW_NODE_ADMIN_EXTRA_HEIGHT = 14
export const FLOW_DATA_SUMMARY_HEIGHT = 30
// 数据端口的纵坐标由节点边框、标题、正文和分隔线高度相加得到。
export const FLOW_DATA_PORT_Y = 128
export const FLOW_DATA_PORT_STEP = 24
export const FLOW_COLUMN_STEP = 360
export const FLOW_ROW_STEP = 220

const LEFT = 52
const TOP = 118


function describeWait(item) {
  const seconds = Number(item?.params?.seconds ?? 0) || 0
  const milliseconds = Number(item?.params?.milliseconds ?? 0) || 0
  if (seconds > 0 && milliseconds > 0) return `${seconds} 秒 + ${milliseconds} 毫秒`
  if (milliseconds > 0) return `${milliseconds} 毫秒`
  if (seconds > 0) return `${seconds} 秒`
  return '时长未设置'
}

function isLeaf(node) {
  return !!node
    && typeof node === 'object'
    && typeof node.type === 'string'
    && !Array.isArray(node.children)
    && !Array.isArray(node.events)
}

function conditionDepth(node) {
  if (isLeaf(node)) return 0
  const children = Array.isArray(node?.children) ? node.children : []
  return 1 + Math.max(0, ...children.map(conditionDepth))
}

export function conditionNodeId(node, path = []) {
  if (isLeaf(node) && node.binding_id) return `trigger-${node.binding_id}`
  return path.length ? `condition-${path.join('-')}` : 'condition-root'
}

export function buildFlowGraph({
  rule,
  condition,
  eventName,
  actionName,
  describeItem,
  isAdmin,
  schema,
  rules = [],
}) {
  const nodes = []
  const edges = []
  let conditionExit
  let leafIndex = 0

  if (condition) {
    const maxDepth = conditionDepth(condition)

    function visit(node, path, depth, negated = false) {
      const id = conditionNodeId(node, path)
      if (!node || typeof node !== 'object' || Array.isArray(node)) {
        const y = TOP + leafIndex * FLOW_ROW_STEP
        leafIndex += 1
        nodes.push({
          id,
          kind: 'invalid',
          path,
          source: node,
          x: LEFT + (maxDepth - depth) * FLOW_COLUMN_STEP,
          y,
          icon: 'error',
          kicker: '无效条件',
          label: '无法读取此节点',
          meta: '删除后重新添加',
          hasInput: false,
          hasOutput: true,
        })
        return { id, y }
      }
      if (isLeaf(node)) {
        const y = TOP + leafIndex * FLOW_ROW_STEP
        leafIndex += 1
        nodes.push({
          id,
          kind: 'trigger',
          negated,
          path,
          source: node,
          x: LEFT + (maxDepth - depth) * FLOW_COLUMN_STEP,
          y,
          icon: 'bolt',
          kicker: `触发 ${leafIndex}`,
          label: eventName(node),
          meta: describeItem(node, 'trigger'),
          admin: isAdmin(node, 'trigger'),
          hasInput: false,
          hasOutput: true,
        })
        return { id, y }
      }

      const children = Array.isArray(node?.children) ? node.children : []
      const childLayouts = children.map((child, index) => visit(child, [...path, index], depth + 1, node.op === 'not'))
      const y = childLayouts.length
        ? childLayouts.reduce((total, child) => total + child.y, 0) / childLayouts.length
        : TOP
      const op = node?.op || 'any'
      nodes.push({
        id,
        kind: 'condition',
        path,
        source: node,
        x: LEFT + (maxDepth - depth) * FLOW_COLUMN_STEP,
        y,
        icon: op === 'not' ? 'timer' : op === 'all' ? 'done_all' : 'alt_route',
        kicker: '逻辑汇合',
        label: op === 'not' ? '未发生 · NOT' : op === 'all' ? '全部满足 · AND' : '满足任一 · OR',
        meta: op === 'not' ? `等待 ${node.within_seconds || 0} 秒未发生` : `${children.length} 条分支${op === 'all' && node?.within_seconds ? ` · ${node.within_seconds} 秒内` : ''}`,
        hasInput: childLayouts.length > 0,
        hasOutput: true,
      })
      childLayouts.forEach((child, index) => edges.push({
        id: `${child.id}-${id}`,
        from: child.id,
        to: id,
        kind: 'condition',
        channel: 'control',
        label: children.length > 1 ? (op === 'all' ? '并且' : '或者') : '',
        branch: index + 1,
      }))
      return { id, y }
    }

    conditionExit = visit(condition, [], 0)
  } else {
    nodes.push({
      id: 'condition-root',
      kind: 'trigger',
      source: null,
      path: [],
      x: LEFT,
      y: TOP,
      icon: 'bolt',
      kicker: '当',
      label: eventName(null),
      meta: '需要配置',
      admin: false,
      hasInput: false,
      hasOutput: true,
    })
    conditionExit = { id: 'condition-root', y: TOP }
  }

  const conditionExitNode = nodes.find(node => node.id === conditionExit.id)
  let previousId = conditionExit.id
  let x = conditionExitNode.x + FLOW_COLUMN_STEP
  const pipelineY = conditionExit.y

  const actions = Array.isArray(rule.actions) ? rule.actions : []
  const actionNodes = []
  actions.forEach((item, index) => {
    const id = `action-${item.binding_id || index}`
    const actionNode = {
      id,
      kind: 'action',
      index,
      source: item,
      x,
      y: pipelineY,
      icon: item.type === 'if' ? 'call_split' : item.type === 'wait' ? 'hourglass_empty' : item.type === 'set_variable' ? 'edit_note' : 'play_arrow',
      kicker: `动作 ${index + 1}`,
      label: actionName(item),
      meta: item.type === 'if' ? `${predicateSummary(item.condition, schema ? precedingSources(rule, item, schema) : [], rules, workflowEventChoices(rule, schema))} · ${ifBranches(item).length} 个出口` : item.type === 'wait' ? describeWait(item) : describeItem(item, 'action'),
      admin: isAdmin(item, 'action'),
      hasInput: true,
      hasOutput: true,
      availableStepIds: actions.slice(0, index).map(action => action.binding_id).filter(Boolean),
    }
    nodes.push(actionNode)
    actionNodes.push(actionNode)
    const previousAction = actions[index - 1]
    edges.push({
      id: `${previousId}-${id}`,
      from: previousId,
      to: id,
      kind: 'pipeline',
      channel: 'control',
      label: previousAction?.failure_actions?.length
        ? '成功后'
        : (index ? '然后' : '执行'),
      insertActionIndex: index,
    })
    previousId = id
    x += FLOW_COLUMN_STEP
  })

  nodes.push({
    id: 'add-action',
    kind: 'add',
    x,
    y: pipelineY,
    icon: 'add',
    kicker: '下一步',
    label: '添加动作',
    meta: '继续这条规则',
    hasInput: true,
    hasOutput: false,
  })
  edges.push({
    id: `${previousId}-add-action`,
    from: previousId,
    to: 'add-action',
    kind: 'add',
    channel: 'control',
    label: actions.at(-1)?.failure_actions?.length ? '成功后' : '',
    insertActionIndex: actions.length,
  })

  actions.forEach((action, actionIndex) => {
    const failureActions = Array.isArray(action.failure_actions) ? action.failure_actions : []
    if (!failureActions.length) return
    const parentNode = actionNodes[actionIndex]
    const availableStepIds = actions.slice(0, actionIndex).map(item => item.binding_id).filter(Boolean)
    let branchPreviousId = parentNode.id
    failureActions.forEach((failureAction, failureIndex) => {
      const id = `failure-action-${failureAction.binding_id || `${actionIndex}-${failureIndex}`}`
      const node = {
        id,
        kind: 'failure-action',
        index: failureIndex,
        parentIndex: actionIndex,
        source: failureAction,
        x: parentNode.x + (failureIndex + 1) * FLOW_COLUMN_STEP,
        y: pipelineY + FLOW_ROW_STEP,
        icon: 'build',
        kicker: `补救 ${failureIndex + 1}`,
        label: actionName(failureAction),
        meta: describeItem(failureAction, 'action'),
        admin: isAdmin(failureAction, 'action'),
        hasInput: true,
        hasOutput: true,
        availableStepIds: [...availableStepIds],
      }
      nodes.push(node)
      edges.push({
        id: `${branchPreviousId}-${id}`,
        from: branchPreviousId,
        to: id,
        kind: 'failure',
        channel: 'control',
        label: failureIndex ? '然后' : '失败时',
      })
      if (failureAction.binding_id) availableStepIds.push(failureAction.binding_id)
      branchPreviousId = id
    })
    if (action.on_error === 'continue') {
      const nextId = actionNodes[actionIndex + 1]?.id || 'add-action'
      edges.push({
        id: `${branchPreviousId}-${nextId}-recover`,
        from: branchPreviousId,
        to: nextId,
        kind: 'failure-continue',
        channel: 'control',
        label: '处理后继续',
      })
    }
  })

  return { nodes, edges }
}

export function routeFlowEdge(edge, nodesById) {
  const source = nodesById.get(edge.from)
  const target = nodesById.get(edge.to)
  if (!source || !target) return null

  const x1 = source.x + FLOW_NODE_WIDTH
  const sourceDataY = source.dataOutputY?.[edge.sourcePortName]
    ?? FLOW_DATA_PORT_Y + edge.sourcePortIndex * FLOW_DATA_PORT_STEP
  const targetDataY = target.dataInputY?.[edge.targetPortName]
    ?? FLOW_DATA_PORT_Y + edge.targetPortIndex * FLOW_DATA_PORT_STEP
  const y1 = source.y + (edge.channel === 'data' ? sourceDataY : FLOW_NODE_PORT_Y)
  const x2 = target.x
  const y2 = target.y + (edge.channel === 'data' ? targetDataY : FLOW_NODE_PORT_Y)

  if (x2 - x1 >= 24) {
    const middle = (x1 + x2) / 2
    return {
      ...edge,
      d: `M ${x1} ${y1} C ${middle} ${y1}, ${middle} ${y2}, ${x2} ${y2}`,
      labelX: middle,
      labelY: (y1 + y2) / 2 - 9,
    }
  }

  // 目标节点在来源左侧时，连线从两节点右侧绕行。
  const routeX = Math.max(x1, target.x + FLOW_NODE_WIDTH) + 58
  return {
    ...edge,
    d: `M ${x1} ${y1} L ${routeX} ${y1} L ${routeX} ${y2} L ${x2} ${y2}`,
    labelX: routeX - 8,
    labelY: (y1 + y2) / 2 - 9,
    reversed: true,
  }
}

export function buildIfSubflowGraph({ rule, action, schema, sources, rules, actionName, describeItem }) {
  const nodes = []
  const edges = []
  const branches = ifBranches(action)
  const groupOps = ['all', 'any', 'not']
  let row = 0
  const sourceNodes = new Map()
  const addEdge = (from, to, label = '', kind = 'pipeline') => edges.push({ id: `${from}-${to}`, from, to, label, kind, channel: 'control' })
  const depthOf = node => groupOps.includes(node?.op) ? 1 + Math.max(0, ...(node.children || []).map(depthOf)) : 0
  const maxDepth = Math.max(0, ...branches.filter(branch => branch.condition).map(branch => depthOf(branch.condition)))
  const decisionX = LEFT + (maxDepth + 2) * FLOW_COLUMN_STEP
  const availableStepIds = sources.filter(source => source.value.$ref.scope === 'step').map(source => source.value.$ref.node)
  function visit(node, branch, path = [], depth = 0) {
    const id = `predicate-${action.binding_id}-${branch.key}-${path.join('-') || 'root'}`
    const grouped = groupOps.includes(node?.op)
    const children = grouped ? (node.children || []).map((child, index) => visit(child, branch, [...path, index], depth + 1)) : []
    const y = children.length ? children.reduce((sum, child) => sum + child.y, 0) / children.length : TOP + row++ * FLOW_ROW_STEP
    const ports = grouped || ['run_status', 'event_occurred'].includes(node?.op) ? [] : ['left', ...(['is_true', 'is_false', 'exists', 'not_exists'].includes(node?.op) ? [] : ['right'])].map((key, index) => ({ id: `input:${key}`, name: key, index, label: key === 'left' ? '判断数据' : '比较目标', type: { type: ['is_true', 'is_false'].includes(node?.op) ? 'bool' : ['gt', 'gte', 'lt', 'lte'].includes(node?.op) ? 'number' : 'any' } }))
    nodes.push({ id, kind: 'predicate', source: node, predicatePath: path, branchKey: branch.key, x: LEFT + (maxDepth - depth + 1) * FLOW_COLUMN_STEP, y, icon: grouped ? 'account_tree' : 'rule', kicker: branch.label, label: grouped ? { all: '全部满足 · AND', any: '满足任一 · OR', not: '取反 · NOT' }[node.op] : '条件判断', meta: grouped ? `${children.length} 个子条件` : predicateSummary(node, sources, rules, workflowEventChoices(rule, schema)), hasInput: grouped && children.length > 0 || node?.op === 'event_occurred', hasOutput: true, availableStepIds, dataInputs: ports, dataOutputs: [] })
    children.forEach(child => addEdge(child.id, id, node.op === 'not' ? '取反' : node.op === 'all' ? '并且' : '或者', 'condition'))
    if (node?.op === 'event_occurred') {
      const trigger = workflowEventChoices(rule, schema).find(item => item.id === node.trigger_id)
      if (trigger) {
        const sourceId = `trigger-${trigger.id}`
        if (!sourceNodes.has(sourceId)) sourceNodes.set(sourceId, { id: sourceId, kind: 'trigger', source: trigger.source, readOnly: true, x: LEFT, y: TOP + sourceNodes.size * FLOW_ROW_STEP, icon: 'bolt', kicker: '本流程触发器', label: trigger.label, meta: '记录流程开始至条件执行时收到的事件', hasInput: false, hasOutput: true, dataInputs: [], dataOutputs: [] })
        sourceNodes.get(sourceId).hasOutput = true
        addEdge(sourceId, id, '事件发生记录', 'condition')
      }
    }
    ports.forEach(port => {
      const reference = node[port.name]?.$ref
      if (!reference || !['trigger', 'step'].includes(reference.scope)) return
      const source = sources.find(item => item.value.$ref.scope === reference.scope && item.value.$ref.node === reference.node && item.value.$ref.path[0] === reference.path[0])
      if (!source) return
      const sourceId = `${reference.scope === 'trigger' ? 'trigger' : 'action'}-${reference.node}`
      if (!sourceNodes.has(sourceId)) {
        const original = reference.scope === 'step' ? findActionSource(rule.actions, reference.node) : findTriggerSource(rule.condition, reference.node)
        sourceNodes.set(sourceId, { id: sourceId, kind: reference.scope === 'trigger' ? 'trigger' : 'action', source: original, readOnly: true, x: LEFT, y: TOP + sourceNodes.size * FLOW_ROW_STEP, icon: reference.scope === 'trigger' ? 'bolt' : 'output', kicker: reference.scope === 'trigger' ? '本次触发数据' : '前序动作输出', label: source.label.split(' · ')[0], meta: '读取本次运行的数据', hasInput: false, hasOutput: false, availableStepIds: [], dataInputs: [] })
      }
      const sourceOutput = sourceNodes.get(sourceId)
      const outputs = sourceOutput.dataOutputs ||= []
      if (!outputs.some(item => item.name === reference.path[0])) outputs.push({ id: `output:${reference.path[0]}`, name: reference.path[0], label: reference.path[0], type: source.type, required: !source.optional, index: outputs.length })
      edges.push({ id: `data-${sourceId}-${id}-${port.name}`, from: sourceId, to: id, channel: 'data', kind: 'data', sourcePortName: reference.path[0], sourcePortIndex: outputs.findIndex(item => item.name === reference.path[0]), targetPortName: port.name, targetPortIndex: port.index, label: port.label, valid: true })
    })
    return { id, y }
  }
  const conditionRoots = branches.filter(branch => branch.condition).map(branch => ({ branch, ...visit(branch.condition, branch) }))
  const gateId = `if-decision-${action.binding_id}`
  const gateY = conditionRoots[0]?.y || TOP
  nodes.push({ id: gateId, kind: 'if-branch', source: action, x: decisionX, y: gateY, icon: 'call_split', kicker: 'IF / ELIF / ELSE', label: '按顺序选择一个分支', meta: '首个成立出口执行；否则走 ELSE', hasInput: true, hasOutput: true })
  conditionRoots.forEach(root => addEdge(root.id, gateId, root.branch.label, 'condition'))
  const maxActions = Math.max(1, ...branches.map(branch => branch.actions.length))
  const mergeId = `if-merge-${action.binding_id}`
  branches.forEach((branch, branchIndex) => {
    let previous = gateId
    const y = TOP + branchIndex * FLOW_ROW_STEP
    branch.actions.forEach((item, index) => {
      const id = `action-${item.binding_id}`
      nodes.push({ id, kind: 'action', source: item, branchKey: branch.key, index, x: decisionX + (index + 1) * FLOW_COLUMN_STEP, y, icon: item.type === 'if' ? 'call_split' : 'play_arrow', kicker: `${branch.label} · ${index + 1}`, label: actionName(item), meta: item.type === 'if' ? predicateSummary(item.condition, precedingSources(rule, item, schema), rules, workflowEventChoices(rule, schema)) : describeItem(item, 'action'), hasInput: true, hasOutput: true, availableStepIds: precedingSources(rule, item, schema).filter(source => source.value.$ref.scope === 'step').map(source => source.value.$ref.node) })
      addEdge(previous, id, index === 0 ? branch.label : '然后')
      let failurePrevious = id
      ;(item.failure_actions || []).forEach((failure, failureIndex) => {
        const failureId = `failure-action-${failure.binding_id}`
        nodes.push({ id: failureId, kind: 'failure-action', source: failure, owner: item, branchKey: branch.key, index: failureIndex, x: decisionX + (index + failureIndex + 2) * FLOW_COLUMN_STEP, y: y + FLOW_ROW_STEP / 2, icon: 'build', kicker: `${branch.label} · 补救 ${failureIndex + 1}`, label: actionName(failure), meta: describeItem(failure, 'action'), hasInput: true, hasOutput: true, availableStepIds: precedingSources(rule, failure, schema).filter(source => source.value.$ref.scope === 'step').map(source => source.value.$ref.node) })
        addEdge(failurePrevious, failureId, failureIndex ? '然后' : '失败时', 'failure')
        failurePrevious = failureId
      })
      previous = id
    })
    const addId = `branch-add-${action.binding_id}-${branch.key}`
    nodes.push({ id: addId, kind: 'if-branch', branchKey: branch.key, addBranch: true, source: action, x: decisionX + (branch.actions.length + 1) * FLOW_COLUMN_STEP, y, icon: 'add', kicker: branch.label, label: '添加分支动作', meta: branch.actions.length ? '继续此分支' : '空分支继续后续流程', hasInput: true, hasOutput: true })
    addEdge(previous, addId, branch.actions.length ? '' : branch.label)
    addEdge(addId, mergeId, '继续')
  })
  nodes.push({ id: mergeId, kind: 'if-merge', source: action, x: decisionX + (maxActions + 2) * FLOW_COLUMN_STEP, y: gateY, icon: 'merge', kicker: '分支结束', label: '返回公共后续流程', meta: '只执行一次后续动作', hasInput: true, hasOutput: false })
  return { nodes: [...sourceNodes.values(), ...nodes], edges }
}
function findActionSource(actions, id) {
  for (const action of actions || []) {
    if (action.binding_id === id) return action
    for (const branch of ifBranches(action)) { const found = findActionSource(branch.actions, id); if (found) return found }
    const failure = findActionSource(action.failure_actions, id); if (failure) return failure
  }
}
function findTriggerSource(node, id) {
  if (node?.binding_id === id) return node
  for (const child of node?.children || []) { const found = findTriggerSource(child, id); if (found) return found }
}
