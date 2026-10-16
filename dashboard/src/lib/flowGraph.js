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
  const x2 = target.x + (edge.targetPort ? target.width || FLOW_NODE_WIDTH : target.inputs?.find(port => port.id === edge.inputPort)?.x || 0)
  const y2 = target.y + (edge.channel === 'data' ? targetDataY : edge.targetPort ? target.outputs?.find(port => port.id === edge.targetPort)?.y ?? FLOW_NODE_PORT_Y : target.inputs?.find(port => port.id === edge.inputPort)?.y ?? target.inputY ?? FLOW_NODE_PORT_Y)

  if (x2 >= x1) {
    const obstacles = [...nodesById.values()].filter(node => node !== source && node !== target && !node.frame)
      .map(node => ({ left: node.x - 10, right: node.x + (node.width || FLOW_NODE_WIDTH) + 10, top: node.y - 10, bottom: node.y + (node.height || node.fixedHeight || FLOW_NODE_BASE_HEIGHT) + 10 }))
    const middle = (x1 + x2) / 2
    const caption = [0.5, 0.35, 0.65, 0.2, 0.8].map(t => {
      const u = 1 - t
      return { x: u ** 3 * x1 + 3 * u * t * middle + t ** 3 * x2, y: (u ** 3 + 3 * u * u * t) * y1 + (3 * u * t * t + t ** 3) * y2 - 9 }
    }).find(point => !obstacles.some(box => point.x + Math.max(24, (edge.label || '').length * 4) > box.left && point.x - Math.max(24, (edge.label || '').length * 4) < box.right && point.y + 6 > box.top && point.y - 14 < box.bottom))
    const blockedCurve = obstacles.some(box => {
      for (let index = 1; index < 40; index++) {
        const t = index / 40, u = 1 - t
        const x = u ** 3 * x1 + 3 * u * t * middle + t ** 3 * x2
        const y = (u ** 3 + 3 * u * u * t) * y1 + (3 * u * t * t + t ** 3) * y2
        if (x > box.left && x < box.right && y > box.top && y < box.bottom) return true
      }
      return false
    })
    if (blockedCurve && !edge.targetPort && x2 - x1 > 48) {
      const clear = (a, b) => !obstacles.some(box => a.x === b.x ? a.x > box.left && a.x < box.right && Math.max(a.y, b.y) > box.top && Math.min(a.y, b.y) < box.bottom : a.y > box.top && a.y < box.bottom && Math.max(a.x, b.x) > box.left && Math.min(a.x, b.x) < box.right)
      const rows = [...new Set([y1, y2, ...obstacles.filter(box => box.right > x1 && box.left < x2).flatMap(box => [box.top - 14, box.bottom + 14])])]
      const routes = rows.map(y => [{ x: x1, y: y1 }, { x: x1 + 24, y: y1 }, { x: x1 + 24, y }, { x: x2 - 24, y }, { x: x2 - 24, y: y2 }, { x: x2, y: y2 }])
        .filter(points => points.slice(1).every((point, index) => clear(points[index], point)))
        .sort((a, b) => a.slice(1).reduce((sum, point, index) => sum + Math.abs(point.x - a[index].x) + Math.abs(point.y - a[index].y), 0) - b.slice(1).reduce((sum, point, index) => sum + Math.abs(point.x - b[index].x) + Math.abs(point.y - b[index].y), 0))
      if (routes.length) {
        const points = routes[0].filter((point, index, all) => !index || point.x !== all[index - 1].x || point.y !== all[index - 1].y)
        let d = `M ${points[0].x} ${points[0].y}`
        for (let index = 1; index < points.length; index++) {
          const point = points[index], before = points[index - 1], after = points[index + 1]
          if (!after) { d += ` L ${point.x} ${point.y}`; continue }
          const radius = Math.min(8, Math.hypot(point.x - before.x, point.y - before.y) / 2, Math.hypot(after.x - point.x, after.y - point.y) / 2)
          d += ` L ${point.x - Math.sign(point.x - before.x) * radius} ${point.y - Math.sign(point.y - before.y) * radius} Q ${point.x} ${point.y}, ${point.x + Math.sign(after.x - point.x) * radius} ${point.y + Math.sign(after.y - point.y) * radius}`
        }
        return { ...edge, d, labelX: (x1 + x2) / 2, labelY: routes[0][2].y - 9 }
      }
    }
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
    return {
      ...edge,
      d: `M ${x1} ${y1} C ${middle} ${y1}, ${middle} ${y2}, ${x2} ${y2}`,
      label: caption ? edge.label : '',
      labelX: caption?.x ?? middle,
      labelY: caption?.y ?? (y1 + y2) / 2 - 9,
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
