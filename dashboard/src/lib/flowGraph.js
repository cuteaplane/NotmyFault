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
  expandBranches = true,
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
  const stepIds = item => schema ? precedingSources(rule, item, schema)
    .filter(source => source.value.$ref.scope === 'step').map(source => source.value.$ref.node) : []
  const connect = (from, to, label = '', kind = 'pipeline', extra = {}) => edges.push({
    id: `${from}-${to}`, from, to, label, kind, channel: 'control', ...extra,
  })

  function sequence(items, startId, startX, y, end, scope = {}) {
    let previous = startId
    let x = startX
    let bottom = y
    let recoveryTail = null
    let previousAction = null
    items.forEach((item, index) => {
      const id = `action-${item.binding_id}`
      nodes.push({
        id, kind: 'action', index, items, source: item, ...scope, x, y,
        icon: item.type === 'if' ? 'call_split' : item.type === 'wait' ? 'hourglass_empty' : item.type === 'set_variable' ? 'edit_note' : 'play_arrow',
        kicker: scope.branchLabel ? `${scope.branchLabel} · ${index + 1}` : `动作 ${index + 1}`,
        label: actionName(item),
        meta: item.type === 'if' ? `${predicateSummary(item.condition, schema ? precedingSources(rule, item, schema) : [], rules, workflowEventChoices(rule, schema))} · ${ifBranches(item).length} 个出口` : item.type === 'wait' ? describeWait(item) : describeItem(item, 'action'),
        admin: isAdmin(item, 'action'), hasInput: true, hasOutput: true, availableStepIds: stepIds(item),
      })
      connect(previous, id, index === 0 ? (scope.branchLabel || '执行') : previousAction?.failure_actions?.length ? '成功后' : '然后', 'pipeline', {
        insertActionIndex: index, ownerId: scope.parentIf?.binding_id, branchKey: scope.branchKey, branchLabel: scope.branchLabel,
      })
      if (recoveryTail) connect(recoveryTail, id, '处理后继续', 'failure-continue')
      recoveryTail = null
      let exit = id
      let nextX = x + FLOW_COLUMN_STEP
      let blockBottom = y
      if (item.type === 'if' && expandBranches) {
        const mergeId = `if-merge-${item.binding_id}`
        let branchY = y
        let mergeX = nextX
        for (const branch of ifBranches(item)) {
          const addId = `branch-add-${item.binding_id}-${branch.key}`
          const branchLayout = sequence(branch.actions, id, x + FLOW_COLUMN_STEP, branchY, {
            id: addId, kind: 'if-branch', branchKey: branch.key, addBranch: true,
            source: item, icon: 'add', kicker: branch.label, label: '添加分支动作',
            meta: branch.actions.length ? '继续此分支' : '空分支继续后续流程', hasOutput: true,
          }, { parentIf: item, branchKey: branch.key, branchLabel: branch.label })
          mergeX = Math.max(mergeX, branchLayout.nextX)
          blockBottom = Math.max(blockBottom, branchLayout.bottom)
          branchY = branchLayout.bottom + FLOW_ROW_STEP
          connect(addId, mergeId, '继续')
        }
        nodes.push({ id: mergeId, kind: 'if-merge', source: item, x: mergeX, y,
          icon: 'merge', kicker: '分支结束', label: '返回公共后续流程',
          meta: '只执行一次后续动作', hasInput: true, hasOutput: true, dataInputs: [], dataOutputs: [],
        })
        exit = mergeId
        nextX = mergeX + FLOW_COLUMN_STEP
      }
      const failures = item.failure_actions || []
      let failurePrevious = id
      failures.forEach((failure, failureIndex) => {
        const failureId = `failure-action-${failure.binding_id}`
        nodes.push({ id: failureId, kind: 'failure-action', source: failure, owner: item,
          index: failureIndex, parentIndex: index, items: failures, ...scope,
          x: x + (failureIndex + 1) * FLOW_COLUMN_STEP, y: blockBottom + FLOW_ROW_STEP,
          icon: 'build', kicker: `补救 ${failureIndex + 1}`, label: actionName(failure),
          meta: describeItem(failure, 'action'), admin: isAdmin(failure, 'action'),
          hasInput: true, hasOutput: true, availableStepIds: stepIds(failure),
        })
        connect(failurePrevious, failureId, failureIndex ? '然后' : '失败时', 'failure')
        failurePrevious = failureId
      })
      if (failures.length) {
        blockBottom += FLOW_ROW_STEP
        nextX = Math.max(nextX, x + (failures.length + 1) * FLOW_COLUMN_STEP)
        if (item.on_error === 'continue') recoveryTail = failurePrevious
      }
      bottom = Math.max(bottom, blockBottom)
      previous = exit
      previousAction = item
      x = nextX
    })
    nodes.push({ ...end, x, y, hasInput: true })
    connect(previous, end.id, items.length ? (previousAction?.failure_actions?.length ? '成功后' : '') : scope.branchLabel || '', 'add', {
      insertActionIndex: items.length, ownerId: scope.parentIf?.binding_id, branchKey: scope.branchKey, branchLabel: scope.branchLabel,
    })
    if (recoveryTail) connect(recoveryTail, end.id, '处理后继续', 'failure-continue')
    return { nextX: x + FLOW_COLUMN_STEP, bottom }
  }
  sequence(rule.actions || [], conditionExit.id, conditionExitNode.x + FLOW_COLUMN_STEP, conditionExit.y, {
    id: 'add-action', kind: 'add', icon: 'add', kicker: '下一步', label: '添加动作',
    meta: '继续这条规则', hasOutput: false,
  })

  return { nodes, edges }
}

export function routeFlowEdge(edge, nodesById) {
  const source = nodesById.get(edge.from)
  const target = nodesById.get(edge.to)
  if (!source || !target) return null

  const x1 = source.x + (edge.sourceSide === 'left' ? 0 : source.width || FLOW_NODE_WIDTH)
  const sourceDataY = source.dataOutputY?.[edge.sourcePortName]
    ?? FLOW_DATA_PORT_Y + edge.sourcePortIndex * FLOW_DATA_PORT_STEP
  const targetDataY = target.dataInputY?.[edge.targetPortName]
    ?? FLOW_DATA_PORT_Y + edge.targetPortIndex * FLOW_DATA_PORT_STEP
  const y1 = source.y + (edge.channel === 'data' ? sourceDataY : edge.sourceInput ? source.inputY ?? FLOW_NODE_PORT_Y : (source.outputs || source.outputAnchors)?.find(port => port.id === edge.sourcePort)?.y ?? FLOW_NODE_PORT_Y)
  const x2 = target.x + (edge.targetPort ? target.width || FLOW_NODE_WIDTH : 0)
  const y2 = target.y + (edge.channel === 'data' ? targetDataY : edge.targetPort ? target.outputs?.find(port => port.id === edge.targetPort)?.y ?? FLOW_NODE_PORT_Y : target.inputY ?? FLOW_NODE_PORT_Y)

  if (x2 >= x1) {
    if (edge.targetPort && target.frame && source.groupOwner === target.source) {
      const obstacles = [...nodesById.values()].filter(node => node !== source && !node.frame && node.groupOwner === target.source && node.x >= x1 && node.x < x2)
      if (obstacles.some(node => node.y < Math.max(y1, y2) + 10 && node.y + (node.height || node.fixedHeight || FLOW_NODE_BASE_HEIGHT) > Math.min(y1, y2) - 10)) {
        const outlet = target.outputs.findIndex(port => port.id === edge.targetPort)
        const bottom = target.y + (target.height || target.fixedHeight) - 6 - 16 * (outlet + 1) / (target.outputs.length + 1)
        const left = x1 + 28, right = x2 - 24
        return { ...edge, d: `M ${x1} ${y1} H ${left - 8} Q ${left} ${y1}, ${left} ${y1 + 8} V ${bottom - 8} Q ${left} ${bottom}, ${left + 8} ${bottom} H ${right - 8} Q ${right} ${bottom}, ${right} ${bottom - 8} V ${y2 + 8} Q ${right} ${y2}, ${right + 8} ${y2} H ${x2}`, labelX: x1 + 50, labelY: y1 + 30 }
      }
    }
    const outlet = source.outputs?.findIndex(port => port.id === edge.sourcePort) ?? -1
    if (source.kind === 'event_group' && edge.channel !== 'data' && !edge.targetPort && outlet >= 0 && y2 - y1 > 64 && x2 - x1 > 48) {
      const rail = x1 + (x2 - x1) * (source.outputs.length - outlet) / (source.outputs.length + 1)
      const radius = Math.min(12, (rail - x1) / 2, (x2 - rail) / 2)
      return { ...edge, d: `M ${x1} ${y1} H ${rail - radius} Q ${rail} ${y1}, ${rail} ${y1 + radius} V ${y2 - radius} Q ${rail} ${y2}, ${rail + radius} ${y2} H ${x2}`, labelX: rail, labelY: (y1 + y2) / 2 - 9 }
    }
    const middle = (x1 + x2) / 2
    return {
      ...edge,
      d: `M ${x1} ${y1} C ${middle} ${y1}, ${middle} ${y2}, ${x2} ${y2}`,
      labelX: middle,
      labelY: (y1 + y2) / 2 - 9,
    }
  }

  // 目标节点在来源左侧时，连线从两节点右侧绕行。
  const routeX = Math.max(x1, target.x + (target.width || FLOW_NODE_WIDTH)) + 58
  return {
    ...edge,
    d: `M ${x1} ${y1} L ${routeX} ${y1} L ${routeX} ${y2} L ${x2} ${y2}`,
    labelX: routeX - 8,
    labelY: (y1 + y2) / 2 - 9,
    reversed: true,
  }
}

export function buildIfSubflowGraph({ rule, action, schema, sources, rules }) {
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
  nodes.push({ id: gateId, kind: 'if-branch', source: action, x: decisionX, y: gateY, icon: 'call_split', kicker: 'IF / ELIF / ELSE', label: '按顺序选择一个分支', meta: '分支动作在主流程图中编辑', hasInput: true, hasOutput: false })
  conditionRoots.forEach(root => addEdge(root.id, gateId, root.branch.label, 'condition'))
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
