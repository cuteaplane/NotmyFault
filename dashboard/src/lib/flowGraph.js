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

function pathId(path) {
  return path.length ? `condition-${path.join('-')}` : 'condition-root'
}

export function buildFlowGraph({
  rule,
  condition,
  eventName,
  actionName,
  describeItem,
  isAdmin,
}) {
  const nodes = []
  const edges = []
  let conditionExit
  let leafIndex = 0

  if (condition) {
    const maxDepth = conditionDepth(condition)

    function visit(node, path, depth) {
      const id = pathId(path)
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
      const childLayouts = children.map((child, index) => visit(child, [...path, index], depth + 1))
      const y = childLayouts.length
        ? childLayouts.reduce((total, child) => total + child.y, 0) / childLayouts.length
        : TOP
      const op = node?.op === 'all' ? 'all' : 'any'
      nodes.push({
        id,
        kind: 'condition',
        path,
        source: node,
        x: LEFT + (maxDepth - depth) * FLOW_COLUMN_STEP,
        y,
        icon: op === 'all' ? 'done_all' : 'alt_route',
        kicker: '逻辑汇合',
        label: op === 'all' ? '全部满足 · AND' : '满足任一 · OR',
        meta: `${children.length} 条分支${op === 'all' && node?.within_seconds ? ` · ${node.within_seconds} 秒内` : ''}`,
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
    const item = rule.event
    nodes.push({
      id: 'trigger',
      kind: 'trigger',
      source: item,
      x: LEFT,
      y: TOP,
      icon: 'bolt',
      kicker: '当',
      label: eventName(item),
      meta: item ? describeItem(item, 'trigger') : '需要配置',
      admin: item ? isAdmin(item, 'trigger') : false,
      hasInput: false,
      hasOutput: true,
    })
    conditionExit = { id: 'trigger', y: TOP }
  }

  const conditionExitNode = nodes.find(node => node.id === conditionExit.id)
  let previousId = conditionExit.id
  let x = conditionExitNode.x + FLOW_COLUMN_STEP
  const pipelineY = conditionExit.y

  const preconditions = Array.isArray(rule.preconditions) ? rule.preconditions : []
  preconditions.forEach((item, index) => {
    const id = `precondition-${item.binding_id || index}`
    nodes.push({
      id,
      kind: 'precondition',
      index,
      source: item,
      x,
      y: pipelineY,
      icon: 'verified_user',
      kicker: `确认 ${index + 1}`,
      label: actionName(item),
      meta: describeItem(item, 'action'),
      admin: isAdmin(item, 'action'),
      hasInput: true,
      hasOutput: true,
    })
    edges.push({
      id: `${previousId}-${id}`,
      from: previousId,
      to: id,
      kind: 'pipeline',
      channel: 'control',
      label: index ? '再确认' : '开始前',
    })
    previousId = id
    x += FLOW_COLUMN_STEP
  })

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
      icon: 'play_arrow',
      kicker: `动作 ${index + 1}`,
      label: actionName(item),
      meta: describeItem(item, 'action'),
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
        : (index || preconditions.length ? '然后' : '执行'),
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
