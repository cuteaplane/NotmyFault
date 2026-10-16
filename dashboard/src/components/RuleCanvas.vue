<script>
// 画布按规则对象保存平移、缩放和展开状态。
const canvasViews = new WeakMap()
</script>

<script setup>
import { computed, nextTick, ref, watch, onMounted, onUnmounted } from 'vue'
import { store } from '../lib/store'
import { FLOW_DATA_PORT_STEP, FLOW_DATA_PORT_Y, FLOW_DATA_SUMMARY_HEIGHT, FLOW_NODE_ADMIN_EXTRA_HEIGHT,
  FLOW_NODE_BASE_HEIGHT, FLOW_NODE_WIDTH, routeFlowEdge } from '../lib/flowGraph'
import { buildNodeDataPorts, deriveDataEdges, typesCompatible } from '../lib/bindings'
import { buildWorkflowGraph, connectGraph, arrangeWorkflow, makeGroupRoom, groupTypeLabel } from '../lib/workflowGraph'
import { predicateSummary, predicateSummaryParts, precedingSources, workflowEventChoices } from '../lib/workflowConditions'
import { typeLabel } from '../lib/valueTypes'
import { isAdmin, eventName, actionName, describeItem } from '../lib/rulePresentation'

const props = defineProps({ rule: Object, selectedNodeId: String })
const emit = defineEmits(['update:selectedNodeId', 'select-node', 'delete-node', 'operation'])
const selectedNodeId = computed({ get: () => props.selectedNodeId, set: id => emit('update:selectedNodeId', id) })
const typeCatalog = computed(() => Object.entries(store.schema.data_types?.custom || {}).map(([id, definition]) => ({ id, ...definition })))

const expandedGroups = ref(new Set())
const contextMenu = ref(null)
const selection = ref(new Set())
const contextMenuRef = ref(null)
watch(contextMenu, async menu => {
  if (!menu) return
  await nextTick()
  if (contextMenu.value !== menu || !contextMenuRef.value) return
  const rect = viewportRef.value?.getBoundingClientRect()
  if (!rect) return
  menu.x = Math.max(8, -rect.left + 8, Math.min(menu.x, Math.min(rect.width, window.innerWidth - rect.left) - contextMenuRef.value.offsetWidth - 8))
  menu.y = Math.max(8, -rect.top + 8, Math.min(menu.y, Math.min(rect.height, window.innerHeight - rect.top) - contextMenuRef.value.offsetHeight - 8))
})
const selectionBox = ref(null)
const dropGroup = ref(null)
const dragFrames = ref(null)
const controlDrag = ref(null)
function openContext(event, context) {
  event?.stopPropagation?.()
  const rect = viewportRef.value?.getBoundingClientRect()
  const anchor = event?.currentTarget?.getBoundingClientRect?.()
  const keyboard = event?.key || event?.type === 'click' && event?.detail === 0
  const clientX = keyboard && anchor ? anchor.left + anchor.width / 2 : event?.clientX ?? rect?.left ?? 0
  const clientY = keyboard && anchor ? anchor.top + anchor.height / 2 : event?.clientY ?? rect?.top ?? 0
  const position = { x: (clientX - (rect?.left || 0) - pan.value.x) / zoom.value, y: (clientY - (rect?.top || 0) - pan.value.y) / zoom.value }
  const maxHeight = Math.max(120, Math.min(rect?.bottom || window.innerHeight, window.innerHeight) - Math.max(0, rect?.top || 0) - 16)
  contextMenu.value = { ids: [...selection.value], position, ...context, maxHeight, x: Math.max(8, Math.min(clientX - (rect?.left || 0), (rect?.width || 800) - 250)), y: Math.max(8, Math.min(clientY - (rect?.top || 0), (rect?.height || 700) - 310)) }
}
function menuOperation(operation, value) {
  const context = contextMenu.value
  if (!context) return
  if (['create-variable', 'assign-variable'].includes(operation) && !context.outlet) {
    const node = context.node || layoutNodes.value.find(node => node.id === context.edge?.from)
    const outlets = node?.outputs?.filter(port => port.id !== 'failure') || []
    if (outlets.length > 1) { contextMenu.value = { ...context, node, kind: 'data-outlet', dataOperation: operation, outlets }; return }
    if (outlets.length === 1) context.outlet = outlets[0].id
  }
  contextMenu.value = null
  if (operation === 'edit-condition') { selectNode(context.edge.inspectionId); return }
  if (operation === 'edit-branch') { selectBranch(context.node, context.node.outputs.find(port => port.id === context.outlet)); return }
  emit('operation', { ...context, operation, value })
  if (operation === 'group-selection') selection.value = new Set()
}
function chooseDataOutlet(outlet) { const context = contextMenu.value; contextMenu.value = { ...context, kind: 'data', outlet }; menuOperation(context.dataOperation) }
function selectBranch(node, port) { const id = `branch-${node.raw.binding_id}-${port.id}`; pointerSelectionId = id; selectNode(id) }
function edgeContext(edge) {
  return edge.activationOwner ? { kind: 'activation', group: edge.activationOwner, target: edge.activationTarget, edge } : { kind: edge.channel === 'data' ? 'data-edge' : edge.kind === 'condition' ? 'condition' : 'control', edge }
}
function activateNode(event, node) {
  contextMenu.value = null
  if (node.frame && event.target === event.currentTarget) { if (event.type === 'contextmenu') backgroundMenu(event); return }
  if (event.type === 'contextmenu' && selection.value.size > 1 && selection.value.has(node.raw?.binding_id)) { openContext(event, { kind: 'control', node }); return }
  if (event.ctrlKey && !node.gate) { const next = new Set(selection.value); next.has(node.raw.binding_id) ? next.delete(node.raw.binding_id) : next.add(node.raw.binding_id); selection.value = next; return }
  selection.value = new Set()
  if (event.type === 'contextmenu') event.currentTarget?.focus?.({ preventScroll: true })
  if (node.kind === 'empty') openContext(event, { kind: 'control', node })
  else selectNode(node.id)
}
function canDeleteNode(node) { return !!node?.raw && !node.gate && !node.exiting }

function deleteNode(node) {
  if (!node || !canDeleteNode(node)) return
  contextMenu.value = null
  emit('delete-node', node)
}
function toggleGroup(id, open) {
  stopCameraAnimation()
  const before = targetLayoutNodes.value.map(node => ({ ...node }))
  const node = layoutNodes.value.find(node => node.source?.binding_id === id)
  const anchor = node ? { x: node.x, y: node.y } : null
  const next = new Set(expandedGroups.value); (open ?? !next.has(id)) ? next.add(id) : next.delete(id); expandedGroups.value = next
  nextTick(() => {
    if (expandedGroups.value.has(id)) makeGroupRoom(props.rule.workflow, before, targetLayoutNodes.value, id)
    const now = targetLayoutNodes.value.find(node => node.raw?.binding_id === id && !node.gate)
    if (anchor && now) pan.value = { x: pan.value.x + (anchor.x - now.x) * zoom.value, y: pan.value.y + (anchor.y - now.y) * zoom.value }
    if (selectedNodeId.value && ![...graph.value.nodes, ...(graph.value.inspectionNodes || [])].some(node => node.id === selectedNodeId.value)) { pointerSelectionId = `action-${id}`; selectNode(pointerSelectionId) }
  })
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
  const controlGraph = buildWorkflowGraph(props.rule, expandedGroups.value, store.schema, { eventName, actionName, describeItem, predicateSummary: describePredicate, frozenGroups: dragFrames.value })
  function decorateGraph(controlGraph) {
    const fullNodes = controlGraph.nodes.map(node => {
      const built = buildNodeDataPorts(node, store.schema, props.rule)
      const predicateParts = node.kind === 'predicate' && !['all', 'any', 'not', 'event_occurred', 'run_status'].includes(node.source.op)
        ? predicateSummaryParts(node.source, sources.get(node.groupOwner) || precedingSources(props.rule, node.raw || node.source, store.schema)) : null
      const metaTitle = ['trigger', 'action'].includes(node.kind)
        ? describeItem(node.source, node.kind === 'trigger' ? 'trigger' : 'action', false) : node.meta
      return { ...built, predicateParts, metaTitle, dataInputs: node.dataInputs ?? built.dataInputs, dataOutputs: node.dataOutputs ?? built.dataOutputs }
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
      const bodyHeight = node.gate ? 104 : outletRows?.length ? 90 + outletRows.length * 28 : FLOW_NODE_BASE_HEIGHT
      const dataStartY = node.frame ? node.fixedHeight - 16 - dataPortRows * FLOW_DATA_PORT_STEP : bodyHeight + contentExtraHeight
        + (hasDataPorts ? FLOW_DATA_SUMMARY_HEIGHT : 0)
      const footerHeight = 0
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
      inspectionNodes: controlGraph.inspectionNodes || [],
      edges: [...controlGraph.edges.filter(edge => edge.channel !== 'data'), ...dataEdges],
    }
  }
  const decorated = decorateGraph(controlGraph)
  if (!props.rule.workflow) return decorated
  // 连线决定显示的数据行，组边界使用同一份卡片高度。
  const heights = new Map(decorated.nodes.filter(node => !node.frame).map(node => [node.id, node.height]))
  return decorateGraph(buildWorkflowGraph(props.rule, expandedGroups.value, store.schema, {
    eventName, actionName, describeItem, predicateSummary: describePredicate,
    nodeHeight: node => heights.get(node.id), frozenGroups: dragFrames.value,
  }))
})

const targetLayoutNodes = computed(() => graph.value.nodes.map(node => ({ ...node, ...(connectionPositions.value?.[node.id] || {}) })))

const layoutNodes = ref([])
let layoutFrame = null
watch(targetLayoutNodes, target => {
  if (layoutFrame !== null) cancelAnimationFrame(layoutFrame)
  const previous = new Map(layoutNodes.value.map(node => [node.id, node]))
  const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  if (!previous.size || dataDrag.value || controlDrag.value || dragState || reduced) { layoutNodes.value = target; return }
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
const inspectionNodes = computed(() => [...layoutNodes.value, ...(graph.value.inspectionNodes || [])])
const selectedGraphNode = computed(() => inspectionNodes.value.find(node => node.id === selectedNodeId.value) || null)
watch(inspectionNodes, nodes => {
  if (
    selectedNodeId.value
    && !nodes.some(node => node.id === selectedNodeId.value)
  ) selectedNodeId.value = null
})
function selectNode(id, reveal = false) {
  if (suppressNodeClickId === id) return
  pointerSelectionId = reveal ? null : id
  emit('select-node', id)
}

function focusNode(node) {
  if (keyboardNavigation && !dataDrag.value && !dragState) centerOnNode(node)
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
function logicalPoint(event) {
  const rect = viewportRef.value.getBoundingClientRect()
  return { x: (event.clientX - rect.left - pan.value.x) / zoom.value, y: (event.clientY - rect.top - pan.value.y) / zoom.value }
}
function groupAt(point, exclude = new Set()) {
  return layoutNodes.value.filter(node => node.frame && !exclude.has(node.raw.binding_id) && point.x > node.x && point.x < node.x + node.width && point.y > node.y + 44 && point.y < node.y + node.height).sort((a, b) => a.width * a.height - b.width * b.height)[0]?.raw.binding_id || null
}
function backgroundMenu(event) {
  if (!blankCanvasTarget(event.target)) return
  const position = logicalPoint(event)
  openContext(event, { kind: 'background', parent: groupAt(position), position })
}
function startPan(event) {
  if (event.button !== 0 || !blankCanvasTarget(event.target)) return
  stopCameraAnimation()
  if (event.ctrlKey) {
    const point = logicalPoint(event)
    selectionBox.value = { start: point, x: point.x, y: point.y, width: 0, height: 0, previous: new Set(selection.value) }
  } else { selection.value = new Set(); selectedNodeId.value = null; panState = { startX: event.clientX, startY: event.clientY, x: pan.value.x, y: pan.value.y, target: event.currentTarget, pointerId: event.pointerId }; isPanning.value = true }
  event.currentTarget.setPointerCapture?.(event.pointerId)
}
function blankCanvasTarget(target) {
  if (target.closest('button, .node-link-hit')) return false
  const card = target.closest('.graph-node')
  return !card || card.classList.contains('graph-group-frame') && target === card
}
function movePan(event) {
  if (selectionBox.value) {
    const point = logicalPoint(event), box = selectionBox.value
    Object.assign(box, { x: Math.min(box.start.x, point.x), y: Math.min(box.start.y, point.y), width: Math.abs(point.x - box.start.x), height: Math.abs(point.y - box.start.y) })
    const next = new Set(box.previous)
    for (const node of layoutNodes.value) {
      if (!node.raw || node.gate) continue
      const contains = box.x <= node.x && box.y <= node.y && box.x + box.width >= node.x + node.width && box.y + box.height >= node.y + node.height
      const intersects = node.x < box.x + box.width && node.x + node.width > box.x && node.y < box.y + box.height && node.y + node.height > box.y
      if (node.frame ? contains : intersects) next.add(node.raw.binding_id)
    }
    selection.value = next; return
  }
  if (panState) pan.value = { x: panState.x + event.clientX - panState.startX, y: panState.y + event.clientY - panState.startY }
}
function endPan() {
  if (panState?.target.hasPointerCapture?.(panState.pointerId)) panState.target.releasePointerCapture(panState.pointerId)
  panState = null; selectionBox.value = null; isPanning.value = false
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
  if (['action', 'action_group'].includes(sourceNode.kind)) {
    return !targetNode.readOnly && ['action', 'predicate'].includes(targetNode.kind)
      && (targetNode.availableStepIds || []).includes(sourceNode.source.binding_id)
  }
  if (sourceNode.kind !== 'trigger') return false

  return !targetNode.readOnly && ['action', 'predicate'].includes(targetNode.kind)
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
  if (dataDrag.value || controlDrag.value || dragState) return
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
  if (node && Number.isFinite(node.x) && node.id !== previous?.id && !dataDrag.value && !controlDrag.value && !dragState && pointerSelectionId !== node.id) centerOnNode(node)
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
function nodeDimmed(id) { return highlightedIds.value ? !highlightedIds.value.has(id) : false }
function edgeDimmed(edge) { return highlightedIds.value ? !highlightedIds.value.has(edge.from) || !highlightedIds.value.has(edge.to) : false }


function resetLayout() {
  arrangeWorkflow(props.rule.workflow, expandedGroups.value, new Map(layoutNodes.value.filter(node => !node.frame && !node.gate).map(node => [node.raw.binding_id, node.height])))
  nextTick(() => fitView({ readable: true }))
}

function startNodeDrag(event, node) {
  if (node.gate || dataDrag.value || controlDrag.value || dragState || event.button !== 0) return
  event.preventDefault(); stopCameraAnimation(); contextMenu.value = null
  if (layoutFrame !== null) { cancelAnimationFrame(layoutFrame); layoutFrame = null }
  const selected = selection.value.has(node.raw.binding_id) ? new Set(selection.value) : new Set([node.raw.binding_id])
  const moving = new Set(selected)
  let changed
  do { changed = false; for (const raw of props.rule.workflow.nodes) if (moving.has(raw.parent) && !moving.has(raw.binding_id)) { moving.add(raw.binding_id); changed = true } } while (changed)
  const positions = new Map(props.rule.workflow.nodes.filter(raw => moving.has(raw.binding_id)).map(raw => { const shown = layoutNodes.value.find(node => node.raw === raw && !node.gate); return [raw.binding_id, { ...raw.position || { x: shown?.x || 64, y: shown?.y || 118 } }] }))
  dragFrames.value = new Map(layoutNodes.value.filter(frame => frame.frame && !moving.has(frame.raw.binding_id)).map(frame => [frame.raw.binding_id, { x: frame.x, y: frame.y, width: frame.width, height: frame.height }]))
  const originalPositions = new Map(props.rule.workflow.nodes.filter(raw => moving.has(raw.binding_id)).map(raw => [raw.binding_id, raw.position ? { ...raw.position } : null]))
  dragState = { id: node.id, node, selected, moving, positions, originalPositions, startX: event.clientX, startY: event.clientY, panX: pan.value.x, panY: pan.value.y, moved: false }
  captureGesture(event)
}
function dragNode(event) {
  if (!dragState || !matchesGesture(event)) return
  const dx = (event.clientX - dragState.startX + dragState.panX - pan.value.x) / zoom.value
  const dy = (event.clientY - dragState.startY + dragState.panY - pan.value.y) / zoom.value
  if (!dragState.moved && Math.hypot(dx, dy) < 5) return
  dragState.moved = true; selectedNodeId.value = null
  for (const raw of props.rule.workflow.nodes) { const old = dragState.positions.get(raw.binding_id); if (old) raw.position = { x: old.x + dx, y: old.y + dy } }
  const point = logicalPoint(event)
  dropGroup.value = groupAt(point, dragState.moving)
  gesturePointer = { clientX: event.clientX, clientY: event.clientY }; scheduleEdgePan()
}
function endNodeDrag(event) {
  if (event && !matchesGesture(event)) return
  if (dragState?.moved && event?.type === 'pointercancel') {
    for (const raw of props.rule.workflow.nodes) if (dragState.originalPositions.has(raw.binding_id)) {
      const position = dragState.originalPositions.get(raw.binding_id)
      if (position) raw.position = position
      else delete raw.position
    }
  }
  if (dragState?.moved && event?.type !== 'pointercancel') {
    emit('operation', { operation: 'move-group', ids: [...dragState.selected], parent: dropGroup.value })
    suppressNodeClickId = dragState.id
    if (suppressNodeClickTimer) clearTimeout(suppressNodeClickTimer)
    suppressNodeClickTimer = setTimeout(() => { suppressNodeClickId = null; suppressNodeClickTimer = null }, 0)
  }
  dragState = null; dragFrames.value = null; dropGroup.value = null; finishGesture()
}

function observeViewport() {
  resizeObserver?.disconnect()
  if (typeof ResizeObserver !== 'undefined' && viewportRef.value) {
    resizeObserver = new ResizeObserver(entries => {
      const rect = entries[0].contentRect
      viewportSize.value = { w: rect.width, h: rect.height }
      if (!fittedOnce && rect.width) { fittedOnce = true; fitView({ readable: true }) }
    })
    resizeObserver.observe(viewportRef.value)
  }
}

onMounted(() => {
  restoreView()
  observeViewport()
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
  if (event.button !== 0 || !props.rule.workflow || (!node.activationValue && node.activationNode)) return
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
  if (drag.moved && target) {
    const targetPort = el?.dataset.controlPort || target.targetPort || 'input'
    const error = connectGraph(props.rule.workflow, drag.node.raw.binding_id, drag.port.id, target.raw.binding_id, targetPort, target.gate || target.kind === 'condition-expression' || target.raw.type === 'event_group' && target.raw.group_type !== 'if' && ['trigger', 'predicate', 'condition-expression'].includes(drag.node.kind) ? 'condition' : null)
    if (error) emit('operation', { operation: 'connection-error', error })
  }
  controlDrag.value = null; connectionPositions.value = null; finishGesture()
  if (!drag.moved) openContext(event, { kind: 'control', node: drag.node, outlet: drag.port.id })
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
  if (event.key === 'Delete' && !event.repeat && !event.defaultPrevented && !dataDrag.value && !controlDrag.value && !dragState) {
    const target = event.target
    if (!viewportRef.value?.contains(target) || target.closest('input,textarea,select,[contenteditable="true"],[role="menu"]')) return
    const node = layoutNodes.value.find(node => node.id === target.closest('[data-node-id]')?.dataset.nodeId) || selectedGraphNode.value
    if (node && canDeleteNode(node)) { event.preventDefault(); deleteNode(node) }
  }
}
function trackPointer() { keyboardNavigation = false }
function saveView() {
  if (!activeRule) return
  canvasViews.set(activeRule, { pan: { ...pan.value }, zoom: zoom.value,
    ports: { ...portExpansion.value }, expanded: [...expandedGroups.value], fitted: fittedOnce })
}
function restoreView() {
  const saved = canvasViews.get(activeRule)
  pan.value = saved?.pan || { x: 0, y: 0 }
  zoom.value = saved?.zoom || 1
  portExpansion.value = saved?.ports || {}
  expandedGroups.value = new Set(saved?.expanded || [])
  fittedOnce = saved?.fitted || false
  stopCameraAnimation()
}
watch(() => props.rule, async () => {
  if (activeRule?.rule_id && activeRule.rule_id === props.rule.rule_id) { activeRule = props.rule; return }
  saveView()
  cancelGesture()
  activeRule = props.rule
  restoreView()
  await nextTick()
  if (!fittedOnce && viewportSize.value.w) { fittedOnce = true; fitView({ readable: true }) }
}, { flush: 'sync' })
defineExpose({ nodes: layoutNodes, targetNodes: targetLayoutNodes, inspectionNodes, toggleGroup, openContext })
</script>

<template>
    <main class="node-editor-workspace editor-mode-active" :class="{ 'with-inspector': selectedNodeId }">
      <section class="node-canvas-panel" aria-label="规则节点画布">
        <div class="node-canvas-toolbar">
          <div class="node-canvas-toolbar-side">
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
        <div ref="viewportRef" class="node-canvas-viewport" :class="{ panning: isPanning }" :style="gridStyle"
          @pointerdown="contextMenu = null; startPan($event)" @pointermove="dataDrag ? updateDataDropTarget($event) : movePan($event)"
          @pointerup="dataDrag ? endDataDrag($event) : endPan()" @pointercancel="cancelGesture" @lostpointercapture="dataDrag || controlDrag || dragState ? cancelGesture() : null"
          @contextmenu.prevent="backgroundMenu" @wheel="onCanvasWheel" tabindex="0" aria-label="规则画布，可用方向键平移，加减号缩放" @keydown.self="handleCanvasKey">
          <div class="node-canvas" :class="{ 'cam-anim': camAnim }" :style="cameraStyle">
            <div v-if="selectionBox" class="canvas-selection-box" :style="{ left: `${selectionBox.x}px`, top: `${selectionBox.y}px`, width: `${selectionBox.width}px`, height: `${selectionBox.height}px` }"></div>
            <!-- 画布和 SVG 共用逻辑像素坐标，overflow:visible 让连线绘制到容器外，设置 viewBox 会让端口错位。 -->
            <svg class="node-links" role="group" aria-label="流程连线">
              <defs>
                <marker id="node-link-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
                  <path d="M 0 0 L 8 4 L 0 8 z" />
                </marker>
                <marker id="node-data-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
                  <path d="M 0 0 L 8 4 L 0 8 z" />
                </marker>
              </defs>
               <g v-for="edge in graphEdges" :key="edge.id" class="node-edge" :class="{ dimmed: edgeDimmed(edge) }">
                <path v-if="!rule.workflow || edge.rawEdge || edge.channel === 'data' || edge.activationOwner || edge.conditionGraph" class="node-link-hit" :d="edge.d" role="button" tabindex="0" :aria-label="`${edge.channel === 'data' ? '数据' : edge.kind === 'condition' ? '条件' : '控制'}连线操作`" @pointerdown.stop @click.stop="openContext($event, edgeContext(edge))" @contextmenu.prevent.stop="openContext($event, edgeContext(edge))" @keydown.enter.prevent.stop="openContext($event, edgeContext(edge))" @keydown.space.prevent.stop="openContext($event, edgeContext(edge))" />
                <path class="node-link" :class="[`node-link-${edge.kind}`, { reversed: edge.reversed }]" :d="edge.d" />
                <text v-if="edge.label" class="node-link-label" :x="edge.labelX" :y="edge.labelY" text-anchor="middle">{{ edge.label }}</text>
               </g>
               <g v-for="node in layoutNodes.filter(node => node.frame && node.kind === 'event_group' && node.raw.group_type !== 'if')" :key="`outlets-${node.id}`">
                 <text v-for="port in node.outputs" :key="port.id" class="node-link-label" :x="node.x + node.width - 14" :y="node.y + port.y - 10" text-anchor="end">{{ port.label }}</text>
               </g>
               <path v-if="pendingControlEdge" class="node-link node-link-pending" :d="pendingControlEdge" />
               <path v-if="pendingDataEdge" class="node-link node-link-data node-link-pending" :d="pendingDataEdge.d" />
            </svg>

            <template v-for="node in layoutNodes" :key="node.id">
            <article
              class="graph-node" :class="[`graph-node-${node.kind}`, { selected: selectedNodeId === node.id, dimmed: nodeDimmed(node.id), 'data-incompatible': nodeDataIncompatible(node), 'graph-group-frame': node.frame, 'multi-selected': node.raw && !node.gate && selection.has(node.raw.binding_id), 'group-drop-target': node.frame && dropGroup === node.raw?.binding_id, 'layout-entering': node.entering, 'layout-exiting': node.exiting }]"
              :data-node-id="node.id"
              :style="{ left: `${node.x}px`, top: `${node.y}px`, height: `${node.height}px`, width: `${node.width || FLOW_NODE_WIDTH}px` }"
              role="button" tabindex="0" @click.stop="activateNode($event, node)" @contextmenu.prevent.stop="activateNode($event, node)" @keydown.enter.self.prevent="selectNode(node.id)" @keydown.space.self.prevent="selectNode(node.id)" @focus="focusNode(node)"
              @pointerenter="hoverNodeId = node.id" @pointerleave="hoverNodeId = null">
<button v-for="port in node.inputs || []" :key="port.id" class="node-port node-port-in control-input" :style="{ top: `${port.y - 7}px`, left: `${(port.x || 0) - 7}px` }" :data-control-input="node.id" :data-control-port="port.id" :aria-label="port.label" @pointerdown.stop @click.stop="selectBranch(node, port)" @contextmenu.prevent.stop="selectBranch(node, port)"></button>
              <template v-if="node.frame && node.raw.group_type === 'if'"><button v-for="port in node.outputs" :key="`label-${port.id}`" class="graph-branch-label" :style="{ top: `${port.y - 18}px` }" :aria-label="`设置 ${port.label} 分支`" @pointerdown.stop @click.stop="selectBranch(node, port)" @contextmenu.prevent.stop="selectBranch(node, port)">{{ port.label }}{{ port.branch.negated ? ' · NOT' : '' }}</button></template>
              <span v-if="node.hasInput" class="node-port node-port-in" :style="{ top: `${(node.inputY ?? 58) - 7}px` }" :data-control-input="node.id" :data-control-port="node.targetPort || 'input'"></span>
              <header class="graph-node-head" @pointerdown.stop="startNodeDrag($event, node)"
                @pointermove.stop="dragNode" @pointerup.stop="endNodeDrag" @pointercancel.stop="endNodeDrag">
                <span class="material-symbols-outlined">{{ node.icon }}</span>
                <button v-if="node.raw?.type === 'event_group' && !node.gate" class="graph-node-head-title group-type-control" aria-label="切换事件组类型" @pointerdown.stop @click.stop="openContext($event, { kind: 'group-type', node })">{{ node.frame ? node.label === groupTypeLabel(node.raw.group_type) ? node.label : `${node.label} · ${groupTypeLabel(node.raw.group_type)}` : `事件组 · ${groupTypeLabel(node.raw.group_type)}` }}</button><span v-else class="graph-node-head-title">{{ node.frame ? node.label : node.kicker }}</span>
                <button v-if="['event_group', 'action_group'].includes(node.kind)" class="icon-btn group-toggle" :title="node.open ? '收起组' : '展开组'" @pointerdown.stop @click.stop="toggleGroup(node.source.binding_id)" @contextmenu.prevent.stop="toggleGroup(node.source.binding_id)"><span class="material-symbols-outlined">{{ node.open ? 'unfold_less' : 'unfold_more' }}</span></button><div class="graph-node-head-actions"><button class="icon-btn node-context-button" title="节点操作" @pointerdown.stop @click.stop="openContext($event, { kind: node.conditionGraph ? 'condition' : 'control', node })" @contextmenu.prevent.stop="openContext($event, { kind: node.conditionGraph ? 'condition' : 'control', node })"><span class="material-symbols-outlined">more_horiz</span></button><button v-if="canDeleteNode(node)" class="icon-btn node-context-button icon-btn-danger" title="删除节点（Delete）" aria-label="删除节点" @pointerdown.stop @click.stop="deleteNode(node)"><span class="material-symbols-outlined">delete</span></button></div><span class="material-symbols-outlined node-drag-handle">drag_indicator</span>
              </header>
              <div v-if="!node.frame" class="graph-node-body" :class="{ 'has-admin': node.admin, 'has-outlets': node.kind === 'event_group' || node.outputAnchors?.some(port => port.label) }">
                <b :title="node.metaTitle">{{ node.predicateParts?.comparison || node.label }}</b>
                <small :title="node.metaTitle">{{ node.predicateParts?.source || node.meta }}</small>
                <span v-if="node.admin" class="node-admin">管理员权限</span>
              </div>
              <div v-if="!node.frame && (node.kind === 'event_group' || node.outputAnchors?.some(port => port.label))" class="graph-node-outlets">
                <div v-for="port in (node.kind === 'event_group' ? node.outputs : node.outputAnchors)" :key="port.id" class="graph-node-outlet"><span>{{ port.label }}</span><span class="material-symbols-outlined">arrow_forward</span></div>
              </div>
              <div v-if="node.hasDataPorts" class="graph-node-data-summary">
                <button :class="{ active: sideExpanded(node.id, 'inputs') }" :disabled="!node.dataInputs.length" @click.stop="togglePorts(node.id, 'inputs')" @contextmenu.prevent.stop="togglePorts(node.id, 'inputs')">
                  <span class="material-symbols-outlined">input</span>{{ dataSideLabel(node, 'inputs') }}
                  <span class="material-symbols-outlined">{{ sideExpanded(node.id, 'inputs') ? 'expand_less' : 'expand_more' }}</span>
                </button>
                <button :class="{ active: sideExpanded(node.id, 'outputs') }" :disabled="!node.dataOutputs.length" @click.stop="togglePorts(node.id, 'outputs')" @contextmenu.prevent.stop="togglePorts(node.id, 'outputs')">
                  {{ dataSideLabel(node, 'outputs') }}<span class="material-symbols-outlined">output</span>
                  <span class="material-symbols-outlined">{{ sideExpanded(node.id, 'outputs') ? 'expand_less' : 'expand_more' }}</span>
                </button>
              </div>
              <div v-if="node.dataPortRows" class="graph-node-data">
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
                       @pointerdown="startDataDrag($event, node, port)" @click.stop @contextmenu.prevent.stop="openContext($event, { kind: 'data', node, port })" @keydown.enter.prevent.stop="openContext($event, { kind: 'data', node, port })" @keydown.space.prevent.stop="openContext($event, { kind: 'data', node, port })"></i>
                   </span>
                </div>
              </div>
              <template v-if="node.outputs"><button v-for="port in node.outputs" :key="port.id" class="node-port node-port-out control-port" :style="{ top: `${port.y - 7}px` }" :title="port.label" :aria-label="`${port.label}出口操作`" @pointerdown="startControlDrag($event, node, port)" @contextmenu.prevent.stop="openContext($event, node.activationValue ? { kind: 'activation', group: node.activationOwner, target: node.activationValue, node } : { kind: 'control', node, outlet: port.id })" @click.stop="$event.detail === 0 && openContext($event, node.activationValue ? { kind: 'activation', group: node.activationOwner, target: node.activationValue, node } : { kind: 'control', node, outlet: port.id })"></button></template><span v-else-if="node.hasOutput" class="node-port node-port-out" role="button" tabindex="0" aria-label="出口操作" @pointerdown="node.conditionGraph ? startControlDrag($event, node, { id: 'condition', y: 58 }) : null" @click.stop="openContext($event, { kind: node.conditionGraph ? 'condition' : 'control', node })" @contextmenu.prevent.stop="openContext($event, { kind: node.conditionGraph ? 'condition' : 'control', node })" @keydown.enter.prevent.stop="openContext($event, { kind: node.conditionGraph ? 'condition' : 'control', node })" @keydown.space.prevent.stop="openContext($event, { kind: node.conditionGraph ? 'condition' : 'control', node })"></span>
            </article>
            </template>
          </div>

          <div v-if="layoutNodes.length" class="node-minimap" tabindex="0" role="group" aria-label="小地图，可用方向键平移" title="小地图：点击或拖动跳转" @keydown.self="handleCanvasKey"
            @pointerdown.stop="startMinimapDrag" @pointermove="moveMinimapDrag"
            @pointerup="endMinimapDrag" @pointercancel="endMinimapDrag">
            <svg :viewBox="`${minimapFrame.x} ${minimapFrame.y} ${minimapFrame.w} ${minimapFrame.h}`" preserveAspectRatio="xMidYMid meet">
              <rect v-for="node in layoutNodes" :key="node.id" class="mm-node" :class="`mm-${node.kind}`"
                :x="node.x" :y="node.y" :width="node.width || FLOW_NODE_WIDTH" :height="node.height" rx="24" />
              <rect class="mm-view" :x="minimapViewport.x" :y="minimapViewport.y"
                :width="minimapViewport.w" :height="minimapViewport.h" rx="60" />
            </svg>
          </div>
          <Transition name="canvas-menu"><div v-if="contextMenu" ref="contextMenuRef" class="canvas-context-menu" role="menu" :style="{ left: `${contextMenu.x}px`, top: `${contextMenu.y}px`, maxHeight: `${contextMenu.maxHeight}px`, maxWidth: `${Math.max(240, viewportSize.w - 16)}px` }" @pointerdown.stop @keydown.esc.stop="contextMenu = null">
            <template v-if="contextMenu.kind === 'group-type'"><button v-for="type in ['if', 'all', 'any', 'not']" :key="type" role="menuitem" @click="menuOperation('group-type', type)">{{ type === 'if' ? 'IF／ELIF／ELSE' : type === 'all' ? 'AND' : type === 'any' ? 'OR' : 'NOT' }}</button></template>
            <template v-else-if="contextMenu.kind === 'data-outlet'"><p class="inspector-lead">选择执行赋值的分支</p><button v-for="port in contextMenu.outlets" :key="port.id" role="menuitem" @click="chooseDataOutlet(port.id)"><span class="material-symbols-outlined">call_split</span>{{ port.label || '完成出口' }}</button></template>
            <template v-else-if="['data', 'data-edge'].includes(contextMenu.kind)"><button role="menuitem" @click="menuOperation('create-variable')"><span class="material-symbols-outlined">add_box</span>创建变量并赋值</button><button role="menuitem" @click="menuOperation('assign-variable')"><span class="material-symbols-outlined">edit_note</span>给变量赋值</button><button role="menuitem" @click="menuOperation('compare')"><span class="material-symbols-outlined">rule</span>创建比较条件</button></template>
            <template v-else>
              <template v-if="contextMenu.ids.length > 1"><button v-for="type in ['if', 'all', 'any', 'not']" :key="type" role="menuitem" @click="menuOperation('group-selection', type)"><span class="material-symbols-outlined">account_tree</span>组合 {{ type === 'if' ? 'IF／ELIF／ELSE' : type === 'all' ? 'AND' : type === 'any' ? 'OR' : 'NOT' }}</button></template>
              <button role="menuitem" @click="menuOperation('trigger')"><span class="material-symbols-outlined">bolt</span>创建触发器</button>
              <button role="menuitem" @click="menuOperation('action')"><span class="material-symbols-outlined">play_arrow</span>创建动作</button>
              <button role="menuitem" @click="menuOperation('event_group')"><span class="material-symbols-outlined">call_split</span>创建 IF 事件组</button>
              <button v-for="type in ['all', 'any', 'not']" :key="type" role="menuitem" @click="menuOperation(type)"><span class="material-symbols-outlined">account_tree</span>创建 {{ type === 'all' ? 'AND' : type === 'any' ? 'OR' : 'NOT' }} 事件组</button>
              <button role="menuitem" @click="menuOperation('condition')"><span class="material-symbols-outlined">rule</span>创建比较条件</button>
              <button role="menuitem" @click="menuOperation('action_group')"><span class="material-symbols-outlined">account_tree</span>创建动作组</button>
              <button role="menuitem" @click="menuOperation('wait')"><span class="material-symbols-outlined">hourglass_empty</span>创建等待动作</button>
              <button role="menuitem" @click="menuOperation('set_variable')"><span class="material-symbols-outlined">edit_note</span>变量赋值</button>
              <button v-if="contextMenu.node?.raw?.group_type === 'if' && contextMenu.outlet" role="menuitem" @click="menuOperation('edit-branch')"><span class="material-symbols-outlined">edit</span>设置此分支</button>
              <button v-if="contextMenu.node?.raw && contextMenu.node?.branch?.else !== true && ['trigger', 'logic', 'event_group'].includes(contextMenu.node.raw.type)" role="menuitem" @click="menuOperation('negate')"><span class="material-symbols-outlined">flip</span>条件取反</button>
              <button v-if="contextMenu.edge?.rawEdge" role="menuitem" @click="menuOperation('disconnect')"><span class="material-symbols-outlined">link_off</span>断开连线</button>
              <button v-if="canDeleteNode(contextMenu.node)" role="menuitem" class="danger-text" @click="deleteNode(contextMenu.node)"><span class="material-symbols-outlined">delete</span>删除节点</button>
            </template>
          </div></Transition>
        </div>
        <div class="canvas-help"><span class="material-symbols-outlined">pan_tool</span>拖空白处平移 · 从输出端口拖至输入端口绑定数据 · 实线为控制流，虚线为数据流</div>
      </section>
      <div class="inspector-width" :class="{ open: selectedNodeId }"><Transition name="node-inspector-slide"><div v-if="selectedNodeId" class="inspector-surface"><slot name="inspector" /></div></Transition></div>

          </main>
</template>
