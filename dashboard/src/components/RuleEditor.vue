<script setup>
import { computed, ref, watch, onMounted, onUnmounted } from 'vue'
import { store } from '../lib/store'
import {
  getVisibleParamDefs,
  buildDefaultParams,
  groupTriggerKeys,
  normalizeConditionTree,
  normalizeRuleDraft,
  optLabel,
  optValue,
} from '../lib/utils'
import {
  FLOW_NODE_WIDTH,
  buildFlowGraph,
  routeFlowEdge,
} from '../lib/flowGraph'
import {
  buildBindingSources,
  createBindingId,
  ensureRuleBindingIds,
  isReference,
  outputDefs,
  regenerateBindingIds,
  typesCompatible,
} from '../lib/bindings'
import ParamInput from './ParamInput.vue'
import ConditionEditor from './ConditionEditor.vue'

const props = defineProps({ rule: Object, dirty: Boolean, testing: Boolean })
const emit = defineEmits(['back', 'delete', 'save', 'save-run'])

const newTriggerType = ref('')
const newActionType = ref('')
const newPreconditionType = ref('')
const editorMode = ref('form')
const selectedNodeId = ref('trigger')
const nodePositions = ref({})
let dragState = null
const triggerKeys = computed(() => Object.keys(store.schema.triggers).filter(
  key => store.schema.triggers[key]?.platform_compatible !== false
))
const triggerGroups = computed(() => groupTriggerKeys(triggerKeys.value))
const actionKeys = computed(() => Object.keys(store.schema.actions).filter(
  key => store.schema.actions[key]?.platform_compatible !== false
))
const preconditionKeys = computed(() => actionKeys.value.filter(
  key => store.schema.actions[key]?.precondition_api === 'context-v1'
))
const isCondition = computed(() => (
  !!props.rule.condition
  && typeof props.rule.condition === 'object'
  && !Array.isArray(props.rule.condition)
))
normalizeRuleDraft(props.rule)
ensureRuleBindingIds(props.rule)
if (props.rule.condition) selectedNodeId.value = 'condition-root'
const conditionNode = computed(() => props.rule.condition ? normalizeConditionTree(props.rule.condition) : null)
const isAdmin = (meta) => !!(meta?.permissions || []).includes('admin')
const eventParams = (event) => getVisibleParamDefs(store.schema.triggers[event.type], event.params)
const actionParams = (action) => getVisibleParamDefs(store.schema.actions[action.type], action.params)
const eventName = (event) => store.schema.triggers[event?.type]?.name || event?.type || '未选择触发器'
const actionName = (action) => store.schema.actions[action?.type]?.name || action?.type || '未选择动作'
function formatParamValue(value, def) {
  if (isReference(value)) return '运行数据'
  if (def?.type === 'password' || def?.type === 'secret') return value ? '已设置' : ''
  if (def?.type === 'select') {
    const option = (def.options || []).find(item => String(optValue(item)) === String(value))
    if (option !== undefined) return optLabel(option)
  }
  if (Array.isArray(value)) return value.join(', ')
  if (value && typeof value === 'object') return JSON.stringify(value)
  if (typeof value === 'boolean') return value ? '是' : '否'
  return String(value ?? '')
}
function describeItem(item, kind) {
  if (!item) return '需要配置'
  const meta = kind === 'trigger' ? store.schema.triggers[item.type] : store.schema.actions[item.type]
  const params = item.params || {}
  const details = getVisibleParamDefs(meta, params)
    .map(def => [def.label || def.name, formatParamValue(params[def.name], def)])
    .filter(([, value]) => value !== '')
    .slice(0, 2)
    .map(([label, value]) => `${label}: ${value}`)
  const text = details.join(' · ') || '无额外参数'
  return text.length > 54 ? `${text.slice(0, 52)}…` : text
}
const graph = computed(() => buildFlowGraph({
  rule: props.rule,
  condition: isCondition.value ? conditionNode.value : null,
  eventName,
  actionName,
  describeItem,
  isAdmin: (item, kind) => isAdmin(
    kind === 'trigger' ? store.schema.triggers[item?.type] : store.schema.actions[item?.type],
  ),
}))
const layoutNodes = computed(() => graph.value.nodes.map(node => ({
  ...node,
  ...(nodePositions.value[node.id] || {}),
})))
const graphEdges = computed(() => {
  const nodesById = new Map(layoutNodes.value.map(node => [node.id, node]))
  return graph.value.edges.map(edge => routeFlowEdge(edge, nodesById)).filter(Boolean)
})
const selectedGraphNode = computed(() => layoutNodes.value.find(node => node.id === selectedNodeId.value) || null)
const selectedKind = computed(() => {
  if (selectedNodeId.value === 'add-precondition') return 'add-precondition'
  return selectedGraphNode.value?.kind || 'trigger'
})
const selectedIndex = computed(() => selectedGraphNode.value?.index ?? -1)
const selectedAction = computed(() => selectedKind.value === 'action' ? props.rule.actions?.[selectedIndex.value] : null)
const selectedPrecondition = computed(() => selectedKind.value === 'precondition' ? props.rule.preconditions?.[selectedIndex.value] : null)
const selectedConditionNode = computed(() => (
  ['condition', 'trigger'].includes(selectedKind.value) && selectedGraphNode.value?.path
    ? selectedGraphNode.value.source
    : null
))
const selectedConditionPath = computed(() => selectedGraphNode.value?.path || [])
const selectedConditionParent = computed(() => {
  const path = selectedConditionPath.value
  if (!path.length || !conditionNode.value) return null
  let parent = conditionNode.value
  for (const index of path.slice(0, -1)) parent = parent?.children?.[index]
  return parent
})

const validationIssues = computed(() => {
  const issues = []
  function validateCondition(node, path = '触发条件') {
    if (!node || typeof node !== 'object') { issues.push(`${path}格式无效`); return }
    const leaf = !!node.type && !node.children && !node.events
    if (leaf) {
      if (
        !store.schema.triggers[node.type]
        || store.schema.triggers[node.type]?.platform_compatible === false
      ) issues.push(`${path}引用了当前系统不可用的触发器`)
      if (node.params != null && (typeof node.params !== 'object' || Array.isArray(node.params))) issues.push(`${path}参数格式无效`)
      return
    }
    if (!['any', 'all'].includes(node.op)) issues.push(`${path}的组合方式无效`)
    if (!Array.isArray(node.children) || !node.children.length) {
      issues.push(`${path}组不能为空`)
      return
    }
    if ('within_seconds' in node) {
      const seconds = Number(node.within_seconds)
      if (!Number.isFinite(seconds) || seconds <= 0) issues.push(`${path}的时间窗口必须大于 0`)
    }
    node.children.forEach((child, index) => validateCondition(child, `${path} ${index + 1}`))
  }
  if (!String(props.rule.name || '').trim()) issues.push('请填写规则名称')
  if (props.rule.event && props.rule.condition) issues.push('单个触发条件和组合条件不能同时存在')
  if (!props.rule.event && !props.rule.condition) issues.push('请选择至少一个触发条件')
  if (props.rule.event) validateCondition(props.rule.event)
  if (props.rule.condition) validateCondition(conditionNode.value)
  const actions = Array.isArray(props.rule.actions) ? props.rule.actions : []
  if (!Array.isArray(props.rule.actions)) issues.push('动作列表格式无效')
  if (!actions.length) issues.push('请添加至少一个执行动作')
  actions.forEach((action, index) => {
    if (
      !store.schema.actions[action?.type]
      || store.schema.actions[action?.type]?.platform_compatible === false
    ) issues.push(`动作 ${index + 1} 引用了当前系统不可用的插件`)
    if (action?.params != null && (typeof action.params !== 'object' || Array.isArray(action.params))) issues.push(`动作 ${index + 1} 参数格式无效`)
    const sources = actionBindingSources(index)
    for (const def of actionParams(action)) {
      const value = action?.params?.[def.name]
      if (!isReference(value)) continue
      const source = sources.find(item => JSON.stringify(item.value) === JSON.stringify(value))
      if (!source) issues.push(`动作 ${index + 1} 的“${def.label || def.name}”引用了不可用数据`)
      else if (!typesCompatible(source.type, def.type)) {
        issues.push(`动作 ${index + 1} 的“${def.label || def.name}”数据类型不兼容`)
      }
    }
  })
  const preconditions = props.rule.preconditions == null
    ? []
    : Array.isArray(props.rule.preconditions) ? props.rule.preconditions : null
  if (preconditions === null) issues.push('开始前确认列表格式无效')
  ;(preconditions || []).forEach((item, index) => {
    if (!preconditionKeys.value.includes(item?.type)) issues.push(`开始前确认 ${index + 1} 不可用`)
    if (item?.params != null && (typeof item.params !== 'object' || Array.isArray(item.params))) issues.push(`开始前确认 ${index + 1} 参数格式无效`)
  })
  return issues
})

function setSingleTrigger(type) {
  if (!type) return
  props.rule.event = {
    binding_id: createBindingId('trigger'),
    type,
    params: buildDefaultParams(store.schema.triggers[type]),
  }
  delete props.rule.condition
  newTriggerType.value = ''
  selectedNodeId.value = 'trigger'
}
function changeSingleTrigger(type) { setSingleTrigger(type) }
function upgradeToConditions() {
  const initial = props.rule.event
  // 保留原 binding_id：动作参数里的 $ref.scope === 'trigger' 仍指向这个节点
  props.rule.condition = {
    op: 'any',
    children: initial ? [{
      binding_id: initial.binding_id || createBindingId('trigger'),
      type: initial.type,
      params: { ...initial.params },
    }] : [],
  }
  delete props.rule.event
  selectedNodeId.value = 'condition-root'
  nodePositions.value = {}
}
function useSingleEvent() {
  function firstEvent(node) {
    for (const child of node?.children || []) {
      if (child?.type && !child.children && !child.events) return child
      const nested = firstEvent(child)
      if (nested) return nested
    }
    return null
  }
  const first = firstEvent(conditionNode.value)
  // 保留选中叶节点的 binding_id，下游 $ref 引用不随结构切换失效
  if (first) {
    props.rule.event = {
      binding_id: first.binding_id || createBindingId('trigger'),
      type: first.type,
      params: { ...first.params },
    }
  }
  else delete props.rule.event
  delete props.rule.condition
  selectedNodeId.value = 'trigger'
  nodePositions.value = {}
}
function conditionId(path) {
  return path.length ? `condition-${path.join('-')}` : 'condition-root'
}
function changeConditionTrigger(type) {
  const node = selectedConditionNode.value
  if (!node || selectedKind.value !== 'trigger') return
  node.type = type
  node.params = buildDefaultParams(store.schema.triggers[type])
}
function changeConditionOp() {
  const node = selectedConditionNode.value
  if (!node || selectedKind.value !== 'condition') return
  if (node.op !== 'all') delete node.within_seconds
}
function addConditionChild(kind) {
  const node = selectedConditionNode.value
  if (!node || selectedKind.value !== 'condition' || !triggerKeys.value.length) return
  if (!Array.isArray(node.children)) node.children = []
  const type = triggerKeys.value[0]
  const event = {
    binding_id: createBindingId('trigger'),
    type,
    params: buildDefaultParams(store.schema.triggers[type]),
  }
  node.children.push(kind === 'group' ? { op: 'any', children: [event] } : event)
  const path = [...selectedConditionPath.value, node.children.length - 1]
  selectedNodeId.value = conditionId(path)
  nodePositions.value = {}
}
function removeSelectedCondition() {
  const parent = selectedConditionParent.value
  const path = selectedConditionPath.value
  if (!parent || !path.length) return
  parent.children.splice(path[path.length - 1], 1)
  selectedNodeId.value = conditionId(path.slice(0, -1))
  nodePositions.value = {}
}
function moveSelectedCondition(offset) {
  const parent = selectedConditionParent.value
  const path = selectedConditionPath.value
  if (!parent || !path.length) return
  const index = path[path.length - 1]
  const target = index + offset
  if (target < 0 || target >= parent.children.length) return
  const [node] = parent.children.splice(index, 1)
  parent.children.splice(target, 0, node)
  selectedNodeId.value = conditionId([...path.slice(0, -1), target])
  nodePositions.value = {}
}
function duplicateSelectedCondition() {
  const parent = selectedConditionParent.value
  const path = selectedConditionPath.value
  if (!parent || !path.length) return
  const index = path[path.length - 1]
  const copy = JSON.parse(JSON.stringify(parent.children[index]))
  regenerateBindingIds(copy, 'trigger')
  parent.children.splice(index + 1, 0, copy)
  selectedNodeId.value = conditionId([...path.slice(0, -1), index + 1])
  nodePositions.value = {}
}
function addAction() {
  const type = newActionType.value || actionKeys.value[0]
  if (!type) return
  if (!Array.isArray(props.rule.actions)) props.rule.actions = []
  props.rule.actions.push({
    binding_id: createBindingId('action'),
    type,
    params: buildDefaultParams(store.schema.actions[type]),
  })
  selectedNodeId.value = `action-${props.rule.actions.at(-1).binding_id}`
  nodePositions.value = {}
  newActionType.value = ''
}
function changeAction(action, type) {
  action.type = type
  action.params = buildDefaultParams(store.schema.actions[type])
}
function removeAction(index) {
  props.rule.actions.splice(index, 1)
  selectedNodeId.value = props.rule.actions.length
    ? `action-${props.rule.actions[Math.min(index, props.rule.actions.length - 1)].binding_id}`
    : 'add-action'
  nodePositions.value = {}
}
function duplicateAction(index) {
  const source = props.rule.actions?.[index]
  if (!source) return
  const copy = JSON.parse(JSON.stringify(source))
  regenerateBindingIds(copy, 'action')
  props.rule.actions.splice(index + 1, 0, copy)
  selectedNodeId.value = `action-${copy.binding_id}`
  nodePositions.value = {}
}
function moveAction(index, offset) {
  const target = index + offset
  if (target < 0 || target >= props.rule.actions.length) return
  const [action] = props.rule.actions.splice(index, 1)
  props.rule.actions.splice(target, 0, action)
  selectedNodeId.value = `action-${action.binding_id}`
  nodePositions.value = {}
}
function addPrecondition() {
  const type = newPreconditionType.value || preconditionKeys.value[0]
  if (!type) return
  if (!Array.isArray(props.rule.preconditions)) props.rule.preconditions = []
  props.rule.preconditions.push({
    binding_id: createBindingId('precondition'),
    type,
    params: buildDefaultParams(store.schema.actions[type]),
  })
  selectedNodeId.value = `precondition-${props.rule.preconditions.at(-1).binding_id}`
  nodePositions.value = {}
  newPreconditionType.value = ''
}
function changePrecondition(item, type) {
  item.type = type
  item.params = buildDefaultParams(store.schema.actions[type])
}
function actionOutputHint(action, index) {
  const outputs = outputDefs(store.schema.actions[action.type])
  return outputs.map(output => `${action.binding_id} · ${output.label || output.name}`).join('　')
}
function actionBindingSources(index) {
  return buildBindingSources(props.rule, index, store.schema)
}
function preconditionBindingSources() {
  return buildBindingSources(props.rule, 0, store.schema, { allowSteps: false })
}
function removePrecondition(index) {
  props.rule.preconditions.splice(index, 1)
  selectedNodeId.value = props.rule.preconditions.length
    ? `precondition-${props.rule.preconditions[Math.min(index, props.rule.preconditions.length - 1)].binding_id}`
    : 'trigger'
  nodePositions.value = {}
}
function selectNode(id) {
  selectedNodeId.value = id
}

// ================================================================
// 画布相机：pan / zoom / fit / 小地图。
// 视口内是一个 0×0 的 transform 容器，节点始终使用逻辑坐标定位，
// 相机只改 translate + scale；连线 SVG 与节点同层，天然随动。
// ================================================================
const viewportRef = ref(null)
const viewportSize = ref({ w: 0, h: 0 })
const zoom = ref(1)
const pan = ref({ x: 0, y: 0 })
const isPanning = ref(false)
const camAnim = ref(false)
const hoverNodeId = ref(null)
const ZOOM_MIN = 0.25
const ZOOM_MAX = 2
let panState = null
let minimapDragging = false
let resizeObserver = null
let fittedOnce = false
let camAnimTimer = null
let formConditionLayoutKey = null

function clampZoom(value) { return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, value)) }
function animateCamera() {
  camAnim.value = true
  if (camAnimTimer) clearTimeout(camAnimTimer)
  camAnimTimer = setTimeout(() => { camAnim.value = false }, 300)
}

// 以视口内 (cx, cy) 为锚点缩放：锚点下的内容保持不动
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

// 空白处拖拽平移（节点与按钮上不触发）
function startPan(event) {
  if (event.button !== 0) return
  if (event.target.closest('.graph-node') || event.target.closest('button')) return
  panState = { startX: event.clientX, startY: event.clientY, x: pan.value.x, y: pan.value.y }
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
  panState = null
  isPanning.value = false
}

// 内容包围盒（节点 228×~120）
const contentBounds = computed(() => {
  const nodes = layoutNodes.value
  if (!nodes.length) return { minX: 0, minY: 0, w: 900, h: 560 }
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
  for (const node of nodes) {
    minX = Math.min(minX, node.x)
    minY = Math.min(minY, node.y)
    maxX = Math.max(maxX, node.x + FLOW_NODE_WIDTH)
    maxY = Math.max(maxY, node.y + 120)
  }
  return { minX, minY, maxX, maxY, w: maxX - minX, h: maxY - minY }
})

function fitView() {
  const { w, h } = viewportSize.value
  if (!w || !h) return
  const bounds = contentBounds.value
  const padding = 72
  const z = clampZoom(Math.min((w - padding * 2) / bounds.w, (h - padding * 2) / bounds.h, 1.2))
  animateCamera()
  zoom.value = z
  pan.value = {
    x: (w - bounds.w * z) / 2 - bounds.minX * z,
    y: (h - bounds.h * z) / 2 - bounds.minY * z,
  }
}

// 把节点平移到视口中心（已经在可视范围内则不动）
function centerOnNode(node) {
  const { w, h } = viewportSize.value
  if (!w || !h || !node) return
  const cx = node.x + FLOW_NODE_WIDTH / 2
  const cy = node.y + 60
  const viewX = -pan.value.x / zoom.value
  const viewY = -pan.value.y / zoom.value
  const margin = 90
  if (
    cx > viewX + margin && cx < viewX + w / zoom.value - margin
    && cy > viewY + margin && cy < viewY + h / zoom.value - margin
  ) return
  animateCamera()
  pan.value = { x: w / 2 - cx * zoom.value, y: h / 2 - cy * zoom.value }
}
// 程序化选中（工具栏、添加节点）时把目标带入视野；用户手动点击的节点本就可见
watch(selectedGraphNode, (node, previous) => {
  if (node && node.id !== previous?.id && editorMode.value === 'canvas') centerOnNode(node)
})

// 小地图：内容坐标系视图 + 当前视口框，点击/拖动跳转
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
  // preserveAspectRatio="meet" 会给比例不一致的小地图留白；点击坐标必须
  // 先映射到实际绘制区域，不能直接按整个容器换算。
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
function endMinimapDrag() { minimapDragging = false }

// 点阵背景随相机移动与缩放（画在视口上，逻辑上无限延伸）
const gridStyle = computed(() => ({
  backgroundImage: 'radial-gradient(circle, var(--md-outline-variant) 1px, transparent 1.2px)',
  backgroundSize: `${20 * zoom.value}px ${20 * zoom.value}px`,
  backgroundPosition: `${pan.value.x}px ${pan.value.y}px`,
}))
const cameraStyle = computed(() => ({
  transform: `translate(${pan.value.x}px, ${pan.value.y}px) scale(${zoom.value})`,
}))

// Hover 节点时沿有向连线收集全部上游和下游节点，追踪完整执行链路。
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

function conditionStructureKey(node) {
  if (!node || typeof node !== 'object') return ''
  if (node.type && !node.children && !node.events) return `leaf:${node.binding_id || node.type}`
  const children = node.children || node.events || []
  return `group:${node.op || node.type || 'any'}(${children.map(conditionStructureKey).join(',')})`
}

watch(editorMode, (mode, previousMode) => {
  if (mode === 'form') {
    formConditionLayoutKey = conditionStructureKey(conditionNode.value)
    return
  }
  if (mode === 'canvas' && previousMode === 'form') {
    if (formConditionLayoutKey !== conditionStructureKey(conditionNode.value)) nodePositions.value = {}
    formConditionLayoutKey = null
  }
})

function resetLayout() {
  nodePositions.value = {}
  fitView()
}
function startNodeDrag(event, node) {
  if (event.button !== 0) return
  const current = nodePositions.value[node.id] || { x: node.x, y: node.y }
  dragState = {
    id: node.id,
    startX: event.clientX,
    startY: event.clientY,
    x: current.x,
    y: current.y,
  }
  selectedNodeId.value = node.id
  event.currentTarget.setPointerCapture?.(event.pointerId)
}
function dragNode(event) {
  if (!dragState) return
  // 屏幕像素换算回逻辑坐标：除以当前缩放
  nodePositions.value = {
    ...nodePositions.value,
    [dragState.id]: {
      x: Math.max(24, dragState.x + (event.clientX - dragState.startX) / zoom.value),
      y: Math.max(24, dragState.y + (event.clientY - dragState.startY) / zoom.value),
    },
  }
}
function endNodeDrag() {
  dragState = null
}

onMounted(() => {
  if (typeof ResizeObserver !== 'undefined' && viewportRef.value) {
    resizeObserver = new ResizeObserver(entries => {
      const rect = entries[0].contentRect
      viewportSize.value = { w: rect.width, h: rect.height }
      // 首次拿到真实尺寸时自动适应全部节点（编辑器打开 / 切到画布标签）
      if (!fittedOnce && rect.width) {
        fittedOnce = true
        fitView()
      }
    })
    resizeObserver.observe(viewportRef.value)
  }
})
onUnmounted(() => {
  resizeObserver?.disconnect()
  if (camAnimTimer) clearTimeout(camAnimTimer)
})
</script>

<template>
  <section class="rule-editor-page flow-rule-editor">
    <header class="rule-editor-head flow-editor-head">
      <button class="btn btn-text" @click="emit('back')"><span class="material-symbols-outlined">arrow_back</span>全部规则</button>
      <div class="rule-editor-title">
        <input v-model="rule.name" class="rule-editor-name" placeholder="给这条规则起个名字">
        <label class="rule-folder-field"><span class="material-symbols-outlined">folder</span><input v-model="rule.folder" placeholder="未分类"></label>
      </div>
      <div class="rule-editor-actions">
        <span v-if="dirty" class="draft-state"><span></span>未保存</span>
        <button class="btn btn-text danger-text" @click="emit('delete')"><span class="material-symbols-outlined">delete</span>删除</button>
        <button class="btn btn-outlined rule-test-btn" :disabled="validationIssues.length || testing"
          title="保存当前修改，并真实执行一次规则中的动作" @click="emit('save-run')">
          <span v-if="testing" class="spinner"></span>
          <span v-else class="material-symbols-outlined">experiment</span>
          {{ testing ? '测试中…' : '测试规则' }}
        </button>
        <button class="btn btn-filled" :disabled="validationIssues.length || !dirty" @click="emit('save')"><span class="material-symbols-outlined">save</span>保存规则</button>
      </div>
    </header>

    <div class="editor-mode-bar">
      <div class="editor-mode-switch" role="tablist" aria-label="规则编辑方式">
        <button role="tab" :aria-selected="editorMode === 'form'" :class="{ active: editorMode === 'form' }" @click="editorMode = 'form'">
          <span class="material-symbols-outlined">view_agenda</span>分步编辑
        </button>
        <button role="tab" :aria-selected="editorMode === 'canvas'" :class="{ active: editorMode === 'canvas' }" @click="editorMode = 'canvas'">
          <span class="material-symbols-outlined">account_tree</span>节点画布<span class="editor-beta">试验</span>
        </button>
      </div>
      <span>{{ editorMode === 'canvas' ? '试验功能 · 修改仍会同步到规则' : '默认编辑方式' }}</span>
    </div>

    <main v-show="editorMode === 'canvas'" class="node-editor-workspace">
      <section class="node-canvas-panel" aria-label="规则节点画布">
        <div class="node-canvas-toolbar">
          <div class="node-canvas-tools">
            <button class="btn btn-text btn-sm" @click="selectNode(isCondition ? 'condition-root' : 'trigger')"><span class="material-symbols-outlined">bolt</span>触发条件</button>
            <button v-if="preconditionKeys.length" class="btn btn-text btn-sm" @click="selectNode('add-precondition')"><span class="material-symbols-outlined">verified_user</span>添加确认</button>
            <button class="btn btn-text btn-sm" @click="selectNode('add-action')"><span class="material-symbols-outlined">add</span>添加动作</button>
          </div>
          <div class="node-canvas-toolbar-side">
            <div class="node-canvas-zoom">
              <button class="icon-btn" title="缩小" @click="zoomStep(1 / 1.25)"><span class="material-symbols-outlined">remove</span></button>
              <button class="zoom-label" title="重置为 100%" @click="resetZoom">{{ Math.round(zoom * 100) }}%</button>
              <button class="icon-btn" title="放大" @click="zoomStep(1.25)"><span class="material-symbols-outlined">add</span></button>
              <button class="icon-btn" title="适应全部节点" @click="fitView"><span class="material-symbols-outlined">fit_screen</span></button>
            </div>
            <div class="node-canvas-status">
              <span>{{ layoutNodes.length }} 个节点 · {{ graphEdges.length }} 条连接</span>
              <button class="icon-btn" title="恢复自动布局" @click="resetLayout"><span class="material-symbols-outlined">auto_fix_high</span></button>
            </div>
          </div>
        </div>
        <div ref="viewportRef" class="node-canvas-viewport" :class="{ panning: isPanning }" :style="gridStyle"
          @pointerdown="startPan" @pointermove="movePan" @pointerup="endPan" @pointercancel="endPan"
          @wheel="onCanvasWheel">
          <div class="node-canvas" :class="{ 'cam-anim': camAnim }" :style="cameraStyle">
            <!--
              画布是 0×0 的 transform 容器，路径坐标和节点位置都使用逻辑像素，
              与节点保持同一坐标系。不要在这里设置 viewBox：SVG 靠
              overflow:visible 画出容器外内容，viewBox 会让连线与节点端口错位。
            -->
            <svg class="node-links" aria-hidden="true">
              <defs>
                <marker id="node-link-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
                  <path d="M 0 0 L 8 4 L 0 8 z" />
                </marker>
              </defs>
              <g v-for="edge in graphEdges" :key="edge.id" class="node-edge" :class="{ dimmed: edgeDimmed(edge) }">
                <path class="node-link" :class="[`node-link-${edge.kind}`, { reversed: edge.reversed }]" :d="edge.d" />
                <text v-if="edge.label" class="node-link-label" :x="edge.labelX" :y="edge.labelY" text-anchor="middle">{{ edge.label }}</text>
              </g>
            </svg>

            <article v-for="node in layoutNodes" :key="node.id"
              class="graph-node" :class="[`graph-node-${node.kind}`, { selected: selectedNodeId === node.id, dimmed: nodeDimmed(node.id) }]"
              :style="{ left: `${node.x}px`, top: `${node.y}px` }"
              role="button" tabindex="0" @click="selectNode(node.id)" @keydown.enter="selectNode(node.id)"
              @pointerenter="hoverNodeId = node.id" @pointerleave="hoverNodeId = null">
              <span v-if="node.hasInput" class="node-port node-port-in"></span>
              <header class="graph-node-head" @pointerdown.stop="startNodeDrag($event, node)"
                @pointermove.stop="dragNode" @pointerup.stop="endNodeDrag" @pointercancel.stop="endNodeDrag">
                <span class="material-symbols-outlined">{{ node.icon }}</span>
                <span>{{ node.kicker }}</span>
                <span class="material-symbols-outlined node-drag-handle">drag_indicator</span>
              </header>
              <div class="graph-node-body">
                <b>{{ node.label }}</b>
                <small :title="node.meta">{{ node.meta }}</small>
                <span v-if="node.admin" class="node-admin">管理员权限</span>
              </div>
              <footer v-if="node.kind === 'action'" class="graph-node-actions">
                <button class="icon-btn" :disabled="node.index === 0" title="提前执行" @click.stop="moveAction(node.index, -1)"><span class="material-symbols-outlined">arrow_back</span></button>
                <button class="icon-btn" :disabled="node.index === rule.actions.length - 1" title="稍后执行" @click.stop="moveAction(node.index, 1)"><span class="material-symbols-outlined">arrow_forward</span></button>
                <button class="icon-btn icon-btn-danger" title="删除动作" @click.stop="removeAction(node.index)"><span class="material-symbols-outlined">delete</span></button>
              </footer>
              <footer v-else-if="node.kind === 'precondition'" class="graph-node-actions">
                <span></span><span></span>
                <button class="icon-btn icon-btn-danger" title="删除确认" @click.stop="removePrecondition(node.index)"><span class="material-symbols-outlined">delete</span></button>
              </footer>
              <span v-if="node.hasOutput" class="node-port node-port-out"></span>
            </article>
          </div>

          <!-- 小地图：整图缩略 + 视口框，点击/拖动跳转 -->
          <div v-if="layoutNodes.length" class="node-minimap" title="小地图：点击或拖动跳转"
            @pointerdown.stop="startMinimapDrag" @pointermove="moveMinimapDrag"
            @pointerup="endMinimapDrag" @pointercancel="endMinimapDrag">
            <svg :viewBox="`${minimapFrame.x} ${minimapFrame.y} ${minimapFrame.w} ${minimapFrame.h}`" preserveAspectRatio="xMidYMid meet">
              <rect v-for="node in layoutNodes" :key="node.id" class="mm-node" :class="`mm-${node.kind}`"
                :x="node.x" :y="node.y" :width="FLOW_NODE_WIDTH" height="116" rx="24" />
              <rect class="mm-view" :x="minimapViewport.x" :y="minimapViewport.y"
                :width="minimapViewport.w" :height="minimapViewport.h" rx="60" />
            </svg>
          </div>
        </div>
        <div class="canvas-help"><span class="material-symbols-outlined">pan_tool</span>拖空白处平移 · 滚轮缩放 · 悬停节点可追踪上下游链路</div>
      </section>

      <aside class="node-inspector">
        <header class="node-inspector-head">
          <span class="material-symbols-outlined">
            {{ selectedKind === 'invalid' ? 'error' : selectedKind === 'condition' ? 'alt_route' : selectedKind === 'trigger' ? 'bolt' : selectedKind.includes('precondition') ? 'verified_user' : selectedKind === 'action' ? 'play_arrow' : 'add' }}
          </span>
          <div><small>节点设置</small><h2>
            {{ selectedKind === 'invalid' ? '无效条件' : selectedKind === 'condition' ? '逻辑组' : selectedKind === 'trigger' ? '触发条件' : selectedKind === 'precondition' ? '开始前确认' : selectedKind === 'action' ? `动作 ${selectedIndex + 1}` : selectedKind === 'add-precondition' ? '添加确认' : '添加动作' }}
          </h2></div>
        </header>
        <div class="node-inspector-body">
          <template v-if="selectedKind === 'invalid'">
            <p class="inspector-lead">这个条件节点格式无效，无法编辑。删除后可从所属逻辑组重新添加。</p>
            <button class="btn btn-text btn-sm danger-text" @click="removeSelectedCondition"><span class="material-symbols-outlined">delete</span>删除无效节点</button>
          </template>

          <template v-else-if="selectedKind === 'condition' && selectedConditionNode">
            <p class="inspector-lead">进入此节点的分支会按这里的逻辑汇合，再继续向右执行。</p>
            <label class="field"><span class="field-label">组合方式</span>
              <select v-model="selectedConditionNode.op" class="select" @change="changeConditionOp">
                <option value="any">任一满足（OR）</option>
                <option value="all">全部满足（AND）</option>
              </select>
            </label>
            <label v-if="selectedConditionNode.op === 'all'" class="field"><span class="field-label">完成时间窗口（秒，可选）</span>
              <input v-model.number="selectedConditionNode.within_seconds" type="number" min="1" class="text-field" placeholder="不限制">
            </label>
            <div class="inspector-add-grid">
              <button class="btn btn-tonal btn-sm" :disabled="!triggerKeys.length" @click="addConditionChild('event')"><span class="material-symbols-outlined">add</span>添加条件</button>
              <button class="btn btn-tonal btn-sm" :disabled="!triggerKeys.length" @click="addConditionChild('group')"><span class="material-symbols-outlined">account_tree</span>添加子组</button>
            </div>
            <div v-if="selectedConditionPath.length" class="inspector-action-row">
              <button class="btn btn-text btn-sm" @click="moveSelectedCondition(-1)"><span class="material-symbols-outlined">arrow_upward</span>上移</button>
              <button class="btn btn-text btn-sm" @click="moveSelectedCondition(1)"><span class="material-symbols-outlined">arrow_downward</span>下移</button>
              <button class="btn btn-text btn-sm" @click="duplicateSelectedCondition"><span class="material-symbols-outlined">content_copy</span>复制</button>
              <button class="btn btn-text btn-sm danger-text" @click="removeSelectedCondition"><span class="material-symbols-outlined">delete</span>删除</button>
            </div>
            <button v-else class="btn btn-text btn-sm inspector-switch" @click="useSingleEvent"><span class="material-symbols-outlined">filter_1</span>改为单个条件</button>
          </template>

          <template v-else-if="selectedKind === 'trigger'">
            <template v-if="isCondition && selectedConditionNode">
              <p class="inspector-lead">此事件是一个独立分支；它会连接到所属逻辑组。</p>
              <label class="field"><span class="field-label">触发方式</span>
                <select class="select" :value="selectedConditionNode.type" @change="changeConditionTrigger($event.target.value)">
                  <optgroup v-for="([group, keys]) in triggerGroups" :key="group" :label="group">
                    <option v-for="key in keys" :key="key" :value="key">{{ store.schema.triggers[key].name || key }}</option>
                  </optgroup>
                </select>
              </label>
              <div class="param-grid"><ParamInput v-for="param in eventParams(selectedConditionNode)" :key="param.name" :def="param" v-model="selectedConditionNode.params[param.name]" /></div>
              <div class="inspector-action-row">
                <button class="btn btn-text btn-sm" @click="moveSelectedCondition(-1)"><span class="material-symbols-outlined">arrow_upward</span>上移</button>
                <button class="btn btn-text btn-sm" @click="moveSelectedCondition(1)"><span class="material-symbols-outlined">arrow_downward</span>下移</button>
                <button class="btn btn-text btn-sm" @click="duplicateSelectedCondition"><span class="material-symbols-outlined">content_copy</span>复制</button>
                <button class="btn btn-text btn-sm danger-text" @click="removeSelectedCondition"><span class="material-symbols-outlined">delete</span>删除</button>
              </div>
            </template>
            <template v-else-if="rule.event">
              <label class="field"><span class="field-label">触发方式</span>
                <select class="select" :value="rule.event.type" @change="changeSingleTrigger($event.target.value)">
                  <optgroup v-for="([group, keys]) in triggerGroups" :key="group" :label="group">
                    <option v-for="key in keys" :key="key" :value="key">{{ store.schema.triggers[key].name || key }}</option>
                  </optgroup>
                </select>
              </label>
              <div class="param-grid"><ParamInput v-for="param in eventParams(rule.event)" :key="param.name" :def="param" v-model="rule.event.params[param.name]" /></div>
              <button class="btn btn-text btn-sm inspector-switch" @click="upgradeToConditions"><span class="material-symbols-outlined">alt_route</span>添加 AND / OR 条件</button>
            </template>
            <template v-else>
              <p class="inspector-lead">选择一个事件作为流程起点。</p>
              <label class="field"><span class="field-label">触发方式</span>
                <select v-model="newTriggerType" class="select">
                  <option value="" disabled>选择触发器…</option>
                  <optgroup v-for="([group, keys]) in triggerGroups" :key="group" :label="group">
                    <option v-for="key in keys" :key="key" :value="key">{{ store.schema.triggers[key].name || key }}</option>
                  </optgroup>
                </select>
              </label>
              <button class="btn btn-filled inspector-primary" :disabled="!newTriggerType" @click="setSingleTrigger(newTriggerType)">设置触发条件</button>
            </template>
          </template>

          <template v-else-if="selectedKind === 'precondition' && selectedPrecondition">
            <p class="inspector-lead">确认未通过时，引擎会稍后重试。</p>
            <label class="field"><span class="field-label">确认方式</span>
              <select class="select" :value="selectedPrecondition.type" @change="changePrecondition(selectedPrecondition, $event.target.value)">
                <option v-for="key in preconditionKeys" :key="key" :value="key">{{ store.schema.actions[key].name || key }}</option>
              </select>
            </label>
            <div class="param-grid"><ParamInput v-for="param in actionParams(selectedPrecondition)" :key="param.name" :def="param" v-model="selectedPrecondition.params[param.name]" allow-binding :binding-sources="preconditionBindingSources()" /></div>
            <button class="btn btn-text btn-sm danger-text inspector-switch" @click="removePrecondition(selectedIndex)"><span class="material-symbols-outlined">delete</span>删除确认</button>
          </template>

          <template v-else-if="selectedKind === 'action' && selectedAction">
            <label class="field"><span class="field-label">动作类型</span>
              <select class="select" :value="selectedAction.type" @change="changeAction(selectedAction, $event.target.value)">
                <option v-for="key in actionKeys" :key="key" :value="key">{{ store.schema.actions[key].name || key }}</option>
              </select>
            </label>
            <div class="param-grid"><ParamInput v-for="param in actionParams(selectedAction)" :key="param.name" :def="param" v-model="selectedAction.params[param.name]" allow-binding :binding-sources="actionBindingSources(selectedIndex)" /></div>
            <div v-if="actionOutputHint(selectedAction, selectedIndex)" class="workflow-output-hint">后续步骤可引用：<code>{{ actionOutputHint(selectedAction, selectedIndex) }}</code></div>
            <div class="inspector-action-row">
              <button class="btn btn-text btn-sm" :disabled="selectedIndex === 0" @click="moveAction(selectedIndex, -1)"><span class="material-symbols-outlined">arrow_back</span>提前</button>
              <button class="btn btn-text btn-sm" :disabled="selectedIndex === rule.actions.length - 1" @click="moveAction(selectedIndex, 1)">稍后<span class="material-symbols-outlined">arrow_forward</span></button>
              <button class="btn btn-text btn-sm" @click="duplicateAction(selectedIndex)"><span class="material-symbols-outlined">content_copy</span>复制</button>
              <button class="btn btn-text btn-sm danger-text" @click="removeAction(selectedIndex)"><span class="material-symbols-outlined">delete</span>删除</button>
            </div>
          </template>

          <template v-else-if="selectedKind === 'add-precondition'">
            <p class="inspector-lead">在执行动作前增加一项确认。</p>
            <label class="field"><span class="field-label">确认方式</span>
              <select v-model="newPreconditionType" class="select"><option value="" disabled>选择确认方式…</option><option v-for="key in preconditionKeys" :key="key" :value="key">{{ store.schema.actions[key].name || key }}</option></select>
            </label>
            <button class="btn btn-filled inspector-primary" :disabled="!newPreconditionType" @click="addPrecondition"><span class="material-symbols-outlined">add</span>添加确认</button>
          </template>

          <template v-else>
            <p class="inspector-lead">选择动作并把它接到流程末尾。</p>
            <label class="field"><span class="field-label">动作类型</span>
              <select v-model="newActionType" class="select"><option value="" disabled>选择动作…</option><option v-for="key in actionKeys" :key="key" :value="key">{{ store.schema.actions[key].name || key }}</option></select>
            </label>
            <button class="btn btn-filled inspector-primary" :disabled="!newActionType" @click="addAction"><span class="material-symbols-outlined">add</span>添加动作</button>
          </template>
        </div>
      </aside>
    </main>

    <main v-show="editorMode === 'form'" class="automation-flow classic-rule-editor">
      <section class="automation-stage stage-when">
        <div class="stage-rail"><span class="stage-node"><span class="material-symbols-outlined">bolt</span></span></div>
        <div class="stage-content">
          <header class="stage-head"><div><span class="stage-kicker">当</span><h2>什么情况会触发这条规则？</h2></div><span class="stage-required">必填</span></header>

          <ConditionEditor v-if="isCondition" :node="conditionNode" />
          <details v-else-if="rule.event" class="flow-card condition-flow-card" open>
            <summary>
              <span class="flow-card-index">1</span>
              <span class="flow-card-copy"><b>{{ eventName(rule.event) }}</b><small>唯一触发条件</small></span>
              <span v-if="isAdmin(store.schema.triggers[rule.event.type])" class="chip chip-admin">管理员</span>
              <span class="material-symbols-outlined flow-expand">expand_more</span>
            </summary>
            <div class="flow-card-body">
              <label class="field field-wide"><span class="field-label">触发方式</span>
                <select class="select" :value="rule.event.type" @change="changeSingleTrigger($event.target.value)">
                  <optgroup v-for="([group, keys]) in triggerGroups" :key="group" :label="group">
                    <option v-for="key in keys" :key="key" :value="key">{{ store.schema.triggers[key].name || key }}</option>
                  </optgroup>
                </select>
              </label>
              <div class="param-grid"><ParamInput v-for="param in eventParams(rule.event)" :key="param.name" :def="param" v-model="rule.event.params[param.name]" /></div>
            </div>
          </details>
          <div v-else class="flow-empty-card">
            <span class="material-symbols-outlined">touch_app</span>
            <div><b>选择触发方式</b><p>先说明什么时候开始执行，不默认替你选择。</p></div>
            <select v-model="newTriggerType" class="select">
              <option value="" disabled>选择触发器…</option>
              <optgroup v-for="([group, keys]) in triggerGroups" :key="group" :label="group">
                <option v-for="key in keys" :key="key" :value="key">{{ store.schema.triggers[key].name || key }}</option>
              </optgroup>
            </select>
            <button class="btn btn-filled" :disabled="!newTriggerType" @click="setSingleTrigger(newTriggerType)">添加</button>
          </div>
          <div class="flow-stage-actions">
            <button v-if="rule.event" class="btn btn-text btn-sm" @click="upgradeToConditions"><span class="material-symbols-outlined">alt_route</span>添加 AND / OR 条件</button>
            <button v-if="isCondition" class="btn btn-text btn-sm" @click="useSingleEvent"><span class="material-symbols-outlined">filter_1</span>改为单个条件</button>
          </div>
        </div>
      </section>

      <section v-if="preconditionKeys.length" class="automation-stage stage-check">
        <div class="stage-rail"><span class="stage-node"><span class="material-symbols-outlined">verified_user</span></span></div>
        <div class="stage-content">
          <header class="stage-head"><div><span class="stage-kicker">开始前确认</span><h2>执行前还需要确认什么？</h2><p>未通过时，引擎会稍后重试。</p></div><span class="stage-optional">可选</span></header>
          <details v-for="(item, index) in (rule.preconditions || [])" :key="item" class="flow-card check-flow-card">
            <summary><span class="material-symbols-outlined flow-kind-icon">verified</span><span class="flow-card-copy"><b>{{ actionName(item) }}</b><small>开始前确认</small></span><button class="icon-btn icon-btn-danger" title="移除确认" @click.prevent.stop="removePrecondition(index)"><span class="material-symbols-outlined">delete</span></button><span class="material-symbols-outlined flow-expand">expand_more</span></summary>
            <div class="flow-card-body">
              <label class="field field-wide"><span class="field-label">确认方式</span><select class="select" :value="item.type" @change="changePrecondition(item, $event.target.value)"><option v-for="key in preconditionKeys" :key="key" :value="key">{{ store.schema.actions[key].name || key }}</option></select></label>
              <div class="param-grid"><ParamInput v-for="param in actionParams(item)" :key="param.name" :def="param" v-model="item.params[param.name]" allow-binding :binding-sources="preconditionBindingSources()" /></div>
            </div>
          </details>
          <div class="flow-add-control"><select v-model="newPreconditionType" class="select"><option value="" disabled>选择确认方式…</option><option v-for="key in preconditionKeys" :key="key" :value="key">{{ store.schema.actions[key].name || key }}</option></select><button class="btn btn-tonal" @click="addPrecondition"><span class="material-symbols-outlined">add</span>添加确认</button></div>
        </div>
      </section>

      <section class="automation-stage stage-then">
        <div class="stage-rail stage-rail-last"><span class="stage-node"><span class="material-symbols-outlined">play_arrow</span></span></div>
        <div class="stage-content">
          <header class="stage-head"><div><span class="stage-kicker">然后</span><h2>按顺序执行这些动作</h2></div><span class="stage-required">必填</span></header>
          <details v-for="(action, index) in rule.actions" :key="action" class="flow-card action-flow-card" :open="rule.actions.length === 1">
            <summary>
              <span class="flow-card-index">{{ index + 1 }}</span><span class="flow-card-copy"><b>{{ actionName(action) }}</b><small>第 {{ index + 1 }} 步</small></span>
              <span v-if="isAdmin(store.schema.actions[action.type])" class="chip chip-admin">管理员</span>
              <span class="flow-card-tools"><button class="icon-btn" :disabled="index === 0" title="上移" @click.prevent.stop="moveAction(index, -1)"><span class="material-symbols-outlined">arrow_upward</span></button><button class="icon-btn" :disabled="index === rule.actions.length - 1" title="下移" @click.prevent.stop="moveAction(index, 1)"><span class="material-symbols-outlined">arrow_downward</span></button><button class="icon-btn icon-btn-danger" title="移除动作" @click.prevent.stop="removeAction(index)"><span class="material-symbols-outlined">delete</span></button></span>
              <span class="material-symbols-outlined flow-expand">expand_more</span>
            </summary>
            <div class="flow-card-body">
              <label class="field field-wide"><span class="field-label">动作类型</span><select class="select" :value="action.type" @change="changeAction(action, $event.target.value)"><option v-for="key in actionKeys" :key="key" :value="key">{{ store.schema.actions[key].name || key }}</option></select></label>
              <div class="param-grid"><ParamInput v-for="param in actionParams(action)" :key="param.name" :def="param" v-model="action.params[param.name]" allow-binding :binding-sources="actionBindingSources(index)" /></div>
              <div v-if="actionOutputHint(action, index)" class="workflow-output-hint">后续步骤可引用：<code>{{ actionOutputHint(action, index) }}</code></div>
            </div>
          </details>
          <div v-if="!rule.actions?.length" class="flow-inline-empty">还没有动作。规则触发后不会执行任何操作。</div>
          <div class="flow-add-control"><select v-model="newActionType" class="select"><option value="" disabled>选择动作…</option><option v-for="key in actionKeys" :key="key" :value="key">{{ store.schema.actions[key].name || key }}</option></select><button class="btn btn-tonal" @click="addAction"><span class="material-symbols-outlined">add</span>添加动作</button></div>
        </div>
      </section>
    </main>

    <footer class="flow-validation" :class="{ valid: !validationIssues.length }">
      <span class="material-symbols-outlined">{{ validationIssues.length ? 'error' : 'check_circle' }}</span>
      <div><b>{{ validationIssues.length ? `${validationIssues.length} 项需要处理` : '规则可以保存' }}</b><p v-if="validationIssues.length">{{ validationIssues.join(' · ') }}</p><p v-else>修改只会在保存后应用到引擎。</p></div>
    </footer>
  </section>
</template>
