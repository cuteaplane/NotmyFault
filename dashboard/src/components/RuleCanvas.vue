<script>
const canvasViews = new WeakMap()
</script>

<script setup>
import { computed, nextTick, ref, watch, onMounted, onUnmounted } from 'vue'
import { store } from '../lib/store'
import { FLOW_DATA_PORT_STEP, FLOW_DATA_PORT_Y, FLOW_DATA_SUMMARY_HEIGHT, FLOW_NODE_ADMIN_EXTRA_HEIGHT,
  FLOW_NODE_BASE_HEIGHT, FLOW_NODE_WIDTH, buildFlowGraph, conditionNodeId, routeFlowEdge } from '../lib/flowGraph'
import { buildNodeDataPorts, deriveDataEdges, typesCompatible } from '../lib/bindings'
import { buildWorkflowGraph, connectGraph, connectConditionGraph, graphId } from '../lib/workflowGraph'
import { predicateSummary, precedingSources, workflowEventChoices } from '../lib/workflowConditions'
import { typeLabel } from '../lib/valueTypes'
import { isAdmin, eventName, actionName, describeItem } from '../lib/rulePresentation'

const props = defineProps({ rule: Object, layout: { type: String, default: 'canvas' }, selectedNodeId: String, graphOverride: Object, viewKey: { type: String, default: 'main' } })
const emit = defineEmits(['update:selectedNodeId', 'select-node', 'add-if', 'add-assignment', 'add-wait', 'open-picker', 'move-action', 'remove-action', 'move-failure-action', 'remove-failure-action', 'add-condition-child', 'open-if', 'predicate-select', 'branch-add', 'branch-remove', 'operation'])
const canvasLayout = computed(() => props.layout === 'canvas')
const selectedNodeId = computed({ get: () => props.selectedNodeId, set: id => emit('update:selectedNodeId', id) })
const conditionNode = computed(() => props.rule.condition || null)
const typeCatalog = computed(() => Object.entries(store.schema.data_types?.custom || {}).map(([id, definition]) => ({ id, ...definition })))

const nodePositions = ref({})
const expandedGroups = ref(new Set())
const contextMenu = ref(null)
const controlDrag = ref(null)
function openContext(event, context) {
  event?.stopPropagation?.()
  const rect = viewportRef.value?.getBoundingClientRect()
  const anchor = event?.currentTarget?.getBoundingClientRect?.()
  const keyboard = event?.key || event?.detail === 0
  const clientX = keyboard && anchor ? anchor.left + anchor.width / 2 : event?.clientX ?? rect?.left ?? 0
  const clientY = keyboard && anchor ? anchor.top + anchor.height / 2 : event?.clientY ?? rect?.top ?? 0
  contextMenu.value = { ...context, x: Math.max(8, Math.min(clientX - (rect?.left || 0), (rect?.width || 800) - 250)), y: Math.max(8, Math.min(clientY - (rect?.top || 0), (rect?.height || 700) - 310)) }
}
function menuOperation(operation) { const context = contextMenu.value; contextMenu.value = null; emit('operation', { ...context, operation }) }
function edgeContext(edge) {
  return edge.activationOwner ? { kind: 'activation', group: edge.activationOwner, target: edge.activationTarget, edge } : { kind: edge.channel === 'data' ? 'data-edge' : edge.kind === 'condition' ? 'condition' : 'control', edge }
}
function toggleGroup(id) {
  stopCameraAnimation()
  const node = layoutNodes.value.find(node => node.source?.binding_id === id)
  const anchor = node ? { x: node.x, y: node.y } : null
  const next = new Set(expandedGroups.value); next.has(id) ? next.delete(id) : next.add(id); expandedGroups.value = next
  if (anchor) nextTick(() => { const now = targetLayoutNodes.value.find(node => node.source?.binding_id === id); if (now) pan.value = { x: pan.value.x + (anchor.x - now.x) * zoom.value, y: pan.value.y + (anchor.y - now.y) * zoom.value } })
}


const portExpansion = ref({})

const dataDrag = ref(null)

const dataDropTarget = ref(null)

let dragState = null
const connectionPositions = ref(null)
function freezeConnectionPositions() {
  connectionPositions.value = Object.fromEntries(layoutNodes.value.map(node => [node.id, { x: node.x, y: node.y, width: node.width, height: node.height, inputY: node.inputY, outputs: node.outputs, outputAnchors: node.outputAnchors }]))
}

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
  const events = workflowEventChoices(props.rule, store.schema), sources = new Map()
  const describePredicate = (predicate, owner) => {
    if (!sources.has(owner)) sources.set(owner, precedingSources(props.rule, owner, store.schema))
    return predicateSummary(predicate, sources.get(owner), store.configData.rules, events)
  }
  const controlGraph = props.graphOverride || (props.rule.workflow ? buildWorkflowGraph(props.rule, expandedGroups.value, store.schema, { eventName, actionName, describeItem, predicateSummary: describePredicate }) : buildFlowGraph({
    expandBranches: canvasLayout.value,
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
  }))
  function decorateGraph(controlGraph) {
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
      const outletRows = !node.frame && (node.kind === 'event_group' ? node.outputs : node.outputAnchors?.filter(port => port.label))
      const bodyHeight = outletRows?.length ? 90 + outletRows.length * 28 : FLOW_NODE_BASE_HEIGHT
      const dataStartY = node.frame ? node.fixedHeight - 16 - dataPortRows * FLOW_DATA_PORT_STEP : bodyHeight + contentExtraHeight
        + (hasDataPorts ? FLOW_DATA_SUMMARY_HEIGHT : 0)
      const footerHeight = !node.readOnly && ((node.kind === 'action' && !props.rule.workflow) || node.kind === 'failure-action') ? 34 : node.kind === 'condition' && !props.rule.workflow ? 40 : 0
      return {
        ...node,
        visibleDataInputs,
        visibleDataOutputs,
        hasDataPorts,
        dataPortRows,
        dataInputY: portYMap(visibleDataInputs, dataStartY),
        dataOutputY: portYMap(visibleDataOutputs, dataStartY),
        height: Math.max(node.fixedHeight || 0, (bodyHeight
          + contentExtraHeight
          + (hasDataPorts ? FLOW_DATA_SUMMARY_HEIGHT : 0)
          + dataPortRows * FLOW_DATA_PORT_STEP
          + footerHeight)),
      }
    })
    return {
      nodes,
      edges: [...controlGraph.edges.filter(edge => edge.channel !== 'data'), ...dataEdges],
    }
  }
  const decorated = decorateGraph(controlGraph)
  if (!props.rule.workflow || props.graphOverride) return decorated
  // 连线决定显示的数据行，组边界使用同一份卡片高度。
  const heights = new Map(decorated.nodes.filter(node => !node.frame).map(node => [node.id, node.height]))
  return decorateGraph(buildWorkflowGraph(props.rule, expandedGroups.value, store.schema, {
    eventName, actionName, describeItem, predicateSummary: describePredicate,
    nodeHeight: node => heights.get(node.id),
  }))
})

const targetLayoutNodes = computed(() => {
  const byId = new Map(graph.value.nodes.map(node => [node.id, node]))
  const displacement = id => {
    const saved = nodePositions.value[id], original = byId.get(id)
    return { x: saved?.dx ?? (saved?.x !== undefined && original ? saved.x - original.x : 0), y: saved?.dy ?? (saved?.y !== undefined && original ? saved.y - original.y : 0) }
  }
  const nodes = graph.value.nodes.map(node => {
    const owners = [...(node.ancestry || []).map(parent => parent.id), ...(node.groupOwner ? [`action-${node.groupOwner.binding_id}`] : [])]
    const offset = [...new Set([...owners, node.id])].reduce((total, id) => { const delta = displacement(id); return { x: total.x + delta.x, y: total.y + delta.y } }, { x: 0, y: 0 })
    return { ...node, x: node.x + offset.x, y: node.y + offset.y, ...(connectionPositions.value?.[node.id] || {}), manuallyPlaced: !!(connectionPositions.value?.[node.id] || nodePositions.value[node.id]) }
  })
  return props.rule.workflow ? nodes : resolveNodeCollisions(nodes)
})

const layoutNodes = ref([])
let layoutFrame = null
watch(targetLayoutNodes, target => {
  if (layoutFrame !== null) cancelAnimationFrame(layoutFrame)
  const previous = new Map(layoutNodes.value.map(node => [node.id, node]))
  const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  if (!previous.size || dataDrag.value || controlDrag.value || dragState || !canvasLayout.value || reduced) { layoutNodes.value = target; return }
  let start = null
  const targetIds = new Set(target.map(node => node.id))
  const leaving = [...previous.values()].filter(node => !targetIds.has(node.id)).map(node => ({ ...node, exiting: true }))
  const interpolate = (node, amount) => {
    const old = previous.get(node.id)
    if (!old) return { ...node, entering: amount < 1 }
    const oldPorts = new Map((old.outputs || old.outputAnchors || []).map(port => [port.id, port]))
    const blendPorts = ports => ports?.map(port => ({ ...port, y: oldPorts.has(port.id) ? oldPorts.get(port.id).y + (port.y - oldPorts.get(port.id).y) * amount : port.y }))
    return { ...node, x: old.x + (node.x - old.x) * amount, y: old.y + (node.y - old.y) * amount, width: (old.width || FLOW_NODE_WIDTH) + ((node.width || FLOW_NODE_WIDTH) - (old.width || FLOW_NODE_WIDTH)) * amount, height: old.height + (node.height - old.height) * amount, inputY: (old.inputY ?? 58) + ((node.inputY ?? 58) - (old.inputY ?? 58)) * amount, outputs: blendPorts(node.outputs), outputAnchors: blendPorts(node.outputAnchors) }
  }
  layoutNodes.value = [...target.map(node => interpolate(node, 0)), ...leaving]
  function frame(time) {
    start ??= time
    const t = Math.min(1, (time - start) / 260), amount = 1 - Math.pow(1 - t, 3)
    layoutNodes.value = [...target.map(node => interpolate(node, amount)), ...(t < 1 ? leaving : [])]
    if (t < 1) layoutFrame = requestAnimationFrame(frame); else layoutFrame = null
  }
  layoutFrame = requestAnimationFrame(frame)
}, { immediate: true })

const graphEdges = computed(() => {
  const nodesById = new Map(layoutNodes.value.map(node => [node.id, node]))
  return graph.value.edges.filter(edge => !nodesById.get(edge.from)?.entering && !nodesById.get(edge.to)?.entering).map(edge => routeFlowEdge(edge, nodesById)).filter(Boolean)
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
  if (node?.activationNode) { emit('select-node', `action-${node.groupOwner.binding_id}`); return }
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
const ZOOM_MIN = 0.01
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
  if (['action', 'failure-action', 'action_group'].includes(sourceNode.kind)) {
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
  if (!canvasLayout.value || dataDrag.value || controlDrag.value || dragState) return
  if (event.button !== 0) return
  event.preventDefault()
  event.stopPropagation()
  contextMenu.value = null
  stopCameraAnimation()
  if (layoutFrame !== null) { cancelAnimationFrame(layoutFrame); layoutFrame = null }
  freezeConnectionPositions()
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
  if (dataDrag.value.moved && source && sourcePort && target && canConnectDataPorts(source, sourcePort, target.node, target.port)) {
    if (target.node.kind === 'predicate') target.node.source[target.port.name] = dataReference(source, sourcePort)
    else if (target.node.source.type === 'set_variable') target.node.source.value = dataReference(source, sourcePort)
    else {
      target.node.source.params ||= {}
      target.node.source.params[target.port.name] = dataReference(source, sourcePort)
    }
  }
  const click = !dataDrag.value.moved
  dataDrag.value = null
  dataDropTarget.value = null
  connectionPositions.value = null
  finishGesture()
  if (click && source && sourcePort) openContext(event, { kind: 'data', node: source, port: sourcePort })
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
  const x1 = source.x + (source.width || FLOW_NODE_WIDTH)
  const y1 = source.y + (source.dataOutputY?.[sourcePort.name]
    ?? FLOW_DATA_PORT_Y + sourcePort.index * FLOW_DATA_PORT_STEP)
  const x2 = (dataDrag.value.clientX - rect.left - pan.value.x) / zoom.value
  const y2 = (dataDrag.value.clientY - rect.top - pan.value.y) / zoom.value
  const middle = Math.max(x1 + 40, (x1 + x2) / 2)
  return { d: `M ${x1} ${y1} C ${middle} ${y1}, ${middle} ${y2}, ${x2} ${y2}` }
})

const contentBounds = computed(() => {
  const nodes = layoutNodes.value
  if (!nodes.length) return { minX: 0, minY: 0, w: 900, h: 560 }
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
  for (const node of nodes) {
    minX = Math.min(minX, node.x)
    minY = Math.min(minY, node.y)
    maxX = Math.max(maxX, node.x + (node.width || FLOW_NODE_WIDTH))
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
  const nodeWidth = (node.width || FLOW_NODE_WIDTH) * zoom.value
  const nodeHeight = node.height * zoom.value
  let nextX = pan.value.x
  let nextY = pan.value.y
  const left = nextX + node.x * zoom.value
  const top = nextY + node.y * zoom.value

  if (left < margin || left + nodeWidth > visibleWidth - margin) {
    nextX = visibleWidth / 2 - (node.x + (node.width || FLOW_NODE_WIDTH) / 2) * zoom.value
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
  if (node && node.id !== previous?.id && canvasLayout.value && !dataDrag.value && !controlDrag.value && !dragState && pointerSelectionId !== node.id) centerOnNode(node)
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
const gridStyle = computed(() => {
  const baseSpacing = 20 * zoom.value
  const spacing = baseSpacing * 2 ** Math.max(0, Math.ceil(Math.log2(12 / baseSpacing)))
  return {
    backgroundImage: 'radial-gradient(circle, var(--md-outline-variant) 1px, transparent 1.2px)',
    backgroundSize: `${spacing}px ${spacing}px`,
    backgroundPosition: `${pan.value.x}px ${pan.value.y}px`,
  }
})
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
  if (!canvasLayout.value || dataDrag.value || controlDrag.value || dragState) return
  if (event.button !== 0) return
  event.preventDefault()
  stopCameraAnimation()
  contextMenu.value = null
  if (layoutFrame !== null) { cancelAnimationFrame(layoutFrame); layoutFrame = null }
  const current = { x: node.x, y: node.y }
  dragState = {
    id: node.id,
    startX: event.clientX,
    startY: event.clientY,
    x: current.x,
    y: current.y,
    dx: nodePositions.value[node.id]?.dx || 0,
    dy: nodePositions.value[node.id]?.dy || 0,
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
      dx: dragState.dx + Math.max(24, dragState.x + (deltaX + dragState.panX - pan.value.x) / zoom.value) - dragState.x,
      dy: dragState.dy + Math.max(24, dragState.y + (deltaY + dragState.panY - pan.value.y) / zoom.value) - dragState.y,
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
  if (layoutFrame !== null) cancelAnimationFrame(layoutFrame)
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
function startControlDrag(event, node, port) {
  if (event.button !== 0 || !props.rule.workflow || (!node.activationValue && (node.activationNode || node.kind === 'trigger')) || node.kind === 'failure-action') return
  event.preventDefault(); event.stopPropagation(); contextMenu.value = null; stopCameraAnimation()
  if (layoutFrame !== null) { cancelAnimationFrame(layoutFrame); layoutFrame = null }
  freezeConnectionPositions()
  controlDrag.value = { node, port, startX: event.clientX, startY: event.clientY, clientX: event.clientX, clientY: event.clientY, moved: false }
  captureGesture(event)
}
function updateControlDrag(event) {
  if (!controlDrag.value || !matchesGesture(event)) return
  Object.assign(controlDrag.value, { clientX: event.clientX, clientY: event.clientY, moved: controlDrag.value.moved || Math.hypot(event.clientX - controlDrag.value.startX, event.clientY - controlDrag.value.startY) >= 5 })
  gesturePointer = { clientX: event.clientX, clientY: event.clientY }; if (controlDrag.value.moved) scheduleEdgePan()
}
function endControlDrag(event) {
  const drag = controlDrag.value; if (!drag) return
  const el = document.elementFromPoint?.(event.clientX, event.clientY)?.closest?.('[data-control-input]') || event.target?.closest?.('[data-control-input]')
  const target = layoutNodes.value.find(node => node.id === el?.dataset.controlInput)
  if (drag.moved && drag.node.activationValue) {
    if (target?.activationOwner === drag.node.activationOwner && target.activationValue?.op) emit('operation', { operation: 'activation-connect', group: drag.node.activationOwner, source: drag.node.activationValue, target: target.activationValue })
    else if (target) emit('operation', { operation: 'connection-error', error: '事件只能连接到同一事件组的触发组合' })
  } else if (drag.moved && drag.node.conditionGraph) {
    const error = target?.conditionGraph === drag.node.conditionGraph ? connectConditionGraph(target.conditionGraph, drag.node.graphCondition.id, target.graphCondition.id) : target ? '条件只能连接到同一分支的条件组' : ''
    if (error) emit('operation', { operation: 'connection-error', error })
  } else if (drag.moved && target) {
    const valid = target.workflow === drag.node.workflow && !target.groupOwner && !target.conditionGraph && connectGraph(drag.node.workflow, drag.node.source.binding_id, drag.port.id, target.source.binding_id)
    if (!valid) emit('operation', { operation: 'connection-error', error: '控制连接不能跨越组边界或形成循环' })
  }
  controlDrag.value = null; connectionPositions.value = null; finishGesture()
  if (!drag.moved) openContext(event, drag.node.activationValue ? { kind: 'activation', group: drag.node.activationOwner, target: drag.node.activationValue, node: drag.node } : { kind: drag.node.conditionGraph ? 'condition' : 'control', node: drag.node, outlet: drag.port.id })
}
const pendingControlEdge = computed(() => {
  const drag = controlDrag.value, rect = viewportRef.value?.getBoundingClientRect(); if (!drag || !rect) return null
  const node = layoutNodes.value.find(node => node.id === drag.node.id), x = node.x + (node.width || FLOW_NODE_WIDTH), y = node.y + drag.port.y
  const endX = (drag.clientX - rect.left - pan.value.x) / zoom.value, endY = (drag.clientY - rect.top - pan.value.y) / zoom.value, mid = (x + endX) / 2
  return `M ${x} ${y} C ${mid} ${y}, ${mid} ${endY}, ${endX} ${endY}`
})
function autoPan(time) {
  edgePanFrame = null
  if (!gesturePointer || !(dataDrag.value?.moved || controlDrag.value?.moved || dragState?.moved) || !viewportRef.value) return
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
  if (controlDrag.value) updateControlDrag(event)
  if (dragState) dragNode(event)
}
function endGesture(event) {
  if (capturePointerId !== null && event.pointerId !== capturePointerId) return
  if (dataDrag.value) endDataDrag(event)
  if (controlDrag.value) endControlDrag(event)
  if (dragState) endNodeDrag(event)
}
function cancelGesture() { controlDrag.value = null; contextMenu.value = null; cancelDataDrag(); endNodeDrag({ type: 'pointercancel' }); endPan() }
function trackKeyboard(event) {
  if (event.key === 'Tab') keyboardNavigation = true
  if (event.key === 'Escape' && (dataDrag.value || controlDrag.value || contextMenu.value || dragState)) { event.preventDefault(); cancelGesture() }
}
function trackPointer() { keyboardNavigation = false }
function saveView() {
  if (!activeRule) return
  let views = canvasViews.get(activeRule)
  if (!views) { views = new Map(); canvasViews.set(activeRule, views) }
  views.set(activeView, { pan: { ...pan.value }, zoom: zoom.value,
    positions: { ...nodePositions.value }, ports: { ...portExpansion.value }, expanded: [...expandedGroups.value], fitted: fittedOnce })
}
function restoreView() {
  const saved = canvasViews.get(activeRule)?.get(activeView)
  pan.value = saved?.pan || { x: 0, y: 0 }
  zoom.value = saved?.zoom || 1
  nodePositions.value = saved?.positions || {}
  portExpansion.value = saved?.ports || {}
  expandedGroups.value = new Set(saved?.expanded || [])
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
defineExpose({ nodes: layoutNodes, toggleGroup, openContext })
</script>

<template>
    <main class="node-editor-workspace editor-mode-active" :class="{ 'form-layout': !canvasLayout, 'with-inspector': canvasLayout && selectedNodeId }">
      <section class="node-canvas-panel" :aria-label="canvasLayout ? '规则节点画布' : '规则步骤'">
        <div class="node-canvas-toolbar">
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
          @pointerdown="contextMenu = null; startPan($event)" @pointermove="dataDrag ? updateDataDropTarget($event) : movePan($event)"
          @pointerup="dataDrag ? endDataDrag($event) : endPan()" @pointercancel="cancelGesture" @lostpointercapture="dataDrag || controlDrag || dragState ? cancelGesture() : null"
          @wheel="onCanvasWheel" tabindex="0" aria-label="规则画布，可用方向键平移，加减号缩放" @keydown.self="handleCanvasKey">
          <div class="node-canvas" :class="{ 'cam-anim': camAnim }" :style="canvasLayout ? cameraStyle : null">
            <!-- 画布和 SVG 共用逻辑像素坐标，overflow:visible 让连线绘制到容器外，设置 viewBox 会让端口错位。 -->
            <svg v-if="canvasLayout" class="node-links" role="group" aria-label="流程连线">
              <defs>
                <marker id="node-link-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
                  <path d="M 0 0 L 8 4 L 0 8 z" />
                </marker>
                <marker id="node-data-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
                  <path d="M 0 0 L 8 4 L 0 8 z" />
                </marker>
              </defs>
               <g v-for="edge in graphEdges" :key="edge.id" class="node-edge" :class="{ dimmed: edgeDimmed(edge) }">
                <path v-if="!rule.workflow || edge.rawEdge || edge.channel === 'data' || edge.activationOwner" class="node-link-hit" :d="edge.d" role="button" tabindex="0" :aria-label="`${edge.channel === 'data' ? '数据' : edge.kind === 'condition' ? '条件' : '控制'}连线操作`" @pointerdown.stop @click.stop="openContext($event, edgeContext(edge))" @keydown.enter.prevent.stop="openContext($event, edgeContext(edge))" @keydown.space.prevent.stop="openContext($event, edgeContext(edge))" />
                <path class="node-link" :class="[`node-link-${edge.kind}`, { reversed: edge.reversed }]" :d="edge.d" />
                <text v-if="edge.label" class="node-link-label" :x="edge.labelX" :y="edge.labelY" text-anchor="middle">{{ edge.label }}</text>
               </g>
               <g v-for="node in layoutNodes.filter(node => node.frame && node.kind === 'event_group')" :key="`outlets-${node.id}`">
                 <text v-for="port in node.outputs" :key="port.id" class="node-link-label" :x="node.x + node.width - 14" :y="node.y + port.y - 10" text-anchor="end">{{ port.label }}</text>
               </g>
               <path v-if="pendingControlEdge" class="node-link node-link-pending" :d="pendingControlEdge" />
               <path v-if="pendingDataEdge" class="node-link node-link-data node-link-pending" :d="pendingDataEdge.d" />
            </svg>

            <template v-for="node in layoutNodes" :key="node.id">
            <article
              class="graph-node" :class="[`graph-node-${node.kind}`, { selected: selectedNodeId === node.id, dimmed: nodeDimmed(node.id), 'data-incompatible': nodeDataIncompatible(node), 'graph-group-frame': node.frame, 'layout-entering': node.entering, 'layout-exiting': node.exiting }]"
              :data-node-id="node.id"
              :style="canvasLayout ? { left: `${node.x}px`, top: `${node.y}px`, height: `${node.height}px`, width: `${node.width || FLOW_NODE_WIDTH}px` } : null"
              role="button" tabindex="0" @click.stop="node.kind === 'empty' ? openContext($event, { kind: 'control', node }) : selectNode(node.id)" @contextmenu.prevent.stop="openContext($event, { kind: node.conditionGraph ? 'condition' : 'control', node })" @keydown.enter.self.prevent="selectNode(node.id)" @keydown.space.self.prevent="selectNode(node.id)" @focus="focusNode(node)"
              @pointerenter="hoverNodeId = node.id" @pointerleave="hoverNodeId = null">
              <span v-if="canvasLayout && node.hasInput" class="node-port node-port-in" :style="{ top: `${(node.inputY ?? 58) - 7}px` }" :data-control-input="node.id"></span>
              <header class="graph-node-head" @pointerdown.stop="startNodeDrag($event, node)"
                @pointermove.stop="dragNode" @pointerup.stop="endNodeDrag" @pointercancel.stop="endNodeDrag">
                <span class="material-symbols-outlined">{{ node.icon }}</span>
                <span class="graph-node-head-title" :title="node.frame ? node.label : node.kicker">{{ node.frame ? node.label : node.kicker }}</span>
                <button v-if="['event_group', 'action_group'].includes(node.kind)" class="icon-btn group-toggle" :title="node.open ? '收起组' : '展开组'" @pointerdown.stop @click.stop="toggleGroup(node.source.binding_id)"><span class="material-symbols-outlined">{{ node.open ? 'unfold_less' : 'unfold_more' }}</span></button><button class="icon-btn node-context-button" title="节点操作" @pointerdown.stop @click.stop="openContext($event, { kind: node.conditionGraph ? 'condition' : 'control', node })"><span class="material-symbols-outlined">more_horiz</span></button><span v-if="canvasLayout" class="material-symbols-outlined node-drag-handle">drag_indicator</span>
              </header>
              <div v-if="!node.frame" class="graph-node-body" :class="{ 'has-admin': node.admin, 'has-outlets': node.kind === 'event_group' || node.outputAnchors?.some(port => port.label) }">
                <b>{{ node.label }}</b>
                <small :title="node.meta">{{ node.meta }}</small>
                <span v-if="node.admin" class="node-admin">管理员权限</span>
              </div>
              <div v-if="!node.frame && (node.kind === 'event_group' || node.outputAnchors?.some(port => port.label))" class="graph-node-outlets">
                <div v-for="port in (node.kind === 'event_group' ? node.outputs : node.outputAnchors)" :key="port.id" class="graph-node-outlet"><span>{{ port.label }}</span><span class="material-symbols-outlined">arrow_forward</span></div>
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
                      <small>{{ port.label }}</small><code class="data-type-chip">{{ typeLabel(port.type) }}</code><i class="data-port-dot draggable" role="button" tabindex="0" :aria-label="`${port.label}输出操作`"
                       @pointerdown="startDataDrag($event, node, port)" @keydown.enter.prevent.stop="openContext($event, { kind: 'data', node, port })" @keydown.space.prevent.stop="openContext($event, { kind: 'data', node, port })"></i>
                   </span>
                </div>
              </div>
              <footer v-if="node.kind === 'action' && !node.readOnly && !rule.workflow" class="graph-node-actions">
                <button class="icon-btn" :disabled="node.index === 0" title="提前执行" @click.stop="emit('move-action', node.index, -1, node)"><span class="material-symbols-outlined">arrow_back</span></button>
                <button class="icon-btn" :disabled="node.index === (node.items?.length ?? rule.actions.length) - 1" title="稍后执行" @click.stop="emit('move-action', node.index, 1, node)"><span class="material-symbols-outlined">arrow_forward</span></button>
                <button class="icon-btn icon-btn-danger" title="删除动作" @click.stop="emit('remove-action', node.index, node)"><span class="material-symbols-outlined">delete</span></button>
              </footer>
              <footer v-else-if="node.kind === 'failure-action' && !node.readOnly && !node.exiting" class="graph-node-actions">
                <button class="icon-btn" :disabled="node.index === 0" title="上移" @click.stop="emit('move-failure-action', node.parentIndex, node.index, -1, node)"><span class="material-symbols-outlined">arrow_back</span></button>
                <button class="icon-btn" :disabled="node.index === (node.owner || rule.actions[node.parentIndex]).failure_actions.length - 1" title="下移" @click.stop="emit('move-failure-action', node.parentIndex, node.index, 1, node)"><span class="material-symbols-outlined">arrow_forward</span></button>
                <button class="icon-btn icon-btn-danger" title="删除补救动作" @click.stop="emit('remove-failure-action', node.parentIndex, node.index, node)"><span class="material-symbols-outlined">delete</span></button>
              </footer>
              <footer v-else-if="node.kind === 'condition' && !rule.workflow" class="graph-node-logic-actions">
                <button @click.stop="emit('add-condition-child', node, false)"><span class="material-symbols-outlined">add</span>条件</button>
                <button @click.stop="emit('add-condition-child', node, true)"><span class="material-symbols-outlined">account_tree</span>条件组</button>
              </footer>
              <template v-if="canvasLayout && node.outputs"><button v-for="port in node.outputs" :key="port.id" class="node-port node-port-out control-port" :style="{ top: `${port.y - 7}px` }" :title="port.label" :aria-label="`${port.label}出口操作`" @pointerdown="startControlDrag($event, node, port)" @click.stop="$event.detail === 0 && openContext($event, node.activationValue ? { kind: 'activation', group: node.activationOwner, target: node.activationValue, node } : { kind: 'control', node, outlet: port.id })"></button></template><span v-else-if="canvasLayout && node.hasOutput" class="node-port node-port-out" role="button" tabindex="0" aria-label="出口操作" @pointerdown="node.conditionGraph ? startControlDrag($event, node, { id: 'condition', y: 58 }) : null" @click.stop="openContext($event, { kind: node.conditionGraph ? 'condition' : 'control', node })" @keydown.enter.prevent.stop="openContext($event, { kind: node.conditionGraph ? 'condition' : 'control', node })" @keydown.space.prevent.stop="openContext($event, { kind: node.conditionGraph ? 'condition' : 'control', node })"></span>
            </article>
            <slot v-if="!canvasLayout && selectedNodeId === node.id" name="inspector" />
            </template>
          </div>

          <div v-if="canvasLayout && layoutNodes.length" class="node-minimap" tabindex="0" role="group" aria-label="小地图，可用方向键平移" title="小地图：点击或拖动跳转" @keydown.self="handleCanvasKey"
            @pointerdown.stop="startMinimapDrag" @pointermove="moveMinimapDrag"
            @pointerup="endMinimapDrag" @pointercancel="endMinimapDrag">
            <svg :viewBox="`${minimapFrame.x} ${minimapFrame.y} ${minimapFrame.w} ${minimapFrame.h}`" preserveAspectRatio="xMidYMid meet">
              <rect v-for="node in layoutNodes" :key="node.id" class="mm-node" :class="`mm-${node.kind}`"
                :x="node.x" :y="node.y" :width="node.width || FLOW_NODE_WIDTH" :height="node.height" rx="24" />
              <rect class="mm-view" :x="minimapViewport.x" :y="minimapViewport.y"
                :width="minimapViewport.w" :height="minimapViewport.h" rx="60" />
            </svg>
          </div>
          <Transition name="canvas-menu"><div v-if="contextMenu" class="canvas-context-menu" role="menu" :style="{ left: `${contextMenu.x}px`, top: `${contextMenu.y}px` }" @pointerdown.stop @keydown.esc.stop="contextMenu = null">
            <template v-if="contextMenu.kind === 'activation'"><button role="menuitem" @click="menuOperation('edit-activation')"><span class="material-symbols-outlined">account_tree</span>编辑触发组合</button></template>
            <template v-else-if="['data', 'data-edge'].includes(contextMenu.kind)"><button role="menuitem" @click="menuOperation('create-variable')"><span class="material-symbols-outlined">add_box</span>创建变量并赋值</button><button role="menuitem" @click="menuOperation('assign-variable')"><span class="material-symbols-outlined">edit_note</span>给变量赋值</button><button role="menuitem" @click="menuOperation('compare')"><span class="material-symbols-outlined">rule</span>添加比较条件</button></template>
            <template v-else-if="contextMenu.kind === 'condition'"><button role="menuitem" @click="menuOperation('condition')"><span class="material-symbols-outlined">rule</span>添加比较条件</button><button v-for="op in ['all', 'any', 'not']" :key="op" role="menuitem" @click="menuOperation(op)"><span class="material-symbols-outlined">account_tree</span>{{ op === 'all' ? '组合 AND' : op === 'any' ? '组合 OR' : '取反 NOT' }}</button></template>
            <template v-else-if="contextMenu.node?.kind !== 'failure-action'"><button role="menuitem" @click="menuOperation('action')"><span class="material-symbols-outlined">play_arrow</span>添加动作</button><button role="menuitem" @click="menuOperation('event_group')"><span class="material-symbols-outlined">call_split</span>创建事件组</button><button role="menuitem" @click="menuOperation('action_group')"><span class="material-symbols-outlined">account_tree</span>创建动作组</button><button role="menuitem" @click="menuOperation('wait')"><span class="material-symbols-outlined">hourglass_empty</span>等待</button><button role="menuitem" @click="menuOperation('set_variable')"><span class="material-symbols-outlined">edit_note</span>变量赋值</button></template>
            <button v-if="contextMenu.node" role="menuitem" @click="selectNode(contextMenu.node.id); contextMenu = null"><span class="material-symbols-outlined">edit</span>编辑节点</button>
          </div></Transition>
        </div>
        <div v-if="canvasLayout" class="canvas-help"><span class="material-symbols-outlined">pan_tool</span>拖空白处平移 · 从输出端口拖至输入端口绑定数据 · 实线为控制流，虚线为数据流</div>
      </section>
      <div v-if="canvasLayout" class="inspector-width" :class="{ open: selectedNodeId }"><Transition name="node-inspector-slide"><div v-if="selectedNodeId" class="inspector-surface"><slot name="inspector" /></div></Transition></div>

          </main>
</template>
