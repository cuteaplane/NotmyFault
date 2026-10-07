<script>
const canvasViews = new WeakMap()
</script>

<script setup>
import { computed, nextTick, ref, watch, onMounted, onUnmounted } from 'vue'
import { store } from '../lib/store'
import { FLOW_DATA_PORT_STEP, FLOW_DATA_PORT_Y, FLOW_DATA_SUMMARY_HEIGHT, FLOW_NODE_ADMIN_EXTRA_HEIGHT,
  FLOW_NODE_BASE_HEIGHT, FLOW_NODE_WIDTH, buildFlowGraph, conditionNodeId, routeFlowEdge } from '../lib/flowGraph'
import { buildNodeDataPorts, deriveDataEdges, typesCompatible } from '../lib/bindings'
import { typeLabel } from '../lib/valueTypes'
import { isAdmin, eventName, actionName, describeItem } from '../lib/rulePresentation'

const props = defineProps({ rule: Object, layout: { type: String, default: 'canvas' }, selectedNodeId: String, graphOverride: Object, viewKey: { type: String, default: 'main' } })
const emit = defineEmits(['update:selectedNodeId', 'select-node', 'add-if', 'add-assignment', 'add-wait', 'open-picker', 'move-action', 'remove-action', 'move-failure-action', 'remove-failure-action', 'add-condition-child', 'open-if', 'predicate-select', 'branch-add', 'branch-remove'])
const canvasLayout = computed(() => props.layout === 'canvas')
const selectedNodeId = computed({ get: () => props.selectedNodeId, set: id => emit('update:selectedNodeId', id) })
const conditionNode = computed(() => props.rule.condition || null)
const typeCatalog = computed(() => Object.entries(store.schema.data_types?.custom || {}).map(([id, definition]) => ({ id, ...definition })))

const nodePositions = ref({})

const portExpansion = ref({})

const dataDrag = ref(null)

const dataDropTarget = ref(null)

let dragState = null
const connectionPositions = ref(null)

function sideExpanded(nodeId, side) {
  return !!portExpansion.value[nodeId]?.[side]
}

function portYMap(ports, startY) {
  return Object.fromEntries(ports.map((port, index) => [port.name, startY + index * FLOW_DATA_PORT_STEP + FLOW_DATA_PORT_STEP / 2]))
}

function resolveNodeCollisions(nodes) {
  const gapX = 72
  const gapY = 56
  const placed = []
  const resolved = new Map()
  // 自动布局先占用用户手动摆放的位置，端口展开时这些节点仍保留原位置。
  const ordered = [...nodes].sort((a, b) => Number(b.manuallyPlaced) - Number(a.manuallyPlaced))
  ordered.forEach(original => {
    const node = { ...original }
    if (!node.manuallyPlaced) {
      let guard = 0
      while (guard++ < 100) {
        const collision = placed.find(other => (
          node.x < other.x + FLOW_NODE_WIDTH + gapX
          && node.x + FLOW_NODE_WIDTH + gapX > other.x
          && node.y < other.y + other.height + gapY
          && node.y + node.height + gapY > other.y
        ))
        if (!collision) break
        node.y = collision.y + collision.height + gapY
      }
    }
    placed.push(node)
    resolved.set(node.id, node)
  })
  return nodes.map(node => resolved.get(node.id))
}

const graph = computed(() => {
  const controlGraph = props.graphOverride || buildFlowGraph({
    rule: props.rule,
    schema: store.schema,
    rules: store.configData.rules,
    condition: conditionNode.value,
    eventName,
    actionName,
    describeItem,
    isAdmin: (item, kind) => isAdmin(
      kind === 'trigger' ? store.schema.triggers[item?.type] : store.schema.actions[item?.type],
    ),
  })
  const fullNodes = controlGraph.nodes.map(node => {
    const built = buildNodeDataPorts(node, store.schema, props.rule)
    return { ...built, dataInputs: node.dataInputs ?? built.dataInputs, dataOutputs: node.dataOutputs ?? built.dataOutputs }
  })
  const suppliedDataEdges = controlGraph.edges.filter(edge => edge.channel === 'data')
  const suppliedIds = new Set(suppliedDataEdges.map(edge => edge.id))
  const dataEdges = [...suppliedDataEdges, ...deriveDataEdges(props.rule, fullNodes, typeCatalog.value).filter(edge => !suppliedIds.has(edge.id))]
  const usedInputs = new Map()
  const usedOutputs = new Map()
  for (const edge of dataEdges) {
    if (!usedInputs.has(edge.to)) usedInputs.set(edge.to, new Set())
    if (!usedOutputs.has(edge.from)) usedOutputs.set(edge.from, new Set())
    usedInputs.get(edge.to).add(edge.targetPortName)
    usedOutputs.get(edge.from).add(edge.sourcePortName)
  }
  const dragSource = dataDrag.value
    ? fullNodes.find(node => node.id === dataDrag.value.sourceNodeId)
    : null
  const dragSourcePort = dragSource?.dataOutputs.find(port => port.name === dataDrag.value?.sourcePortName)
  const nodes = fullNodes.map(node => {
    const boundInputs = usedInputs.get(node.id) || new Set()
    const boundOutputs = usedOutputs.get(node.id) || new Set()
    const compatibleInputs = dragSource && dragSourcePort
      ? node.dataInputs.filter(port => canConnectDataPorts(dragSource, dragSourcePort, node, port))
      : []
    const visibleDataInputs = sideExpanded(node.id, 'inputs')
      ? node.dataInputs
      : node.dataInputs.filter(port => boundInputs.has(port.name) || compatibleInputs.some(item => item.name === port.name))
    const visibleDataOutputs = sideExpanded(node.id, 'outputs')
      ? node.dataOutputs
      : node.dataOutputs.filter(port => boundOutputs.has(port.name))
    const hasDataPorts = !!(node.dataInputs.length || node.dataOutputs.length)
    const dataPortRows = Math.max(visibleDataInputs.length, visibleDataOutputs.length)
    const contentExtraHeight = node.admin ? FLOW_NODE_ADMIN_EXTRA_HEIGHT : 0
    const dataStartY = FLOW_NODE_BASE_HEIGHT + contentExtraHeight
      + (hasDataPorts ? FLOW_DATA_SUMMARY_HEIGHT : 0)
    const footerHeight = !node.readOnly && ['action', 'failure-action'].includes(node.kind) ? 34 : node.kind === 'condition' ? 40 : 0
    return {
      ...node,
      visibleDataInputs,
      visibleDataOutputs,
      hasDataPorts,
      dataPortRows,
      dataInputY: portYMap(visibleDataInputs, dataStartY),
      dataOutputY: portYMap(visibleDataOutputs, dataStartY),
      height: FLOW_NODE_BASE_HEIGHT
        + contentExtraHeight
        + (hasDataPorts ? FLOW_DATA_SUMMARY_HEIGHT : 0)
        + dataPortRows * FLOW_DATA_PORT_STEP
        + footerHeight,
    }
  })
  return {
    nodes,
    edges: [...controlGraph.edges.filter(edge => edge.channel !== 'data'), ...dataEdges],
  }
})

const layoutNodes = computed(() => resolveNodeCollisions(graph.value.nodes.map(node => ({
  ...node,
  ...(connectionPositions.value?.[node.id] || nodePositions.value[node.id] || {}),
  manuallyPlaced: !!(connectionPositions.value?.[node.id] || nodePositions.value[node.id]),
}))))

const graphEdges = computed(() => {
  const nodesById = new Map(layoutNodes.value.map(node => [node.id, node]))
  return graph.value.edges.map(edge => routeFlowEdge(edge, nodesById)).filter(Boolean)
})
const selectedGraphNode = computed(() => layoutNodes.value.find(node => node.id === selectedNodeId.value) || null)
watch(layoutNodes, nodes => {
  if (
    selectedNodeId.value
    && !nodes.some(node => node.id === selectedNodeId.value)
  ) selectedNodeId.value = null
})
function selectNode(id, reveal = false) {
  if (suppressNodeClickId === id) { suppressNodeClickId = null; return }
  pointerSelectionId = reveal ? null : id
  const node = layoutNodes.value.find(item => item.id === id)
  if (node?.readOnly) return
  if (canvasLayout.value && node?.kind === 'action' && node.source?.type === 'if') emit('open-if', node.source)
  else if (node?.kind === 'predicate') { emit('select-node', id); emit('predicate-select', node) }
  else if (node?.kind === 'if-branch' && node.addBranch) emit('branch-add', node)
  else emit('select-node', id)
}
function focusNode(node) {
  if (keyboardNavigation && canvasLayout.value && !dataDrag.value && !dragState) centerOnNode(node)
}

// 画布相机只改变 0×0 容器的 translate 和 scale，节点、连线都用同一逻辑坐标。
const viewportRef = ref(null)
const viewportSize = ref({ w: 0, h: 0 })
const zoom = ref(1)
const pan = ref({ x: 0, y: 0 })
const isPanning = ref(false)
const camAnim = ref(false)
const hoverNodeId = ref(null)
const ZOOM_MIN = 0.25
const ZOOM_MAX = 2
const INSPECTOR_WIDTH = 380
let panState = null
let minimapDragging = false
let resizeObserver = null
let fittedOnce = false
let camAnimTimer = null
let suppressNodeClickId = null
let suppressNodeClickTimer = null
let pointerSelectionId = null
let keyboardNavigation = false
let edgePanFrame = null
let edgePanTime = null
let gesturePointer = null
let captureTarget = null
let capturePointerId = null
let activeView = props.viewKey
let activeRule = props.rule

function clampZoom(value) { return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, value)) }
function animateCamera() {
  camAnim.value = true
  if (camAnimTimer) clearTimeout(camAnimTimer)
  camAnimTimer = setTimeout(() => { camAnim.value = false }, 300)
}

// 以视口坐标 cx、cy 为锚点缩放，锚点下的内容位置保持不变。
function zoomAt(nextZoom, cx, cy) {
  const z = clampZoom(nextZoom)
  const scale = z / zoom.value
  pan.value = {
    x: cx - (cx - pan.value.x) * scale,
    y: cy - (cy - pan.value.y) * scale,
  }
  zoom.value = z
}
function onCanvasWheel(event) {
  if (!canvasLayout.value) return
  if (!viewportRef.value) return
  event.preventDefault()
  if (dataDrag.value || dragState) return
  const rect = viewportRef.value.getBoundingClientRect()
  const factor = event.deltaY < 0 ? 1.12 : 1 / 1.12
  zoomAt(zoom.value * factor, event.clientX - rect.left, event.clientY - rect.top)
}
function zoomStep(factor) {
  animateCamera()
  zoomAt(zoom.value * factor, viewportSize.value.w / 2, viewportSize.value.h / 2)
}
function resetZoom() {
  animateCamera()
  zoomAt(1, viewportSize.value.w / 2, viewportSize.value.h / 2)
}

// 只有在空白处拖拽才会平移画布。
function startPan(event) {
  if (!canvasLayout.value) return
  if (event.button !== 0) return
  if (event.target.closest('.graph-node') || event.target.closest('button')) return
  selectedNodeId.value = null
  stopCameraAnimation()
  panState = { startX: event.clientX, startY: event.clientY, x: pan.value.x, y: pan.value.y, target: event.currentTarget, pointerId: event.pointerId }
  isPanning.value = true
  event.currentTarget.setPointerCapture?.(event.pointerId)
}
function movePan(event) {
  if (!panState) return
  pan.value = {
    x: panState.x + event.clientX - panState.startX,
    y: panState.y + event.clientY - panState.startY,
  }
}
function endPan() {
  if (panState?.target.hasPointerCapture?.(panState.pointerId)) panState.target.releasePointerCapture(panState.pointerId)
  panState = null
  isPanning.value = false
}

function dataReference(node, port) {
  return {
    $ref: {
      scope: node.kind === 'trigger' ? 'trigger' : 'step',
      node: node.source.binding_id,
      path: [port.name],
      ...(!port.required ? { on_missing: 'error' } : {}),
    },
  }
}
function canConnectDataPorts(sourceNode, sourcePort, targetNode, targetPort) {
  if (!typesCompatible(sourcePort.type, targetPort.type)) return false
  if (['action', 'failure-action'].includes(sourceNode.kind)) {
    return !targetNode.readOnly && ['action', 'failure-action', 'predicate'].includes(targetNode.kind)
      && (targetNode.availableStepIds || []).includes(sourceNode.source.binding_id)
  }
  if (sourceNode.kind !== 'trigger') return false

  return !targetNode.readOnly && ['action', 'failure-action', 'predicate'].includes(targetNode.kind)
}
function inputPortAt(event) {
  const target = document.elementFromPoint?.(event.clientX, event.clientY)?.closest?.('[data-input-node][data-input-port]')
    || event.target?.closest?.('[data-input-node][data-input-port]')
  if (!target) return null
  const node = layoutNodes.value.find(item => item.id === target.dataset.inputNode)
  const port = node?.dataInputs.find(item => item.name === target.dataset.inputPort)
  return node && port ? { node, port } : null
}
function updateDataDropTarget(event) {
  if (!dataDrag.value || !matchesGesture(event)) return
  dataDrag.value = { ...dataDrag.value, clientX: event.clientX, clientY: event.clientY,
    moved: dataDrag.value.moved || Math.hypot(event.clientX - dataDrag.value.startX, event.clientY - dataDrag.value.startY) >= 5 }
  gesturePointer = { clientX: event.clientX, clientY: event.clientY }
  if (dataDrag.value.moved) scheduleEdgePan()
  const target = inputPortAt(event)
  const source = layoutNodes.value.find(node => node.id === dataDrag.value.sourceNodeId)
  const sourcePort = source?.dataOutputs.find(port => port.name === dataDrag.value.sourcePortName)
  dataDropTarget.value = target && source && sourcePort && canConnectDataPorts(source, sourcePort, target.node, target.port)
    ? target
    : null
}
function startDataDrag(event, node, port) {
  if (!canvasLayout.value || dataDrag.value || dragState) return
  if (event.button !== 0) return
  event.preventDefault()
  event.stopPropagation()
  stopCameraAnimation()
  connectionPositions.value = Object.fromEntries(layoutNodes.value.map(item => [item.id, { x: item.x, y: item.y }]))
  dataDrag.value = {
    sourceNodeId: node.id,
    sourcePortName: port.name,
    startX: event.clientX, startY: event.clientY, moved: false,
    clientX: event.clientX,
    clientY: event.clientY,
  }
  dataDropTarget.value = null
  captureGesture(event)
}
function endDataDrag(event) {
  if (!dataDrag.value || !matchesGesture(event)) return
  updateDataDropTarget(event)
  const source = layoutNodes.value.find(node => node.id === dataDrag.value.sourceNodeId)
  const sourcePort = source?.dataOutputs.find(port => port.name === dataDrag.value.sourcePortName)
  const target = dataDropTarget.value
  if (source && sourcePort && target && canConnectDataPorts(source, sourcePort, target.node, target.port)) {
    if (target.node.kind === 'predicate') target.node.source[target.port.name] = dataReference(source, sourcePort)
    else if (target.node.source.type === 'set_variable') target.node.source.value = dataReference(source, sourcePort)
    else {
      target.node.source.params ||= {}
      target.node.source.params[target.port.name] = dataReference(source, sourcePort)
    }
  }
  dataDrag.value = null
  dataDropTarget.value = null
  connectionPositions.value = null
  finishGesture()
}
function cancelDataDrag() {
  dataDrag.value = null
  dataDropTarget.value = null
  connectionPositions.value = null
  finishGesture()
}
function dataInputDropState(node, port) {
  if (!dataDrag.value) return ''
  const source = layoutNodes.value.find(item => item.id === dataDrag.value.sourceNodeId)
  const sourcePort = source?.dataOutputs.find(item => item.name === dataDrag.value.sourcePortName)
  if (!source || !sourcePort) return 'invalid'
  return canConnectDataPorts(source, sourcePort, node, port) ? 'valid' : 'invalid'
}
function togglePorts(nodeId, side) {
  const current = portExpansion.value[nodeId] || { inputs: false, outputs: false }
  portExpansion.value = {
    ...portExpansion.value,
    [nodeId]: { ...current, [side]: !current[side] },
  }
}
function dataSideLabel(node, side) {
  const ports = side === 'inputs' ? node.dataInputs : node.dataOutputs
  const visible = side === 'inputs' ? node.visibleDataInputs : node.visibleDataOutputs
  const label = side === 'inputs' ? '输入' : '输出'
  const used = visible.length && !sideExpanded(node.id, side) ? ` · 已用 ${visible.length}` : ''
  return `${label} ${ports.length}${used}`
}
function nodeDataIncompatible(node) {
  if (!dataDrag.value || node.id === dataDrag.value.sourceNodeId) return false
  const source = layoutNodes.value.find(item => item.id === dataDrag.value.sourceNodeId)
  const sourcePort = source?.dataOutputs.find(item => item.name === dataDrag.value.sourcePortName)
  if (!source || !sourcePort) return false
  if (!node.dataInputs.length) return true
  return !node.dataInputs.some(port => canConnectDataPorts(source, sourcePort, node, port))
}
const pendingDataEdge = computed(() => {
  if (!dataDrag.value || !viewportRef.value) return null
  const source = layoutNodes.value.find(node => node.id === dataDrag.value.sourceNodeId)
  const sourcePort = source?.dataOutputs.find(port => port.name === dataDrag.value.sourcePortName)
  if (!source || !sourcePort) return null
  const rect = viewportRef.value.getBoundingClientRect()
  const x1 = source.x + FLOW_NODE_WIDTH
  const y1 = source.y + (source.dataOutputY?.[sourcePort.name]
    ?? FLOW_DATA_PORT_Y + sourcePort.index * FLOW_DATA_PORT_STEP)
  const x2 = (dataDrag.value.clientX - rect.left - pan.value.x) / zoom.value
  const y2 = (dataDrag.value.clientY - rect.top - pan.value.y) / zoom.value
  const middle = Math.max(x1 + 40, (x1 + x2) / 2)
  return { d: `M ${x1} ${y1} C ${middle} ${y1}, ${middle} ${y2}, ${x2} ${y2}` }
})

// 内容包围盒按节点宽度 228 和高度约 120 计算。
const contentBounds = computed(() => {
  const nodes = layoutNodes.value
  if (!nodes.length) return { minX: 0, minY: 0, w: 900, h: 560 }
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
  for (const node of nodes) {
    minX = Math.min(minX, node.x)
    minY = Math.min(minY, node.y)
    maxX = Math.max(maxX, node.x + FLOW_NODE_WIDTH)
    maxY = Math.max(maxY, node.y + node.height)
  }
  return { minX, minY, maxX, maxY, w: maxX - minX, h: maxY - minY }
})

function fitView(options = {}) {
  const { w, h } = viewportSize.value
  if (!w || !h) return
  const bounds = contentBounds.value
  const padding = 72
  const readable = options?.readable === true
  const fittedZoom = Math.min((w - padding * 2) / bounds.w, (h - padding * 2) / bounds.h, 1.2)
  const z = clampZoom(readable ? Math.max(.72, fittedZoom) : fittedZoom)
  const overflowsHorizontally = bounds.w * z > w - padding * 2
  animateCamera()
  zoom.value = z
  pan.value = {
    x: readable && overflowsHorizontally
      ? padding - bounds.minX * z
      : (w - bounds.w * z) / 2 - bounds.minX * z,
    y: (h - bounds.h * z) / 2 - bounds.minY * z,
  }
}

// 右侧抽屉打开时，选中节点移到剩余的可见画布内。
function centerOnNode(node) {
  const { w, h } = viewportSize.value
  if (!w || !h || !node) return
  const drawerWidth = selectedNodeId.value && w > 760
    ? Math.min(INSPECTOR_WIDTH, w - 48)
    : 0
  const visibleWidth = Math.max(280, w - drawerWidth)
  const margin = Math.min(90, visibleWidth / 4)
  const nodeWidth = FLOW_NODE_WIDTH * zoom.value
  const nodeHeight = node.height * zoom.value
  let nextX = pan.value.x
  let nextY = pan.value.y
  const left = nextX + node.x * zoom.value
  const top = nextY + node.y * zoom.value

  if (left < margin || left + nodeWidth > visibleWidth - margin) {
    nextX = visibleWidth / 2 - (node.x + FLOW_NODE_WIDTH / 2) * zoom.value
  }
  if (top + nodeHeight < margin || top > h - margin) {
    nextY = h / 2 - (node.y + node.height / 2) * zoom.value
  }

  if (nextX === pan.value.x && nextY === pan.value.y) return
  animateCamera()
  pan.value = { x: nextX, y: nextY }
}
// 工具栏或添加节点触发选中时先把目标移到视野内，手动点击的节点直接保留原位。
watch(selectedGraphNode, (node, previous) => {
  if (node && node.id !== previous?.id && canvasLayout.value && !dataDrag.value && !dragState && pointerSelectionId !== node.id) centerOnNode(node)
  pointerSelectionId = null
})

// 小地图显示整张内容和当前视口，点击或拖动后跳到对应位置。
const minimapFrame = computed(() => {
  const bounds = contentBounds.value
  const padding = 90
  return {
    x: (bounds.minX ?? 0) - padding,
    y: (bounds.minY ?? 0) - padding,
    w: bounds.w + padding * 2,
    h: bounds.h + padding * 2,
  }
})
const minimapViewport = computed(() => ({
  x: -pan.value.x / zoom.value,
  y: -pan.value.y / zoom.value,
  w: viewportSize.value.w / zoom.value,
  h: viewportSize.value.h / zoom.value,
}))
function jumpMinimap(event) {
  const rect = event.currentTarget.getBoundingClientRect()
  if (!rect.width || !rect.height) return
  const frame = minimapFrame.value
  // preserveAspectRatio="meet" 会给不同比例的小地图留白，点击坐标要先换算到实际绘制区域。
  const scale = Math.min(rect.width / frame.w, rect.height / frame.h)
  const drawnWidth = frame.w * scale
  const drawnHeight = frame.h * scale
  const offsetX = (rect.width - drawnWidth) / 2
  const offsetY = (rect.height - drawnHeight) / 2
  const relativeX = Math.min(1, Math.max(0, (event.clientX - rect.left - offsetX) / drawnWidth))
  const relativeY = Math.min(1, Math.max(0, (event.clientY - rect.top - offsetY) / drawnHeight))
  const contentX = frame.x + relativeX * frame.w
  const contentY = frame.y + relativeY * frame.h
  pan.value = {
    x: viewportSize.value.w / 2 - contentX * zoom.value,
    y: viewportSize.value.h / 2 - contentY * zoom.value,
  }
}
function startMinimapDrag(event) {
  minimapDragging = true
  jumpMinimap(event)
  event.currentTarget.setPointerCapture?.(event.pointerId)
}
function moveMinimapDrag(event) { if (minimapDragging) jumpMinimap(event) }
function handleCanvasKey(event) {
  if (dataDrag.value || dragState) return
  const movement = { ArrowLeft: [80, 0], ArrowRight: [-80, 0], ArrowUp: [0, 80], ArrowDown: [0, -80] }[event.key]
  if (movement) { event.preventDefault(); pan.value = { x: pan.value.x + movement[0], y: pan.value.y + movement[1] }; return }
  if (['+', '=', '-'].includes(event.key)) { event.preventDefault(); zoomStep(event.key === '-' ? 1 / 1.25 : 1.25) }
}
function endMinimapDrag() { minimapDragging = false }

// 点阵背景按当前相机的平移和缩放绘制在视口上。
const gridStyle = computed(() => ({
  backgroundImage: 'radial-gradient(circle, var(--md-outline-variant) 1px, transparent 1.2px)',
  backgroundSize: `${20 * zoom.value}px ${20 * zoom.value}px`,
  backgroundPosition: `${pan.value.x}px ${pan.value.y}px`,
}))
const cameraStyle = computed(() => ({
  transform: `translate(${pan.value.x}px, ${pan.value.y}px) scale(${zoom.value})`,
}))

// 鼠标悬停节点时，沿有向连线收集全部上游和下游节点并高亮执行链路。
const highlightedIds = computed(() => {
  const nodeId = hoverNodeId.value
  if (!layoutNodes.value.some(node => node.id === nodeId)) return null
  const ids = new Set([nodeId])
  const follow = (currentId, direction) => {
    for (const edge of graph.value.edges) {
      const nextId = direction === 'upstream'
        ? (edge.to === currentId ? edge.from : null)
        : (edge.from === currentId ? edge.to : null)
      if (nextId && !ids.has(nextId)) {
        ids.add(nextId)
        follow(nextId, direction)
      }
    }
  }
  follow(nodeId, 'upstream')
  follow(nodeId, 'downstream')
  return ids
})
function nodeDimmed(id) { return canvasLayout.value && highlightedIds.value ? !highlightedIds.value.has(id) : false }
function edgeDimmed(edge) { return highlightedIds.value ? !highlightedIds.value.has(edge.from) || !highlightedIds.value.has(edge.to) : false }

function conditionStructureKey(node) {
  if (!node || typeof node !== 'object') return ''
  if (node.type && !node.children && !node.events) return `leaf:${node.binding_id || node.type}`
  const children = node.children || node.events || []
  return `group:${node.op || node.type || 'any'}(${children.map(conditionStructureKey).join(',')})`
}

watch(canvasLayout, async active => {
  if (!active) { resizeObserver?.disconnect(); return }
  await nextTick()
  observeViewport()
})
watch(() => conditionStructureKey(conditionNode.value), () => {
  nodePositions.value = Object.fromEntries(Object.entries(nodePositions.value)
    .filter(([id]) => !id.startsWith('condition-')))
})

function resetLayout() {
  nodePositions.value = {}
  fitView({ readable: true })
}

function startNodeDrag(event, node) {
  if (!canvasLayout.value || dataDrag.value || dragState) return
  if (event.button !== 0) return
  event.preventDefault()
  stopCameraAnimation()
  const current = nodePositions.value[node.id] || { x: node.x, y: node.y }
  dragState = {
    id: node.id,
    startX: event.clientX,
    startY: event.clientY,
    x: current.x,
    y: current.y,
    moved: false,
    panX: pan.value.x, panY: pan.value.y,
  }
  captureGesture(event)
}
function dragNode(event) {
  if (!dragState || !matchesGesture(event)) return
  const deltaX = event.clientX - dragState.startX
  const deltaY = event.clientY - dragState.startY
  if (!dragState.moved) {
    if (Math.hypot(deltaX, deltaY) < 5) return
    dragState.moved = true
    // 开始拖动节点时关闭设置抽屉，并清除当前选中节点。
    selectedNodeId.value = null
  }
  gesturePointer = { clientX: event.clientX, clientY: event.clientY }
  scheduleEdgePan()
  nodePositions.value = {
    ...nodePositions.value,
    [dragState.id]: {
      x: Math.max(24, dragState.x + (deltaX + dragState.panX - pan.value.x) / zoom.value),
      y: Math.max(24, dragState.y + (deltaY + dragState.panY - pan.value.y) / zoom.value),
    },
  }
}
function endNodeDrag(event) {
  if (event && !matchesGesture(event)) return
  if (dragState?.moved && event?.type !== 'pointercancel') {
    // pointerup 后浏览器会补发 click，这里只屏蔽当前节点的一次点击。
    suppressNodeClickId = dragState.id
    if (suppressNodeClickTimer) clearTimeout(suppressNodeClickTimer)
    suppressNodeClickTimer = setTimeout(() => {
      suppressNodeClickId = null
      suppressNodeClickTimer = null
    }, 0)
  }
  dragState = null
  finishGesture()
}

function observeViewport() {
  resizeObserver?.disconnect()
  if (typeof ResizeObserver !== 'undefined' && viewportRef.value) {
    resizeObserver = new ResizeObserver(entries => {
      const rect = entries[0].contentRect
      viewportSize.value = { w: rect.width, h: rect.height }
      // 首次打开按可读文字的比例适配，长流程保留横向平移，工具栏可再次适配全图。
      if (!fittedOnce && rect.width) {
        fittedOnce = true
        fitView({ readable: true })
      }
    })
    resizeObserver.observe(viewportRef.value)
  }
}


onMounted(() => {
  restoreView()
  if (canvasLayout.value) observeViewport()
  window.addEventListener('pointermove', moveGesture)
  window.addEventListener('pointerup', endGesture)
  window.addEventListener('pointercancel', cancelGesture)
  window.addEventListener('blur', cancelGesture)
  window.addEventListener('keydown', trackKeyboard, true)
  window.addEventListener('pointerdown', trackPointer, true)
})
onUnmounted(() => {
  saveView()
  cancelGesture()
  window.removeEventListener('pointermove', moveGesture)
  window.removeEventListener('pointerup', endGesture)
  window.removeEventListener('pointercancel', cancelGesture)
  window.removeEventListener('blur', cancelGesture)
  window.removeEventListener('keydown', trackKeyboard, true)
  window.removeEventListener('pointerdown', trackPointer, true)
  resizeObserver?.disconnect()
  if (camAnimTimer) clearTimeout(camAnimTimer)
  if (suppressNodeClickTimer) clearTimeout(suppressNodeClickTimer)
})
function stopCameraAnimation() {
  if (camAnimTimer) clearTimeout(camAnimTimer)
  camAnim.value = false
}
function captureGesture(event) {
  captureTarget = event.currentTarget
  capturePointerId = event.pointerId
  captureTarget.setPointerCapture?.(capturePointerId)
  gesturePointer = { clientX: event.clientX, clientY: event.clientY }
}
function finishGesture() {
  if (edgePanFrame !== null) cancelAnimationFrame(edgePanFrame)
  edgePanFrame = null
  edgePanTime = null
  gesturePointer = null
  if (captureTarget?.hasPointerCapture?.(capturePointerId)) captureTarget.releasePointerCapture(capturePointerId)
  captureTarget = null
  capturePointerId = null
}
function scheduleEdgePan() {
  if (edgePanFrame === null) edgePanFrame = requestAnimationFrame(autoPan)
}
function autoPan(time) {
  edgePanFrame = null
  if (!gesturePointer || !(dataDrag.value?.moved || dragState?.moved) || !viewportRef.value) return
  const rect = viewportRef.value.getBoundingClientRect()
  const band = Math.min(56, rect.width / 4, rect.height / 4)
  if (band <= 0) return
  const velocity = (position, start, end) => position < start + band
    ? Math.min(1, Math.max(0, (start + band - position) / band)) * 600
    : position > end - band ? -Math.min(1, Math.max(0, (position - end + band) / band)) * 600 : 0
  const seconds = Math.min(32, edgePanTime === null ? 16 : time - edgePanTime) / 1000
  edgePanTime = time
  const dx = velocity(gesturePointer.clientX, rect.left, rect.right) * seconds
  const dy = velocity(gesturePointer.clientY, rect.top, rect.bottom) * seconds
  if (dx || dy) {
    pan.value = { x: pan.value.x + dx, y: pan.value.y + dy }
    if (dragState?.moved) dragNode(gesturePointer)
    if (dataDrag.value) nextTick(() => { if (dataDrag.value && gesturePointer) updateDataDropTarget(gesturePointer) })
  }
  scheduleEdgePan()
}
function matchesGesture(event) { return capturePointerId === null || event.pointerId === undefined || event.pointerId === capturePointerId }
function moveGesture(event) {
  if (capturePointerId !== null && event.pointerId !== capturePointerId) return
  if (dataDrag.value) updateDataDropTarget(event)
  if (dragState) dragNode(event)
}
function endGesture(event) {
  if (capturePointerId !== null && event.pointerId !== capturePointerId) return
  if (dataDrag.value) endDataDrag(event)
  if (dragState) endNodeDrag(event)
}
function cancelGesture() { cancelDataDrag(); endNodeDrag({ type: 'pointercancel' }); endPan() }
function trackKeyboard(event) {
  if (event.key === 'Tab') keyboardNavigation = true
  if (event.key === 'Escape' && (dataDrag.value || dragState)) { event.preventDefault(); cancelGesture() }
}
function trackPointer() { keyboardNavigation = false }
function saveView() {
  if (!activeRule) return
  let views = canvasViews.get(activeRule)
  if (!views) { views = new Map(); canvasViews.set(activeRule, views) }
  views.set(activeView, { pan: { ...pan.value }, zoom: zoom.value,
    positions: { ...nodePositions.value }, ports: { ...portExpansion.value }, fitted: fittedOnce })
}
function restoreView() {
  const saved = canvasViews.get(activeRule)?.get(activeView)
  pan.value = saved?.pan || { x: 0, y: 0 }
  zoom.value = saved?.zoom || 1
  nodePositions.value = saved?.positions || {}
  portExpansion.value = saved?.ports || {}
  fittedOnce = saved?.fitted || false
  stopCameraAnimation()
}
watch(() => [props.rule, props.viewKey], async () => {
  saveView()
  cancelGesture()
  activeRule = props.rule
  activeView = props.viewKey
  restoreView()
  await nextTick()
  if (!fittedOnce && viewportSize.value.w) { fittedOnce = true; fitView({ readable: true }) }
}, { flush: 'sync' })
defineExpose({ nodes: layoutNodes })
</script>

<template>
    <main class="node-editor-workspace editor-mode-active" :class="{ 'form-layout': !canvasLayout, 'with-inspector': canvasLayout && selectedNodeId }">
      <section class="node-canvas-panel" :aria-label="canvasLayout ? '规则节点画布' : '规则步骤'">
        <div class="node-canvas-toolbar">
          <div v-if="!graphOverride" class="node-canvas-tools">
            <button class="btn btn-text btn-sm" @click="selectNode(conditionNodeId(rule.condition, []), true)"><span class="material-symbols-outlined">bolt</span>触发条件</button>
            <button class="btn btn-text btn-sm" @click="emit('add-if')"><span class="material-symbols-outlined">call_split</span>添加 IF</button>
            <button class="btn btn-text btn-sm" @click="emit('add-assignment')"><span class="material-symbols-outlined">edit_note</span>变量赋值</button>
            <button class="btn btn-text btn-sm" @click="emit('add-wait')"><span class="material-symbols-outlined">hourglass_empty</span>等待</button>
            <button class="btn btn-text btn-sm" @click="selectNode('add-action', true)"><span class="material-symbols-outlined">add</span>添加动作</button>
          </div>
          <div v-if="canvasLayout" class="node-canvas-toolbar-side">
            <div class="node-canvas-zoom">
              <button class="icon-btn" title="缩小" @click="zoomStep(1 / 1.25)"><span class="material-symbols-outlined">remove</span></button>
              <button class="zoom-label" title="重置为 100%" @click="resetZoom">{{ Math.round(zoom * 100) }}%</button>
              <button class="icon-btn" title="放大" @click="zoomStep(1.25)"><span class="material-symbols-outlined">add</span></button>
              <button class="icon-btn" title="适应全部节点" @click="fitView"><span class="material-symbols-outlined">fit_screen</span></button>
            </div>
            <div class="node-canvas-status">
              <span>{{ layoutNodes.length }} 个节点 · {{ graphEdges.length }} 条连接</span>
              <button class="icon-btn" title="整理布局" @click="resetLayout"><span class="material-symbols-outlined">auto_fix_high</span></button>
            </div>
          </div>
        </div>
        <div ref="viewportRef" class="node-canvas-viewport" :class="{ panning: isPanning }" :style="canvasLayout ? gridStyle : null"
          @pointerdown="startPan" @pointermove="dataDrag ? updateDataDropTarget($event) : movePan($event)"
          @pointerup="dataDrag ? endDataDrag($event) : endPan()" @pointercancel="cancelGesture" @lostpointercapture="cancelGesture"
          @wheel="onCanvasWheel" tabindex="0" aria-label="规则画布，可用方向键平移，加减号缩放" @keydown.self="handleCanvasKey">
          <div class="node-canvas" :class="{ 'cam-anim': camAnim }" :style="canvasLayout ? cameraStyle : null">
            <!-- 画布和 SVG 共用逻辑像素坐标，overflow:visible 让连线绘制到容器外，设置 viewBox 会让端口错位。 -->
            <svg v-if="canvasLayout" class="node-links" aria-hidden="true">
              <defs>
                <marker id="node-link-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
                  <path d="M 0 0 L 8 4 L 0 8 z" />
                </marker>
                <marker id="node-data-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
                  <path d="M 0 0 L 8 4 L 0 8 z" />
                </marker>
              </defs>
               <g v-for="edge in graphEdges" :key="edge.id" class="node-edge" :class="{ dimmed: edgeDimmed(edge) }">
                <path class="node-link" :class="[`node-link-${edge.kind}`, { reversed: edge.reversed }]" :d="edge.d" />
                <text v-if="edge.label" class="node-link-label" :x="edge.labelX" :y="edge.labelY" text-anchor="middle">{{ edge.label }}</text>
               </g>
               <path v-if="pendingDataEdge" class="node-link node-link-data node-link-pending" :d="pendingDataEdge.d" />
            </svg>

            <button v-for="edge in graphEdges.filter(item => !graphOverride && canvasLayout && Number.isInteger(item.insertActionIndex) && item.to !== 'add-action')"
              :key="`insert-${edge.id}`" class="canvas-edge-add" :style="{ left: `${edge.labelX}px`, top: `${edge.labelY + 9}px` }"
              :title="`在第 ${edge.insertActionIndex + 1} 步插入动作`"
              @click.stop="emit('open-picker', 'action', { mode: 'action', index: edge.insertActionIndex, title: '插入动作' })">
              <span class="material-symbols-outlined">add</span>
            </button>

            <template v-for="node in layoutNodes" :key="node.id">
            <article
              class="graph-node" :class="[`graph-node-${node.kind}`, { selected: selectedNodeId === node.id, dimmed: nodeDimmed(node.id), 'data-incompatible': nodeDataIncompatible(node) }]"
              :style="canvasLayout ? { left: `${node.x}px`, top: `${node.y}px`, height: `${node.height}px` } : null"
              role="button" tabindex="0" @click="selectNode(node.id)" @keydown.enter.self.prevent="selectNode(node.id)" @keydown.space.self.prevent="selectNode(node.id)" @focus="focusNode(node)"
              @pointerenter="hoverNodeId = node.id" @pointerleave="hoverNodeId = null">
              <span v-if="canvasLayout && node.hasInput" class="node-port node-port-in"></span>
              <header class="graph-node-head" @pointerdown.stop="startNodeDrag($event, node)"
                @pointermove.stop="dragNode" @pointerup.stop="endNodeDrag" @pointercancel.stop="endNodeDrag">
                <span class="material-symbols-outlined">{{ node.icon }}</span>
                <span>{{ node.kicker }}</span>
                <span v-if="canvasLayout" class="material-symbols-outlined node-drag-handle">drag_indicator</span>
              </header>
              <div class="graph-node-body" :class="{ 'has-admin': node.admin }">
                <b>{{ node.label }}</b>
                <small :title="node.meta">{{ node.meta }}</small>
                <span v-if="node.admin" class="node-admin">管理员权限</span>
              </div>
              <div v-if="canvasLayout && node.hasDataPorts" class="graph-node-data-summary">
                <button :class="{ active: sideExpanded(node.id, 'inputs') }" :disabled="!node.dataInputs.length" @click.stop="togglePorts(node.id, 'inputs')">
                  <span class="material-symbols-outlined">input</span>{{ dataSideLabel(node, 'inputs') }}
                  <span class="material-symbols-outlined">{{ sideExpanded(node.id, 'inputs') ? 'expand_less' : 'expand_more' }}</span>
                </button>
                <button :class="{ active: sideExpanded(node.id, 'outputs') }" :disabled="!node.dataOutputs.length" @click.stop="togglePorts(node.id, 'outputs')">
                  {{ dataSideLabel(node, 'outputs') }}<span class="material-symbols-outlined">output</span>
                  <span class="material-symbols-outlined">{{ sideExpanded(node.id, 'outputs') ? 'expand_less' : 'expand_more' }}</span>
                </button>
              </div>
              <div v-if="canvasLayout && node.dataPortRows" class="graph-node-data">
                 <div class="data-port-column data-port-column-input">
                   <span v-for="port in node.visibleDataInputs" :key="port.id" class="data-port-row"
                     :class="dataInputDropState(node, port)" :data-input-node="node.id" :data-input-port="port.name"
                     :title="`${port.label} · ${typeLabel(port.type)} · ${JSON.stringify(port.type)}`">
                      <i class="data-port-dot"></i><small>{{ port.label }}</small><code class="data-type-chip">{{ typeLabel(port.type) }}</code>
                   </span>
                 </div>
                 <div class="data-port-column data-port-column-output">
                   <span v-for="port in node.visibleDataOutputs" :key="port.id" class="data-port-row" :class="{ optional: !port.required }" :title="`${port.label} · ${typeLabel(port.type)} · ${JSON.stringify(port.type)}`">
                      <small>{{ port.label }}</small><code class="data-type-chip">{{ typeLabel(port.type) }}</code><i class="data-port-dot draggable"
                       @pointerdown="startDataDrag($event, node, port)"></i>
                   </span>
                </div>
              </div>
              <footer v-if="node.kind === 'action' && !node.readOnly" class="graph-node-actions">
                <button class="icon-btn" :disabled="node.index === 0" title="提前执行" @click.stop="emit('move-action', node.index, -1, node)"><span class="material-symbols-outlined">arrow_back</span></button>
                <button class="icon-btn" :disabled="node.index === (graphOverride ? layoutNodes.filter(item => item.kind === 'action' && !item.readOnly && item.branchKey === node.branchKey).length : rule.actions.length) - 1" title="稍后执行" @click.stop="emit('move-action', node.index, 1, node)"><span class="material-symbols-outlined">arrow_forward</span></button>
                <button class="icon-btn icon-btn-danger" title="删除动作" @click.stop="emit('remove-action', node.index, node)"><span class="material-symbols-outlined">delete</span></button>
              </footer>
              <footer v-else-if="node.kind === 'failure-action' && !node.readOnly" class="graph-node-actions">
                <button class="icon-btn" :disabled="node.index === 0" title="上移" @click.stop="emit('move-failure-action', node.parentIndex, node.index, -1, node)"><span class="material-symbols-outlined">arrow_back</span></button>
                <button class="icon-btn" :disabled="node.index === (node.owner || rule.actions[node.parentIndex]).failure_actions.length - 1" title="下移" @click.stop="emit('move-failure-action', node.parentIndex, node.index, 1, node)"><span class="material-symbols-outlined">arrow_forward</span></button>
                <button class="icon-btn icon-btn-danger" title="删除补救动作" @click.stop="emit('remove-failure-action', node.parentIndex, node.index, node)"><span class="material-symbols-outlined">delete</span></button>
              </footer>
              <footer v-else-if="node.kind === 'condition'" class="graph-node-logic-actions">
                <button @click.stop="emit('add-condition-child', node, false)"><span class="material-symbols-outlined">add</span>条件</button>
                <button @click.stop="emit('add-condition-child', node, true)"><span class="material-symbols-outlined">account_tree</span>条件组</button>
              </footer>
              <span v-if="canvasLayout && node.hasOutput" class="node-port node-port-out"></span>
            </article>
            <slot v-if="!canvasLayout && selectedNodeId === node.id" name="inspector" />
            </template>
          </div>

          <div v-if="canvasLayout && layoutNodes.length" class="node-minimap" tabindex="0" role="group" aria-label="小地图，可用方向键平移" title="小地图：点击或拖动跳转" @keydown.self="handleCanvasKey"
            @pointerdown.stop="startMinimapDrag" @pointermove="moveMinimapDrag"
            @pointerup="endMinimapDrag" @pointercancel="endMinimapDrag">
            <svg :viewBox="`${minimapFrame.x} ${minimapFrame.y} ${minimapFrame.w} ${minimapFrame.h}`" preserveAspectRatio="xMidYMid meet">
              <rect v-for="node in layoutNodes" :key="node.id" class="mm-node" :class="`mm-${node.kind}`"
                :x="node.x" :y="node.y" :width="FLOW_NODE_WIDTH" :height="node.height" rx="24" />
              <rect class="mm-view" :x="minimapViewport.x" :y="minimapViewport.y"
                :width="minimapViewport.w" :height="minimapViewport.h" rx="60" />
            </svg>
          </div>
        </div>
        <div v-if="canvasLayout" class="canvas-help"><span class="material-symbols-outlined">pan_tool</span>拖空白处平移 · 从输出端口拖至输入端口绑定数据 · 实线为控制流，虚线为数据流</div>
      </section>
      <slot v-if="canvasLayout" name="inspector" />

          </main>
</template>
