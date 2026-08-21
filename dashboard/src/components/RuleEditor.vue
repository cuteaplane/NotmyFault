<script setup>
import { computed, nextTick, ref, watch, onMounted, onUnmounted } from 'vue'
import { store } from '../lib/store'
import {
  getVisibleParamDefs,
  buildDefaultParams,
  groupActionKeys,
  groupTriggerKeys,
  normalizeConditionTree,
  normalizeRuleDraft,
  optLabel,
  optValue,
} from '../lib/utils'
import {
  FLOW_DATA_PORT_STEP,
  FLOW_DATA_PORT_Y,
  FLOW_DATA_SUMMARY_HEIGHT,
  FLOW_NODE_ADMIN_EXTRA_HEIGHT,
  FLOW_NODE_BASE_HEIGHT,
  FLOW_NODE_WIDTH,
  buildFlowGraph,
  routeFlowEdge,
} from '../lib/flowGraph'
import {
  buildBindingSources,
  buildFailureBindingSources,
  buildNodeDataPorts,
  createBindingId,
  deriveDataEdges,
  ensureRuleBindingIds,
  guaranteedTriggerIds,
  isReference,
  outputDefs,
  regenerateBindingIds,
  typesCompatible,
} from '../lib/bindings'
import ParamInput from './ParamInput.vue'
import ConditionEditor from './ConditionEditor.vue'
import PluginPicker from './PluginPicker.vue'
import FolderPicker from './FolderPicker.vue'
import ActionFailureSettings from './ActionFailureSettings.vue'
import DesktopRecorderDialog from './DesktopRecorderDialog.vue'
import NaturalDraftPanel from './NaturalDraftPanel.vue'
import { validateRuleDraft } from '../lib/api'

const props = defineProps({
  rule: Object,
  dirty: Boolean,
  testing: Boolean,
  canUndo: Boolean,
  canRedo: Boolean,
  changeSummary: { type: Array, default: () => [] },
  folders: { type: Array, default: () => [] },
  initialNodeId: { type: String, default: '' },
})
const emit = defineEmits(['back', 'delete', 'save', 'save-run', 'undo', 'redo', 'ai-draft'])

const newTriggerType = ref('')
const newActionType = ref('')
const newPreconditionType = ref('')

const aiEnabled = computed(() => store.aiDrafting?.enabled)
const aiPanelOpen = ref(false)
const aiPanelRef = ref(null)
function savedEditorMode() {
  try { return localStorage.getItem('notmyfault.ruleEditorMode') === 'form' ? 'form' : 'canvas' }
  catch { return 'canvas' }
}
const editorMode = ref(savedEditorMode())
const selectedNodeId = ref(null)


const wideLayout = ref(false)
let wideLayoutMedia = null
let inspectorBeforeAi = null
function syncWideLayout(event) {
  wideLayout.value = event.matches
  if (wideLayout.value && inspectorBeforeAi && !selectedNodeId.value) {
    selectedNodeId.value = inspectorBeforeAi
    inspectorBeforeAi = null
  }
}
function setAiPanel(open) {
  if (open === aiPanelOpen.value) return
  if (open) {
    if (!wideLayout.value && selectedNodeId.value) {
      inspectorBeforeAi = selectedNodeId.value
      selectedNodeId.value = null
    }
    aiPanelOpen.value = true
  } else {
    aiPanelOpen.value = false
    if (inspectorBeforeAi && !selectedNodeId.value) selectedNodeId.value = inspectorBeforeAi
    inspectorBeforeAi = null
  }
}
function toggleAiPanel() {
  setAiPanel(!aiPanelOpen.value)
}
watch(selectedNodeId, id => {
  if (id && aiPanelOpen.value) {
    inspectorBeforeAi = null
    if (!wideLayout.value) setAiPanel(false)
  }
})
const hasSidePanels = computed(() => (
  aiPanelOpen.value || (editorMode.value === 'canvas' && !!selectedNodeId.value)
))
function onAiDraft(draft) {
  emit('ai-draft', draft)
}
function highlightNodeById(nodeId) {
  if (!nodeId) return
  selectedNodeId.value = nodeId
}
function newAiConversation() {
  aiPanelRef.value?.requestNewConversation?.()
}

const aiMenuOpen = ref(false)
function openAiSettings() {
  aiMenuOpen.value = false
  window.__nmf?.switchPage?.('settings')
}
function downloadAiConversation() {
  aiMenuOpen.value = false
  aiPanelRef.value?.downloadConversation?.()
}


const AI_PANEL_MIN_WIDTH = 420
const AI_PANEL_MAX_WIDTH = 560
const aiPanelWidth = ref(480)
let aiResizeState = null
function startAiPanelResize(event) {
  aiResizeState = { startX: event.clientX, startWidth: aiPanelWidth.value }
  document.body.classList.add('ai-panel-resizing')
  window.addEventListener('pointermove', onAiPanelResize)
  window.addEventListener('pointerup', stopAiPanelResize)
  event.preventDefault()
}
function onAiPanelResize(event) {
  if (!aiResizeState) return
  const next = aiResizeState.startWidth + (aiResizeState.startX - event.clientX)
  aiPanelWidth.value = Math.min(AI_PANEL_MAX_WIDTH, Math.max(AI_PANEL_MIN_WIDTH, next))
}
function stopAiPanelResize() {
  if (!aiResizeState) return
  aiResizeState = null
  document.body.classList.remove('ai-panel-resizing')
  window.removeEventListener('pointermove', onAiPanelResize)
  window.removeEventListener('pointerup', stopAiPanelResize)
  try { localStorage.setItem('notmyfault.aiPanelWidth', String(aiPanelWidth.value)) } catch {}
}

const aiContextRule = computed(() => {
  const rule = props.rule
  if (!rule) return null
  const nodes = 1 + (rule.preconditions?.length || 0) + (rule.actions?.length || 0)
  return { name: rule.name || '未命名规则', nodes }
})
const nodePositions = ref({})
const portExpansion = ref({})
const dataDrag = ref(null)
const dataDropTarget = ref(null)
const picker = ref({ open: false, kind: 'action', mode: 'append', index: null, path: [], op: 'any' })
const folderPickerOpen = ref(false)
const desktopRecorderOpen = ref(false)
const editingRuleName = ref(false)
const ruleNameInput = ref(null)
let dragState = null
const currentFolderName = computed(() => {
  const folder = String(props.rule.folder || '').trim()
  return folder === '未分类' ? '' : folder
})
async function startRuleNameEdit() {
  editingRuleName.value = true
  await nextTick()
  ruleNameInput.value?.focus()
  ruleNameInput.value?.select()
}
function finishRuleNameEdit() {
  props.rule.name = String(props.rule.name || '').trim() || '未命名规则'
  editingRuleName.value = false
}
function chooseFolder(folder) {
  props.rule.folder = String(folder || '').trim()
  folderPickerOpen.value = false
}
const triggerKeys = computed(() => Object.keys(store.schema.triggers).filter(
  key => store.schema.triggers[key]?.platform_compatible !== false
))
const triggerGroups = computed(() => groupTriggerKeys(triggerKeys.value))
const actionKeys = computed(() => Object.keys(store.schema.actions).filter(
  key => store.schema.actions[key]?.platform_compatible !== false
))
// 录制入口由插件的 uia_selector 采集组件声明驱动，不写死具体插件。
const desktopRecorder = computed(() => store.components.find(component => (
  component.available
  && (component.param_types || []).includes('uia_selector')
)) || null)
const desktopRecorderAvailable = computed(() => !!desktopRecorder.value)
const preconditionKeys = computed(() => actionKeys.value.filter(
  key => store.schema.actions[key]?.precondition_api === 'context-v1'
))
const actionGroups = computed(() => groupActionKeys(actionKeys.value))
const preconditionGroups = computed(() => groupActionKeys(preconditionKeys.value))
const isCondition = computed(() => (
  !!props.rule.condition
  && typeof props.rule.condition === 'object'
  && !Array.isArray(props.rule.condition)
))
normalizeRuleDraft(props.rule)
ensureRuleBindingIds(props.rule)
const conditionNode = computed(() => props.rule.condition ? normalizeConditionTree(props.rule.condition) : null)
const isAdmin = (meta) => !!(meta?.permissions || []).includes('admin')
const eventParams = (event) => getVisibleParamDefs(store.schema.triggers[event.type], event.params)
const actionParams = (action) => getVisibleParamDefs(store.schema.actions[action.type], action.params)
const eventName = (event) => store.schema.triggers[event?.type]?.name || event?.type || '未选择触发器'
const actionName = (action) => store.schema.actions[action?.type]?.name || action?.type || '未选择动作'
function actionFailureSummary(action) {
  const parts = [action?.on_error === 'continue' ? '失败后继续' : '失败后停止']
  const retries = Math.min(Math.max(Number(action?.retry || 0), 0), 3)
  if (retries) parts.push(`最多重试 ${retries} 次`)
  if (action?.timeout_seconds) parts.push(`最多运行 ${action.timeout_seconds} 秒`)
  if (action?.failure_actions?.length) parts.push(`${action.failure_actions.length} 个补救动作`)
  return parts.join(' · ')
}
function validUiaSelector(value) {
  return value && typeof value === 'object'
    && value.version === 1
    && value.window && typeof value.window === 'object'
    && value.target && typeof value.target === 'object'
    && Number.isInteger(value.target.control_type)
    && ['automation_id', 'name', 'class_name'].some(
      key => typeof value.target[key] === 'string' && value.target[key],
    )
}
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
  const controlGraph = buildFlowGraph({
    rule: props.rule,
    condition: isCondition.value ? conditionNode.value : null,
    eventName,
    actionName,
    describeItem,
    isAdmin: (item, kind) => isAdmin(
      kind === 'trigger' ? store.schema.triggers[item?.type] : store.schema.actions[item?.type],
    ),
  })
  const fullNodes = controlGraph.nodes.map(node => buildNodeDataPorts(node, store.schema))
  const dataEdges = deriveDataEdges(props.rule, fullNodes)
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
    const footerHeight = ['action', 'failure-action', 'precondition'].includes(node.kind) ? 34 : node.kind === 'condition' ? 40 : 0
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
    edges: [...controlGraph.edges, ...dataEdges],
  }
})
const layoutNodes = computed(() => resolveNodeCollisions(graph.value.nodes.map(node => ({
  ...node,
  ...(nodePositions.value[node.id] || {}),
  manuallyPlaced: !!nodePositions.value[node.id],
}))))
const graphEdges = computed(() => {
  const nodesById = new Map(layoutNodes.value.map(node => [node.id, node]))
  return graph.value.edges.map(edge => routeFlowEdge(edge, nodesById)).filter(Boolean)
})
const selectedGraphNode = computed(() => layoutNodes.value.find(node => node.id === selectedNodeId.value) || null)
watch(layoutNodes, nodes => {
  if (
    selectedNodeId.value
    && selectedNodeId.value !== 'add-precondition'
    && !nodes.some(node => node.id === selectedNodeId.value)
  ) selectedNodeId.value = null
})
const selectedKind = computed(() => {
  if (selectedNodeId.value === 'add-precondition') return 'add-precondition'
  return selectedGraphNode.value?.kind || 'trigger'
})
const selectedIndex = computed(() => selectedGraphNode.value?.index ?? -1)
const selectedAction = computed(() => selectedKind.value === 'action' ? props.rule.actions?.[selectedIndex.value] : null)
const selectedFailureAction = computed(() => selectedKind.value === 'failure-action'
  ? props.rule.actions?.[selectedGraphNode.value?.parentIndex]?.failure_actions?.[selectedIndex.value]
  : null)
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

const clientValidationIssues = computed(() => {
  const issues = []
  const add = (message, target = '') => issues.push({ severity: 'error', message, target })
  function validateCondition(node, path = '触发条件') {
    if (!node || typeof node !== 'object') { add(`${path}格式无效`, 'trigger'); return }
    const leaf = !!node.type && !node.children && !node.events
    if (leaf) {
      if (
        !store.schema.triggers[node.type]
        || store.schema.triggers[node.type]?.platform_compatible === false
      ) add(`${path}引用了当前系统不可用的触发器`, 'trigger')
      if (node.params != null && (typeof node.params !== 'object' || Array.isArray(node.params))) add(`${path}参数格式无效`, 'trigger')
      return
    }
    if (!['any', 'all'].includes(node.op)) add(`${path}的组合方式无效`, 'trigger')
    if (!Array.isArray(node.children) || !node.children.length) {
      add(`${path}组不能为空`, 'trigger')
      return
    }
    if ('within_seconds' in node) {
      const seconds = Number(node.within_seconds)
      if (!Number.isFinite(seconds) || seconds <= 0) add(`${path}的时间窗口必须大于 0`, 'trigger')
    }
    node.children.forEach((child, index) => validateCondition(child, `${path} ${index + 1}`))
  }
  if (!String(props.rule.name || '').trim()) add('请填写规则名称', 'name')
  if (props.rule.event && props.rule.condition) add('单个触发条件和组合条件不能同时存在', 'trigger')
  if (!props.rule.event && !props.rule.condition) add('请选择至少一个触发条件', 'trigger')
  if (props.rule.event) validateCondition(props.rule.event)
  if (props.rule.condition) validateCondition(conditionNode.value)
  const actions = Array.isArray(props.rule.actions) ? props.rule.actions : []
  if (!Array.isArray(props.rule.actions)) add('动作列表格式无效', 'actions')
  if (!actions.length) add('请添加至少一个执行动作', 'actions')
  function validateTimeout(action, label, nodeId) {
    if (action?.timeout_seconds == null) return
    const seconds = Number(action.timeout_seconds)
    if (!Number.isFinite(seconds) || seconds < 1 || seconds > 86400) {
      add(`${label}的最长运行时间必须是 1 到 86400 秒`, nodeId)
      return
    }
    if (store.schema.actions[action.type]?.cancellation_api !== 'runtime-v1') {
      add(`${label}不能安全停止，不能设置最长运行时间`, nodeId)
    }
  }
  actions.forEach((action, index) => {
    if (
      !store.schema.actions[action?.type]
      || store.schema.actions[action?.type]?.platform_compatible === false
    ) add(`动作 ${index + 1} 引用了当前系统不可用的插件`, `action:${index}`)
    if (action?.params != null && (typeof action.params !== 'object' || Array.isArray(action.params))) add(`动作 ${index + 1} 参数格式无效`, `action:${index}`)
    validateTimeout(action, `动作 ${index + 1}`, `action:${index}`)
    const sources = actionBindingSources(index)
    for (const def of actionParams(action)) {
      const value = action?.params?.[def.name]
      if (def.type === 'uia_selector' && !isReference(value)) {
        if (!validUiaSelector(value)) add(`动作 ${index + 1} 还没有选择有效的屏幕控件`, `action:${index}`)
      }
      if (!isReference(value)) continue
      const source = sources.find(item => JSON.stringify(item.value) === JSON.stringify(value))
      if (!source) add(`动作 ${index + 1} 的“${def.label || def.name}”引用了不可用数据`, `action:${index}`)
      else if (!typesCompatible(source.type, def.value_type || def.type)) {
        add(`动作 ${index + 1} 的“${def.label || def.name}”数据类型不兼容`, `action:${index}`)
      }
    }
    ;(Array.isArray(action?.failure_actions) ? action.failure_actions : []).forEach((failureAction, failureIndex) => {
      if (
        !store.schema.actions[failureAction?.type]
        || store.schema.actions[failureAction?.type]?.platform_compatible === false
      ) add(`动作 ${index + 1} 的补救动作 ${failureIndex + 1} 不可用`, `action:${index}`)
      if (failureAction?.params != null && (typeof failureAction.params !== 'object' || Array.isArray(failureAction.params))) {
        add(`动作 ${index + 1} 的补救动作 ${failureIndex + 1} 参数格式无效`, `action:${index}`)
      }
      validateTimeout(failureAction, `动作 ${index + 1} 的补救动作 ${failureIndex + 1}`, `action:${index}`)
      const failureSources = failureActionBindingSources(index, failureIndex)
      for (const def of actionParams(failureAction)) {
        const value = failureAction?.params?.[def.name]
        if (def.type === 'uia_selector' && !isReference(value)) {
          if (!validUiaSelector(value)) add(`动作 ${index + 1} 的补救动作 ${failureIndex + 1} 还没有选择有效的屏幕控件`, `action:${index}`)
        }
        if (!isReference(value)) continue
        const source = failureSources.find(item => JSON.stringify(item.value) === JSON.stringify(value))
        if (!source) add(`动作 ${index + 1} 的补救动作 ${failureIndex + 1} 引用了不可用数据`, `action:${index}`)
        else if (!typesCompatible(source.type, def.value_type || def.type)) {
          add(`动作 ${index + 1} 的补救动作 ${failureIndex + 1} 数据类型不兼容`, `action:${index}`)
        }
      }
    })
  })
  const preconditions = props.rule.preconditions == null
    ? []
    : Array.isArray(props.rule.preconditions) ? props.rule.preconditions : null
  if (preconditions === null) add('开始前确认列表格式无效', 'preconditions')
  ;(preconditions || []).forEach((item, index) => {
    if (!preconditionKeys.value.includes(item?.type)) add(`开始前确认 ${index + 1} 不可用`, `precondition:${index}`)
    if (item?.params != null && (typeof item.params !== 'object' || Array.isArray(item.params))) add(`开始前确认 ${index + 1} 参数格式无效`, `precondition:${index}`)
    const sources = preconditionBindingSources()
    for (const def of actionParams(item)) {
      const value = item?.params?.[def.name]
      if (!isReference(value)) continue
      const source = sources.find(candidate => JSON.stringify(candidate.value) === JSON.stringify(value))
      if (!source) add(`开始前确认 ${index + 1} 的“${def.label || def.name}”引用了不可用数据`, `precondition:${index}`)
      else if (!typesCompatible(source.type, def.value_type || def.type)) {
        add(`开始前确认 ${index + 1} 的“${def.label || def.name}”数据类型不兼容`, `precondition:${index}`)
      }
    }
  })
  return issues
})

const serverValidationIssues = ref([])
const checkingRule = ref(false)
const checkerError = ref('')
let checkerTimer = null
let checkerSequence = 0

function issueTarget(issue) {
  const text = `${issue.location || ''} ${issue.message || ''}`
  const action = text.match(/actions\[(\d+)\]/)
  if (action) return `action:${action[1]}`
  const precondition = text.match(/preconditions\[(\d+)\]/)
  if (precondition) return `precondition:${precondition[1]}`
  if (/event|condition|触发器|触发条件/.test(text)) return 'trigger'
  if (/name 不能为空|规则名称/.test(text)) return 'name'
  return ''
}

const validationIssues = computed(() => {
  const combined = [
    ...clientValidationIssues.value,
    ...serverValidationIssues.value.map(issue => ({
      ...issue,
      target: issueTarget(issue),
    })),
  ]
  const seen = new Set()
  return combined.filter(issue => {
    if (seen.has(issue.message)) return false
    seen.add(issue.message)
    return true
  })
})
const validationErrorCount = computed(() => validationIssues.value.filter(issue => issue.severity !== 'warning').length)
const validationWarningCount = computed(() => validationIssues.value.filter(issue => issue.severity === 'warning').length)

async function runRuleCheck() {
  if (checkerTimer) { clearTimeout(checkerTimer); checkerTimer = null }
  const sequence = ++checkerSequence
  checkingRule.value = true
  checkerError.value = ''
  try {
    const result = await validateRuleDraft(JSON.parse(JSON.stringify(props.rule)))
    if (sequence !== checkerSequence) return
    if (!result?.ok || !Array.isArray(result.issues)) {
      checkerError.value = result?.error || '检查服务没有返回有效结果'
      serverValidationIssues.value = []
      return
    }
    serverValidationIssues.value = result.issues
  } catch (error) {
    if (sequence !== checkerSequence) return
    checkerError.value = error.message || '无法连接规则检查服务'
    serverValidationIssues.value = []
  } finally {
    if (sequence === checkerSequence) checkingRule.value = false
  }
}

function scheduleRuleCheck() {
  if (checkerTimer) clearTimeout(checkerTimer)
  checkerTimer = setTimeout(runRuleCheck, 350)
}

function focusValidationIssue(issue) {
  if (issue.target === 'name') { startRuleNameEdit(); return }
  editorMode.value = 'canvas'
  nextTick(() => {
    if (issue.target === 'trigger') selectNode(isCondition.value ? 'condition-root' : 'trigger')
    else if (issue.target === 'actions') selectNode('add-action')
    else if (issue.target?.startsWith('action:')) {
      const action = props.rule.actions?.[Number(issue.target.split(':')[1])]
      if (action) selectNode(`action-${action.binding_id}`)
    } else if (issue.target?.startsWith('precondition:')) {
      const item = props.rule.preconditions?.[Number(issue.target.split(':')[1])]
      if (item) selectNode(`precondition-${item.binding_id}`)
    }
  })
}

watch(() => JSON.stringify(props.rule), scheduleRuleCheck, { immediate: true })

function setSingleTrigger(type) {
  if (!type) return
  const bindingId = props.rule.event?.binding_id || createBindingId('trigger')
  props.rule.event = {
    binding_id: bindingId,
    type,
    params: buildDefaultParams(store.schema.triggers[type]),
  }
  delete props.rule.condition
  newTriggerType.value = ''
  selectedNodeId.value = 'trigger'
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
  // 结构切换沿用选中叶节点的 binding_id，下游 $ref 继续指向该节点。
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
function changeConditionOp() {
  const node = selectedConditionNode.value
  if (!node || selectedKind.value !== 'condition') return
  if (node.op !== 'all') delete node.within_seconds
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
function addAction(type = newActionType.value || actionKeys.value[0], insertIndex = null) {
  if (!type) return
  if (!Array.isArray(props.rule.actions)) props.rule.actions = []
  const action = {
    binding_id: createBindingId('action'),
    type,
    params: buildDefaultParams(store.schema.actions[type]),
  }
  const index = insertIndex == null
    ? props.rule.actions.length
    : Math.max(0, Math.min(props.rule.actions.length, insertIndex))
  props.rule.actions.splice(index, 0, action)
  selectedNodeId.value = `action-${action.binding_id}`
  newActionType.value = ''
}
function insertRecordedSteps(actions) {
  if (!Array.isArray(actions) || !actions.length) return
  if (!Array.isArray(props.rule.actions)) props.rule.actions = []
  const inserted = actions.map(action => ({
    ...action,
    binding_id: createBindingId('action'),
  }))
  props.rule.actions.push(...inserted)
  selectedNodeId.value = `action-${inserted[inserted.length - 1].binding_id}`
  nodePositions.value = {}
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
}
function duplicateAction(index) {
  const source = props.rule.actions?.[index]
  if (!source) return
  const copy = JSON.parse(JSON.stringify(source))
  regenerateBindingIds(copy, 'action')
  props.rule.actions.splice(index + 1, 0, copy)
  selectedNodeId.value = `action-${copy.binding_id}`
}
function moveAction(index, offset) {
  const target = index + offset
  if (target < 0 || target >= props.rule.actions.length) return
  const [action] = props.rule.actions.splice(index, 1)
  props.rule.actions.splice(target, 0, action)
  selectedNodeId.value = `action-${action.binding_id}`
}
function addPrecondition(type = newPreconditionType.value || preconditionKeys.value[0]) {
  if (!type) return
  if (!Array.isArray(props.rule.preconditions)) props.rule.preconditions = []
  props.rule.preconditions.push({
    binding_id: createBindingId('precondition'),
    type,
    params: buildDefaultParams(store.schema.actions[type]),
  })
  selectedNodeId.value = `precondition-${props.rule.preconditions.at(-1).binding_id}`
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
function failureActionBindingSources(actionIndex, failureIndex) {
  return buildFailureBindingSources(props.rule, actionIndex, failureIndex, store.schema)
}
function preconditionBindingSources() {
  return buildBindingSources(props.rule, 0, store.schema, {
    allowSteps: false,
    allowConditionalTriggers: false,
  })
}
function removePrecondition(index) {
  props.rule.preconditions.splice(index, 1)
  selectedNodeId.value = props.rule.preconditions.length
    ? `precondition-${props.rule.preconditions[Math.min(index, props.rule.preconditions.length - 1)].binding_id}`
    : 'trigger'
}
function addFailureAction(actionIndex, type) {
  const action = props.rule.actions?.[actionIndex]
  if (!action || !type) return
  if (!Array.isArray(action.failure_actions)) action.failure_actions = []
  const failureAction = {
    binding_id: createBindingId('action'),
    type,
    params: buildDefaultParams(store.schema.actions[type]),
  }
  action.failure_actions.push(failureAction)
  selectedNodeId.value = `failure-action-${failureAction.binding_id}`
}
function removeFailureAction(actionIndex, failureIndex) {
  const failureActions = props.rule.actions?.[actionIndex]?.failure_actions
  if (!Array.isArray(failureActions)) return
  failureActions.splice(failureIndex, 1)
  if (!failureActions.length) delete props.rule.actions[actionIndex].failure_actions
  selectedNodeId.value = `action-${props.rule.actions[actionIndex].binding_id}`
}
function moveFailureAction(actionIndex, failureIndex, offset) {
  const failureActions = props.rule.actions?.[actionIndex]?.failure_actions
  const target = failureIndex + offset
  if (!Array.isArray(failureActions) || target < 0 || target >= failureActions.length) return
  const [failureAction] = failureActions.splice(failureIndex, 1)
  failureActions.splice(target, 0, failureAction)
  selectedNodeId.value = `failure-action-${failureAction.binding_id}`
}
function requestAddFailureAction(actionIndex) {
  openPluginPicker('action', { mode: 'failure-action', parentIndex: actionIndex, title: '添加补救动作' })
}
function requestReplaceFailureAction(actionIndex, failureIndex) {
  openPluginPicker('action', { mode: 'replace-failure-action', parentIndex: actionIndex, failureIndex, title: '更换补救动作' })
}
function selectNode(id) {
  if (suppressNodeClickId === id) {
    suppressNodeClickId = null
    return
  }
  if (id === 'add-action') {
    openPluginPicker('action', { mode: 'action', index: props.rule.actions?.length || 0, title: '添加下一步' })
    return
  }
  selectedNodeId.value = id
}

function focusInitialNode(id) {
  if (!id) return
  editorMode.value = 'canvas'
  nextTick(() => {
    if (layoutNodes.value.some(node => node.id === id)) selectNode(id)
  })
}
watch(() => props.initialNodeId, focusInitialNode, { immediate: true })

function openPluginPicker(kind, options = {}) {
  picker.value = {
    open: true,
    kind,
    mode: options.mode || kind,
    index: options.index ?? null,
    parentIndex: options.parentIndex ?? null,
    failureIndex: options.failureIndex ?? null,
    path: options.path || [],
    op: options.op || 'any',
    title: options.title || '',
  }
}
function closePluginPicker() { picker.value = { ...picker.value, open: false } }
function conditionAtPath(path) {
  let node = conditionNode.value
  for (const index of path || []) node = node?.children?.[index]
  return node
}
function addConditionFromPicker(type, { path, group = false } = {}) {
  const parent = conditionAtPath(path)
  if (!parent || !Array.isArray(parent.children)) return
  const event = {
    binding_id: createBindingId('trigger'),
    type,
    params: buildDefaultParams(store.schema.triggers[type]),
  }
  parent.children.push(group ? { op: 'any', children: [event] } : event)
  const eventPath = group
    ? [...path, parent.children.length - 1, 0]
    : [...path, parent.children.length - 1]
  selectedNodeId.value = conditionId(eventPath)
}
function upgradeWithCondition(type, op) {
  const initial = props.rule.event
  const added = {
    binding_id: createBindingId('trigger'),
    type,
    params: buildDefaultParams(store.schema.triggers[type]),
  }
  props.rule.condition = {
    op,
    children: [
      ...(initial ? [{
        binding_id: initial.binding_id || createBindingId('trigger'),
        type: initial.type,
        params: { ...initial.params },
      }] : []),
      added,
    ],
  }
  delete props.rule.event
  selectedNodeId.value = conditionId([props.rule.condition.children.length - 1])
}
function choosePlugin(key) {
  const context = { ...picker.value }
  closePluginPicker()
  if (context.mode === 'set-trigger' || context.mode === 'replace-trigger') setSingleTrigger(key)
  else if (context.mode === 'replace-condition-trigger') {
    const node = conditionAtPath(context.path)
    if (node?.type) {
      node.type = key
      node.params = buildDefaultParams(store.schema.triggers[key])
    }
  }
  else if (context.mode === 'action') addAction(key, context.index)
  else if (context.mode === 'replace-action') {
    const action = props.rule.actions?.[context.index]
    if (action) changeAction(action, key)
  }
  else if (context.mode === 'failure-action') addFailureAction(context.parentIndex, key)
  else if (context.mode === 'replace-failure-action') {
    const action = props.rule.actions?.[context.parentIndex]?.failure_actions?.[context.failureIndex]
    if (action) changeAction(action, key)
  }
  else if (context.mode === 'precondition') addPrecondition(key)
  else if (context.mode === 'condition-child') addConditionFromPicker(key, { path: context.path })
  else if (context.mode === 'condition-group') addConditionFromPicker(key, { path: context.path, group: true })
  else if (context.mode === 'upgrade') upgradeWithCondition(key, context.op)
}
function requestConditionChild(node, group = false) {
  openPluginPicker('trigger', {
    mode: group ? 'condition-group' : 'condition-child',
    path: node.path || [],
    title: group ? '添加条件组的第一个条件' : '添加条件',
  })
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
let formConditionLayoutKey = null
let suppressNodeClickId = null
let suppressNodeClickTimer = null

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
  if (event.button !== 0) return
  if (event.target.closest('.graph-node') || event.target.closest('button')) return
  selectedNodeId.value = null
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

function dataReference(node, port) {
  return {
    $ref: {
      scope: node.kind === 'trigger' ? 'trigger' : 'step',
      node: node.source.binding_id,
      path: [port.name],
    },
  }
}
function canConnectDataPorts(sourceNode, sourcePort, targetNode, targetPort) {
  if (!sourcePort.required || !typesCompatible(sourcePort.type, targetPort.type)) return false
  if (['action', 'failure-action'].includes(sourceNode.kind)) {
    return ['action', 'failure-action'].includes(targetNode.kind)
      && (targetNode.availableStepIds || []).includes(sourceNode.source.binding_id)
  }
  if (sourceNode.kind !== 'trigger') return false
  if (targetNode.kind === 'precondition') {
    const guaranteed = guaranteedTriggerIds(props.rule.condition || props.rule.event)
    return guaranteed.has(sourceNode.source.binding_id)
  }
  return ['action', 'failure-action'].includes(targetNode.kind)
}
function inputPortAt(event) {
  const target = event.target?.closest?.('[data-input-node][data-input-port]')
    || document.elementFromPoint?.(event.clientX, event.clientY)?.closest?.('[data-input-node][data-input-port]')
  if (!target) return null
  const node = layoutNodes.value.find(item => item.id === target.dataset.inputNode)
  const port = node?.dataInputs.find(item => item.name === target.dataset.inputPort)
  return node && port ? { node, port } : null
}
function updateDataDropTarget(event) {
  if (!dataDrag.value) return
  dataDrag.value = { ...dataDrag.value, clientX: event.clientX, clientY: event.clientY }
  const target = inputPortAt(event)
  const source = layoutNodes.value.find(node => node.id === dataDrag.value.sourceNodeId)
  const sourcePort = source?.dataOutputs.find(port => port.name === dataDrag.value.sourcePortName)
  dataDropTarget.value = target && source && sourcePort && canConnectDataPorts(source, sourcePort, target.node, target.port)
    ? target
    : null
}
function startDataDrag(event, node, port) {
  if (event.button !== 0 || !port.required) return
  event.stopPropagation()
  dataDrag.value = {
    sourceNodeId: node.id,
    sourcePortName: port.name,
    clientX: event.clientX,
    clientY: event.clientY,
  }
  dataDropTarget.value = null
  event.currentTarget.setPointerCapture?.(event.pointerId)
}
function endDataDrag(event) {
  if (!dataDrag.value) return
  updateDataDropTarget(event)
  const source = layoutNodes.value.find(node => node.id === dataDrag.value.sourceNodeId)
  const sourcePort = source?.dataOutputs.find(port => port.name === dataDrag.value.sourcePortName)
  const target = dataDropTarget.value
  if (source && sourcePort && target && canConnectDataPorts(source, sourcePort, target.node, target.port)) {
    target.node.source.params ||= {}
    target.node.source.params[target.port.name] = dataReference(source, sourcePort)
  }
  dataDrag.value = null
  dataDropTarget.value = null
}
function cancelDataDrag() {
  dataDrag.value = null
  dataDropTarget.value = null
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
  if (node && node.id !== previous?.id && editorMode.value === 'canvas') centerOnNode(node)
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
function nodeDimmed(id) { return highlightedIds.value ? !highlightedIds.value.has(id) : false }
function edgeDimmed(edge) { return highlightedIds.value ? !highlightedIds.value.has(edge.from) || !highlightedIds.value.has(edge.to) : false }

function conditionStructureKey(node) {
  if (!node || typeof node !== 'object') return ''
  if (node.type && !node.children && !node.events) return `leaf:${node.binding_id || node.type}`
  const children = node.children || node.events || []
  return `group:${node.op || node.type || 'any'}(${children.map(conditionStructureKey).join(',')})`
}

watch(editorMode, async (mode, previousMode) => {
  try { localStorage.setItem('notmyfault.ruleEditorMode', mode) } catch {}
  if (mode === 'form') {
    formConditionLayoutKey = conditionStructureKey(conditionNode.value)
    resizeObserver?.disconnect()
    return
  }
  if (mode === 'canvas' && previousMode === 'form') {
    if (formConditionLayoutKey !== conditionStructureKey(conditionNode.value)) nodePositions.value = {}
    formConditionLayoutKey = null
  }
  await nextTick()
  observeViewport()
})

function resetLayout() {
  nodePositions.value = {}
  fitView({ readable: true })
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
    moved: false,
  }
  event.currentTarget.setPointerCapture?.(event.pointerId)
}
function dragNode(event) {
  if (!dragState) return
  const deltaX = event.clientX - dragState.startX
  const deltaY = event.clientY - dragState.startY
  if (!dragState.moved) {
    if (Math.hypot(deltaX, deltaY) < 5) return
    dragState.moved = true
    // 开始拖动节点时关闭设置抽屉，并清除当前选中节点。
    selectedNodeId.value = null
  }
  // 屏幕像素换算为逻辑坐标时除以当前缩放。
  nodePositions.value = {
    ...nodePositions.value,
    [dragState.id]: {
      x: Math.max(24, dragState.x + deltaX / zoom.value),
      y: Math.max(24, dragState.y + deltaY / zoom.value),
    },
  }
}
function endNodeDrag() {
  if (dragState?.moved) {
    // pointerup 后浏览器会补发 click，这里只屏蔽当前节点的一次点击。
    suppressNodeClickId = dragState.id
    if (suppressNodeClickTimer) clearTimeout(suppressNodeClickTimer)
    suppressNodeClickTimer = setTimeout(() => {
      suppressNodeClickId = null
      suppressNodeClickTimer = null
    }, 0)
  }
  dragState = null
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
  observeViewport()
  window.addEventListener('keydown', onEditorKeydown)
  aiPanelOpen.value = store.pendingAiPanel === true
  store.pendingAiPanel = false
  const savedWidth = Number(localStorage.getItem('notmyfault.aiPanelWidth'))
  if (savedWidth >= AI_PANEL_MIN_WIDTH && savedWidth <= AI_PANEL_MAX_WIDTH) {
    aiPanelWidth.value = savedWidth
  }
  wideLayoutMedia = window.matchMedia('(min-width: 1360px)')
  wideLayout.value = wideLayoutMedia.matches
  wideLayoutMedia.addEventListener?.('change', syncWideLayout)
})
onUnmounted(() => {
  resizeObserver?.disconnect()
  if (camAnimTimer) clearTimeout(camAnimTimer)
  if (suppressNodeClickTimer) clearTimeout(suppressNodeClickTimer)
  if (checkerTimer) clearTimeout(checkerTimer)
  checkerSequence++
  window.removeEventListener('keydown', onEditorKeydown)
  wideLayoutMedia?.removeEventListener?.('change', syncWideLayout)
  stopAiPanelResize()
})
function onEditorKeydown(event) {
  const target = event.target
  const editingText = target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement
  if ((event.ctrlKey || event.metaKey) && !editingText) {
    if (event.key.toLowerCase() === 'z' && !event.shiftKey && props.canUndo) {
      event.preventDefault()
      emit('undo')
      return
    }
    if ((event.key.toLowerCase() === 'y' || (event.key.toLowerCase() === 'z' && event.shiftKey)) && props.canRedo) {
      event.preventDefault()
      emit('redo')
      return
    }
  }
  if (event.key !== 'Escape') return
  if (picker.value.open) closePluginPicker()
  else if (folderPickerOpen.value) folderPickerOpen.value = false
  else if (editingRuleName.value) finishRuleNameEdit()
  else selectedNodeId.value = null
}
</script>

<template>
  <section class="rule-editor-page flow-rule-editor" :class="{ 'with-side-panels': hasSidePanels }">
    <header class="rule-editor-head flow-editor-head">
      <div class="rule-editor-identity">
        <button class="btn btn-text rule-back-btn" @click="emit('back')"><span class="material-symbols-outlined">arrow_back</span>全部规则</button>
        <div class="rule-title-capsule" :class="{ editing: editingRuleName }">
          <input v-if="editingRuleName" ref="ruleNameInput" v-model="rule.name" type="text" aria-label="规则名称"
            autocomplete="off" autocorrect="off" spellcheck="false"
            @blur="finishRuleNameEdit" @keydown.enter.prevent="finishRuleNameEdit">
          <span v-else :title="rule.name">{{ rule.name || '未命名规则' }}</span>
        </div>
        <button class="icon-btn rule-title-edit" title="修改规则名称" @click="startRuleNameEdit">
          <span class="material-symbols-outlined">edit</span>
        </button>
        <button class="rule-folder-button" :class="{ empty: !currentFolderName }" title="管理文件夹" @click="folderPickerOpen = true">
          <span class="material-symbols-outlined">folder</span><span v-if="currentFolderName">{{ currentFolderName }}</span>
        </button>
      </div>
    </header>

    <div class="editor-mode-bar">
      <div class="editor-mode-switch" role="tablist" aria-label="规则编辑方式">
        <button role="tab" :aria-selected="editorMode === 'canvas'" :class="{ active: editorMode === 'canvas' }" @click="editorMode = 'canvas'">
          <span class="material-symbols-outlined">account_tree</span>节点编辑
        </button>
        <button role="tab" :aria-selected="editorMode === 'form'" :class="{ active: editorMode === 'form' }" @click="editorMode = 'form'">
          <span class="material-symbols-outlined">view_agenda</span>普通模式
        </button>
      </div>
      <div class="rule-editor-actions">
        <button v-if="aiEnabled" class="icon-btn" :class="{ 'ai-panel-active': aiPanelOpen }"
          :title="aiPanelOpen ? '收起 AI 起草' : '打开 AI 起草'" @click="toggleAiPanel">
          <span class="material-symbols-outlined">auto_awesome</span>
        </button>
        <div class="rule-history-actions" aria-label="草稿历史">
          <button class="icon-btn" :disabled="!canUndo" title="撤销（Ctrl+Z）" @click="emit('undo')"><span class="material-symbols-outlined">undo</span></button>
          <button class="icon-btn" :disabled="!canRedo" title="重做（Ctrl+Y）" @click="emit('redo')"><span class="material-symbols-outlined">redo</span></button>
        </div>
        <button class="btn btn-text rule-check-btn" :disabled="checkingRule" title="立即检查当前草稿" @click="runRuleCheck">
          <span v-if="checkingRule" class="spinner"></span>
          <span v-else class="material-symbols-outlined">fact_check</span>
          {{ checkingRule ? '检查中…' : '规则检查' }}
          <span v-if="validationErrorCount" class="rule-check-count">{{ validationErrorCount }}</span>
        </button>
        <button class="btn btn-text danger-text" @click="emit('delete')"><span class="material-symbols-outlined">delete</span>删除</button>
        <button class="btn btn-outlined rule-test-btn" :disabled="validationErrorCount || testing"
          title="保存当前修改，并真实执行一次规则中的动作" @click="emit('save-run')">
          <span v-if="testing" class="spinner"></span>
          <span v-else class="material-symbols-outlined">experiment</span>
          {{ testing ? '测试中…' : '测试规则' }}
        </button>
        <button class="btn btn-filled" :disabled="validationErrorCount || !dirty" @click="emit('save')"><span class="material-symbols-outlined">save</span>保存规则</button>
      </div>
    </div>

    <Transition name="status-strip">
      <div v-if="dirty" class="draft-change-strip" aria-live="polite">
        <span class="material-symbols-outlined">difference</span>
        <b>未保存</b>
        <div class="draft-change-items">
          <span v-for="item in changeSummary" :key="item">{{ item }}</span>
        </div>
        <small>保存后应用</small>
      </div>
    </Transition>

    <div class="rule-editor-main-grid">
    <div class="editor-canvas-column">
    <main v-show="editorMode === 'canvas'" class="node-editor-workspace" :class="{ 'editor-mode-active': editorMode === 'canvas' }">
      <section class="node-canvas-panel" aria-label="规则节点画布">
        <div class="node-canvas-toolbar">
          <div class="node-canvas-tools">
            <button class="btn btn-text btn-sm" @click="selectNode(isCondition ? 'condition-root' : 'trigger')"><span class="material-symbols-outlined">bolt</span>触发条件</button>
            <button v-if="preconditionKeys.length" class="btn btn-text btn-sm" @click="openPluginPicker('precondition', { mode: 'precondition', title: '添加开始前确认' })"><span class="material-symbols-outlined">verified_user</span>添加确认</button>
            <button v-if="desktopRecorderAvailable" class="btn btn-text btn-sm" @click="desktopRecorderOpen = true"><span class="material-symbols-outlined">screen_record</span>{{ desktopRecorder.ui?.button_label || '录制桌面步骤' }}</button>
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
              <button class="icon-btn" title="整理布局" @click="resetLayout"><span class="material-symbols-outlined">auto_fix_high</span></button>
            </div>
          </div>
        </div>
        <div ref="viewportRef" class="node-canvas-viewport" :class="{ panning: isPanning }" :style="gridStyle"
          @pointerdown="startPan" @pointermove="dataDrag ? updateDataDropTarget($event) : movePan($event)"
          @pointerup="dataDrag ? endDataDrag($event) : endPan()" @pointercancel="dataDrag ? cancelDataDrag() : endPan()"
          @wheel="onCanvasWheel">
          <div class="node-canvas" :class="{ 'cam-anim': camAnim }" :style="cameraStyle">
            <!-- 画布和 SVG 共用逻辑像素坐标，overflow:visible 让连线绘制到容器外，设置 viewBox 会让端口错位。 -->
            <svg class="node-links" aria-hidden="true">
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

            <button v-for="edge in graphEdges.filter(item => Number.isInteger(item.insertActionIndex) && item.to !== 'add-action')"
              :key="`insert-${edge.id}`" class="canvas-edge-add" :style="{ left: `${edge.labelX}px`, top: `${edge.labelY + 9}px` }"
              :title="`在第 ${edge.insertActionIndex + 1} 步插入动作`"
              @click.stop="openPluginPicker('action', { mode: 'action', index: edge.insertActionIndex, title: '插入动作' })">
              <span class="material-symbols-outlined">add</span>
            </button>

            <article v-for="node in layoutNodes" :key="node.id"
              class="graph-node" :class="[`graph-node-${node.kind}`, { selected: selectedNodeId === node.id, dimmed: nodeDimmed(node.id), 'data-incompatible': nodeDataIncompatible(node) }]"
              :style="{ left: `${node.x}px`, top: `${node.y}px`, height: `${node.height}px` }"
              role="button" tabindex="0" @click="selectNode(node.id)" @keydown.enter="selectNode(node.id)"
              @pointerenter="hoverNodeId = node.id" @pointerleave="hoverNodeId = null">
              <span v-if="node.hasInput" class="node-port node-port-in"></span>
              <header class="graph-node-head" @pointerdown.stop="startNodeDrag($event, node)"
                @pointermove.stop="dragNode" @pointerup.stop="endNodeDrag" @pointercancel.stop="endNodeDrag">
                <span class="material-symbols-outlined">{{ node.icon }}</span>
                <span>{{ node.kicker }}</span>
                <span class="material-symbols-outlined node-drag-handle">drag_indicator</span>
              </header>
              <div class="graph-node-body" :class="{ 'has-admin': node.admin }">
                <b>{{ node.label }}</b>
                <small :title="node.meta">{{ node.meta }}</small>
                <span v-if="node.admin" class="node-admin">管理员权限</span>
              </div>
              <div v-if="node.hasDataPorts" class="graph-node-data-summary">
                <button :class="{ active: sideExpanded(node.id, 'inputs') }" :disabled="!node.dataInputs.length" @click.stop="togglePorts(node.id, 'inputs')">
                  <span class="material-symbols-outlined">input</span>{{ dataSideLabel(node, 'inputs') }}
                  <span class="material-symbols-outlined">{{ sideExpanded(node.id, 'inputs') ? 'expand_less' : 'expand_more' }}</span>
                </button>
                <button :class="{ active: sideExpanded(node.id, 'outputs') }" :disabled="!node.dataOutputs.length" @click.stop="togglePorts(node.id, 'outputs')">
                  {{ dataSideLabel(node, 'outputs') }}<span class="material-symbols-outlined">output</span>
                  <span class="material-symbols-outlined">{{ sideExpanded(node.id, 'outputs') ? 'expand_less' : 'expand_more' }}</span>
                </button>
              </div>
              <div v-if="node.dataPortRows" class="graph-node-data">
                 <div class="data-port-column data-port-column-input">
                   <span v-for="port in node.visibleDataInputs" :key="port.id" class="data-port-row"
                     :class="dataInputDropState(node, port)" :data-input-node="node.id" :data-input-port="port.name"
                     :title="`${port.label} · ${port.type}`">
                      <i class="data-port-dot"></i><small>{{ port.label }}</small><code class="data-type-chip">{{ port.type }}</code>
                   </span>
                 </div>
                 <div class="data-port-column data-port-column-output">
                   <span v-for="port in node.visibleDataOutputs" :key="port.id" class="data-port-row" :class="{ optional: !port.required }" :title="`${port.label} · ${port.type}`">
                      <small>{{ port.label }}</small><code class="data-type-chip">{{ port.type }}</code><i class="data-port-dot"
                       :class="{ draggable: port.required }" @pointerdown="startDataDrag($event, node, port)"></i>
                   </span>
                </div>
              </div>
              <footer v-if="node.kind === 'action'" class="graph-node-actions">
                <button class="icon-btn" :disabled="node.index === 0" title="提前执行" @click.stop="moveAction(node.index, -1)"><span class="material-symbols-outlined">arrow_back</span></button>
                <button class="icon-btn" :disabled="node.index === rule.actions.length - 1" title="稍后执行" @click.stop="moveAction(node.index, 1)"><span class="material-symbols-outlined">arrow_forward</span></button>
                <button class="icon-btn icon-btn-danger" title="删除动作" @click.stop="removeAction(node.index)"><span class="material-symbols-outlined">delete</span></button>
              </footer>
              <footer v-else-if="node.kind === 'failure-action'" class="graph-node-actions">
                <button class="icon-btn" :disabled="node.index === 0" title="上移" @click.stop="moveFailureAction(node.parentIndex, node.index, -1)"><span class="material-symbols-outlined">arrow_back</span></button>
                <button class="icon-btn" :disabled="node.index === rule.actions[node.parentIndex].failure_actions.length - 1" title="下移" @click.stop="moveFailureAction(node.parentIndex, node.index, 1)"><span class="material-symbols-outlined">arrow_forward</span></button>
                <button class="icon-btn icon-btn-danger" title="删除补救动作" @click.stop="removeFailureAction(node.parentIndex, node.index)"><span class="material-symbols-outlined">delete</span></button>
              </footer>
              <footer v-else-if="node.kind === 'precondition'" class="graph-node-actions">
                <span></span><span></span>
                <button class="icon-btn icon-btn-danger" title="删除确认" @click.stop="removePrecondition(node.index)"><span class="material-symbols-outlined">delete</span></button>
              </footer>
              <footer v-else-if="node.kind === 'condition'" class="graph-node-logic-actions">
                <button @click.stop="requestConditionChild(node, false)"><span class="material-symbols-outlined">add</span>条件</button>
                <button @click.stop="requestConditionChild(node, true)"><span class="material-symbols-outlined">account_tree</span>条件组</button>
              </footer>
              <span v-if="node.hasOutput" class="node-port node-port-out"></span>
            </article>
          </div>

          <div v-if="layoutNodes.length" class="node-minimap" title="小地图：点击或拖动跳转"
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
        <div class="canvas-help"><span class="material-symbols-outlined">pan_tool</span>拖空白处平移 · 从输出端口拖至输入端口绑定数据 · 实线为控制流，虚线为数据流</div>
      </section>

          </main>

    <main v-show="editorMode === 'form'" class="automation-flow classic-rule-editor" :class="{ 'editor-mode-active': editorMode === 'form' }">
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
              <div class="field field-wide"><span class="field-label">触发方式</span>
                <button class="plugin-type-button" type="button"
                  @click="openPluginPicker('trigger', { mode: 'replace-trigger', title: '更换触发方式' })">
                  <span class="material-symbols-outlined">bolt</span>
                  <span>{{ eventName(rule.event) }}</span>
                  <span class="material-symbols-outlined">arrow_forward</span>
                </button>
              </div>
              <div class="param-grid"><ParamInput v-for="param in eventParams(rule.event)" :key="param.name" :def="param" :plugin-id="rule.event.type" v-model="rule.event.params[param.name]" /></div>
            </div>
          </details>
          <div v-else class="flow-empty-card">
            <span class="material-symbols-outlined">touch_app</span>
            <div><b>选择触发方式</b><p>先说明什么时候开始执行，不默认替你选择。</p></div>
            <button class="btn btn-filled" @click="openPluginPicker('trigger', { mode: 'set-trigger', title: '选择触发方式' })">选择</button>
          </div>
          <div class="flow-stage-actions">
            <template v-if="rule.event">
              <button class="btn btn-text btn-sm" @click="openPluginPicker('trigger', { mode: 'upgrade', op: 'all', title: '添加“并且”条件' })"><span class="material-symbols-outlined">done_all</span>并且满足</button>
              <button class="btn btn-text btn-sm" @click="openPluginPicker('trigger', { mode: 'upgrade', op: 'any', title: '添加“或者”条件' })"><span class="material-symbols-outlined">alt_route</span>或者满足</button>
            </template>
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
              <div class="param-grid"><ParamInput v-for="param in actionParams(item)" :key="param.name" :def="param" :plugin-id="item.type" v-model="item.params[param.name]" allow-binding :binding-sources="preconditionBindingSources()" /></div>
            </div>
          </details>
          <div class="flow-add-control"><button class="btn btn-tonal" @click="openPluginPicker('precondition', { mode: 'precondition', title: '添加开始前确认' })"><span class="material-symbols-outlined">add</span>添加确认</button></div>
        </div>
      </section>

      <section class="automation-stage stage-then">
        <div class="stage-rail stage-rail-last"><span class="stage-node"><span class="material-symbols-outlined">play_arrow</span></span></div>
        <div class="stage-content">
          <header class="stage-head"><div><span class="stage-kicker">然后</span><h2>按顺序执行这些动作</h2></div><div class="stage-head-tools"><button v-if="desktopRecorderAvailable" class="btn btn-text btn-sm" @click="desktopRecorderOpen = true"><span class="material-symbols-outlined">screen_record</span>{{ desktopRecorder.ui?.button_label || '录制桌面步骤' }}</button><span class="stage-required">必填</span></div></header>
          <details v-for="(action, index) in rule.actions" :key="action" class="flow-card action-flow-card" :open="rule.actions.length === 1">
            <summary>
              <span class="flow-card-index">{{ index + 1 }}</span><span class="flow-card-copy"><b>{{ actionName(action) }}</b><small>第 {{ index + 1 }} 步 · {{ actionFailureSummary(action) }}</small></span>
              <span v-if="isAdmin(store.schema.actions[action.type])" class="chip chip-admin">管理员</span>
              <span class="flow-card-tools"><button class="icon-btn" :disabled="index === 0" title="上移" @click.prevent.stop="moveAction(index, -1)"><span class="material-symbols-outlined">arrow_upward</span></button><button class="icon-btn" :disabled="index === rule.actions.length - 1" title="下移" @click.prevent.stop="moveAction(index, 1)"><span class="material-symbols-outlined">arrow_downward</span></button><button class="icon-btn icon-btn-danger" title="移除动作" @click.prevent.stop="removeAction(index)"><span class="material-symbols-outlined">delete</span></button></span>
              <span class="material-symbols-outlined flow-expand">expand_more</span>
            </summary>
            <div class="flow-card-body">
              <div class="field field-wide"><span class="field-label">动作类型</span>
                <button class="plugin-type-button" type="button"
                  @click="openPluginPicker('action', { mode: 'replace-action', index, title: '更换动作类型' })">
                  <span class="material-symbols-outlined">play_arrow</span>
                  <span>{{ actionName(action) }}</span>
                  <span class="material-symbols-outlined">arrow_forward</span>
                </button>
              </div>
              <div class="param-grid"><ParamInput v-for="param in actionParams(action)" :key="param.name" :def="param" :plugin-id="action.type" v-model="action.params[param.name]" allow-binding :binding-sources="actionBindingSources(index)" /></div>
              <div v-if="actionOutputHint(action, index)" class="workflow-output-hint">后续步骤可引用：<code>{{ actionOutputHint(action, index) }}</code></div>
              <ActionFailureSettings :action="action" :meta="store.schema.actions[action.type]" :schema="store.schema.actions"
                :binding-sources="failureIndex => failureActionBindingSources(index, failureIndex)"
                @add-failure-action="requestAddFailureAction(index)"
                @replace-failure-action="failureIndex => requestReplaceFailureAction(index, failureIndex)"
                @remove-failure-action="failureIndex => removeFailureAction(index, failureIndex)"
                @move-failure-action="(failureIndex, offset) => moveFailureAction(index, failureIndex, offset)" />
            </div>
          </details>
          <div v-if="!rule.actions?.length" class="flow-inline-empty">还没有动作。规则触发后不会执行任何操作。</div>
          <div class="flow-add-control"><button class="btn btn-tonal" @click="openPluginPicker('action', { mode: 'action', index: rule.actions?.length || 0, title: '添加动作' })"><span class="material-symbols-outlined">add</span>添加动作</button></div>
        </div>
      </section>
    </main>

    <footer class="flow-validation" :class="{ valid: !validationErrorCount, warning: !validationErrorCount && validationWarningCount }">
      <span class="material-symbols-outlined">{{ validationErrorCount ? 'error' : validationWarningCount ? 'warning' : 'check_circle' }}</span>
      <div class="flow-validation-body">
        <b v-if="validationErrorCount">{{ validationErrorCount }} 项需要处理</b>
        <b v-else-if="validationWarningCount">可以保存，另有 {{ validationWarningCount }} 项提醒</b>
        <b v-else>{{ checkingRule ? '正在检查当前草稿…' : '规则可以保存' }}</b>
        <div v-if="validationIssues.length" class="flow-validation-list">
          <button v-for="(issue, index) in validationIssues" :key="`${issue.message}-${index}`" type="button"
            :class="`flow-validation-item ${issue.severity === 'warning' ? 'warning' : ''}`"
            :disabled="!issue.target" @click="focusValidationIssue(issue)">
            <span class="material-symbols-outlined">{{ issue.severity === 'warning' ? 'warning' : 'error' }}</span>
            <span>{{ issue.message }}</span>
            <span v-if="issue.target" class="material-symbols-outlined">arrow_forward</span>
          </button>
        </div>
        <p v-else-if="checkerError" class="flow-validation-service-error">在线检查暂不可用：{{ checkerError }}。保存时仍会由后台校验。</p>
        <p v-else-if="!checkingRule">修改只会在保存后应用到引擎。</p>
      </div>
    </footer>
    </div>

    <Transition name="node-inspector-slide" mode="out-in">
        <aside v-if="selectedNodeId && editorMode === 'canvas'" :key="selectedNodeId" class="node-inspector" role="complementary" aria-label="节点设置" @pointerdown.stop>
          <header class="node-inspector-head">
            <span class="material-symbols-outlined">
              {{ selectedKind === 'invalid' ? 'error' : selectedKind === 'condition' ? 'alt_route' : selectedKind === 'trigger' ? 'bolt' : selectedKind.includes('precondition') ? 'verified_user' : selectedKind === 'failure-action' ? 'build' : selectedKind === 'action' ? 'play_arrow' : 'add' }}
            </span>
            <div><small>节点设置</small><h2>
              {{ selectedKind === 'invalid' ? '无效条件' : selectedKind === 'condition' ? '逻辑组' : selectedKind === 'trigger' ? '触发条件' : selectedKind === 'precondition' ? '开始前确认' : selectedKind === 'failure-action' ? `补救动作 ${selectedIndex + 1}` : selectedKind === 'action' ? `动作 ${selectedIndex + 1}` : selectedKind === 'add-precondition' ? '添加确认' : '添加动作' }}
            </h2></div>
            <button class="icon-btn node-inspector-close" title="关闭设置" @click="selectedNodeId = null"><span class="material-symbols-outlined">close</span></button>
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
                <button class="btn btn-tonal btn-sm" :disabled="!triggerKeys.length" @click="requestConditionChild(selectedGraphNode, false)"><span class="material-symbols-outlined">add</span>添加条件</button>
                <button class="btn btn-tonal btn-sm" :disabled="!triggerKeys.length" @click="requestConditionChild(selectedGraphNode, true)"><span class="material-symbols-outlined">account_tree</span>添加子组</button>
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
                <div class="field"><span class="field-label">触发方式</span>
                  <button class="plugin-type-button" type="button"
                    @click="openPluginPicker('trigger', { mode: 'replace-condition-trigger', path: selectedConditionPath, title: '更换触发方式' })">
                    <span class="material-symbols-outlined">bolt</span>
                    <span>{{ eventName(selectedConditionNode) }}</span>
                    <span class="material-symbols-outlined">arrow_forward</span>
                  </button>
                </div>
                <div class="param-grid"><ParamInput v-for="param in eventParams(selectedConditionNode)" :key="param.name" :def="param" :plugin-id="selectedConditionNode.type" v-model="selectedConditionNode.params[param.name]" /></div>
                <div class="inspector-action-row">
                  <button class="btn btn-text btn-sm" @click="moveSelectedCondition(-1)"><span class="material-symbols-outlined">arrow_upward</span>上移</button>
                  <button class="btn btn-text btn-sm" @click="moveSelectedCondition(1)"><span class="material-symbols-outlined">arrow_downward</span>下移</button>
                  <button class="btn btn-text btn-sm" @click="duplicateSelectedCondition"><span class="material-symbols-outlined">content_copy</span>复制</button>
                  <button class="btn btn-text btn-sm danger-text" @click="removeSelectedCondition"><span class="material-symbols-outlined">delete</span>删除</button>
                </div>
              </template>
              <template v-else-if="rule.event">
                <div class="field"><span class="field-label">触发方式</span>
                  <button class="plugin-type-button" type="button"
                    @click="openPluginPicker('trigger', { mode: 'replace-trigger', title: '更换触发方式' })">
                    <span class="material-symbols-outlined">bolt</span>
                    <span>{{ eventName(rule.event) }}</span>
                    <span class="material-symbols-outlined">arrow_forward</span>
                  </button>
                </div>
                <div class="param-grid"><ParamInput v-for="param in eventParams(rule.event)" :key="param.name" :def="param" :plugin-id="rule.event.type" v-model="rule.event.params[param.name]" /></div>
                <div class="inspector-upgrade-grid">
                  <button class="btn btn-tonal btn-sm" @click="openPluginPicker('trigger', { mode: 'upgrade', op: 'all', title: '添加“并且”条件' })"><span class="material-symbols-outlined">done_all</span>并且满足</button>
                  <button class="btn btn-tonal btn-sm" @click="openPluginPicker('trigger', { mode: 'upgrade', op: 'any', title: '添加“或者”条件' })"><span class="material-symbols-outlined">alt_route</span>或者满足</button>
                </div>
              </template>
              <template v-else>
                <p class="inspector-lead">选择一个事件作为流程起点。</p>
                <button class="btn btn-filled inspector-primary" @click="openPluginPicker('trigger', { mode: 'set-trigger', title: '选择触发方式' })">选择触发方式</button>
              </template>
            </template>

            <template v-else-if="selectedKind === 'precondition' && selectedPrecondition">
              <p class="inspector-lead">确认未通过时，引擎会稍后重试。</p>
              <label class="field"><span class="field-label">确认方式</span>
                <select class="select" :value="selectedPrecondition.type" @change="changePrecondition(selectedPrecondition, $event.target.value)">
                  <option v-for="key in preconditionKeys" :key="key" :value="key">{{ store.schema.actions[key].name || key }}</option>
                </select>
              </label>
              <div class="param-grid"><ParamInput v-for="param in actionParams(selectedPrecondition)" :key="param.name" :def="param" :plugin-id="selectedPrecondition.type" v-model="selectedPrecondition.params[param.name]" allow-binding :binding-sources="preconditionBindingSources()" /></div>
              <button class="btn btn-text btn-sm danger-text inspector-switch" @click="removePrecondition(selectedIndex)"><span class="material-symbols-outlined">delete</span>删除确认</button>
            </template>

            <template v-else-if="selectedKind === 'action' && selectedAction">
              <div class="field"><span class="field-label">动作类型</span>
                <button class="plugin-type-button" type="button"
                  @click="openPluginPicker('action', { mode: 'replace-action', index: selectedIndex, title: '更换动作类型' })">
                  <span class="material-symbols-outlined">play_arrow</span>
                  <span>{{ actionName(selectedAction) }}</span>
                  <span class="material-symbols-outlined">arrow_forward</span>
                </button>
              </div>
              <div class="param-grid"><ParamInput v-for="param in actionParams(selectedAction)" :key="param.name" :def="param" :plugin-id="selectedAction.type" v-model="selectedAction.params[param.name]" allow-binding :binding-sources="actionBindingSources(selectedIndex)" /></div>
              <div v-if="actionOutputHint(selectedAction, selectedIndex)" class="workflow-output-hint">后续步骤可引用：<code>{{ actionOutputHint(selectedAction, selectedIndex) }}</code></div>
              <ActionFailureSettings :action="selectedAction" :meta="store.schema.actions[selectedAction.type]" :schema="store.schema.actions"
                :binding-sources="failureIndex => failureActionBindingSources(selectedIndex, failureIndex)"
                @add-failure-action="requestAddFailureAction(selectedIndex)"
                @replace-failure-action="failureIndex => requestReplaceFailureAction(selectedIndex, failureIndex)"
                @remove-failure-action="failureIndex => removeFailureAction(selectedIndex, failureIndex)"
                @move-failure-action="(failureIndex, offset) => moveFailureAction(selectedIndex, failureIndex, offset)" />
              <div class="inspector-action-row">
                <button class="btn btn-text btn-sm" :disabled="selectedIndex === 0" @click="moveAction(selectedIndex, -1)"><span class="material-symbols-outlined">arrow_back</span>提前</button>
                <button class="btn btn-text btn-sm" :disabled="selectedIndex === rule.actions.length - 1" @click="moveAction(selectedIndex, 1)">稍后<span class="material-symbols-outlined">arrow_forward</span></button>
                <button class="btn btn-text btn-sm" @click="duplicateAction(selectedIndex)"><span class="material-symbols-outlined">content_copy</span>复制</button>
                <button class="btn btn-text btn-sm danger-text" @click="removeAction(selectedIndex)"><span class="material-symbols-outlined">delete</span>删除</button>
              </div>
            </template>

            <template v-else-if="selectedKind === 'failure-action' && selectedFailureAction">
              <p class="inspector-lead">这个动作只会在上方主动作最终失败时执行。</p>
              <div class="field"><span class="field-label">补救动作类型</span>
                <button class="plugin-type-button" type="button"
                  @click="requestReplaceFailureAction(selectedGraphNode.parentIndex, selectedIndex)">
                  <span class="material-symbols-outlined">build</span>
                  <span>{{ actionName(selectedFailureAction) }}</span>
                  <span class="material-symbols-outlined">arrow_forward</span>
                </button>
              </div>
              <div class="param-grid"><ParamInput v-for="param in actionParams(selectedFailureAction)" :key="param.name" :def="param" :plugin-id="selectedFailureAction.type" v-model="selectedFailureAction.params[param.name]" allow-binding :binding-sources="failureActionBindingSources(selectedGraphNode.parentIndex, selectedIndex)" /></div>
              <p class="failure-action-note">{{ selectedFailureAction.on_error === 'continue' ? '如果它也失败，会继续执行剩余补救动作。' : '如果它也失败，会停止剩余补救动作。' }}</p>
              <button class="btn btn-tonal btn-sm" @click="selectedNodeId = `action-${rule.actions[selectedGraphNode.parentIndex].binding_id}`"><span class="material-symbols-outlined">tune</span>设置重试和失败处理</button>
              <div class="inspector-action-row">
                <button class="btn btn-text btn-sm" :disabled="selectedIndex === 0" @click="moveFailureAction(selectedGraphNode.parentIndex, selectedIndex, -1)"><span class="material-symbols-outlined">arrow_back</span>提前</button>
                <button class="btn btn-text btn-sm" :disabled="selectedIndex === rule.actions[selectedGraphNode.parentIndex].failure_actions.length - 1" @click="moveFailureAction(selectedGraphNode.parentIndex, selectedIndex, 1)">稍后<span class="material-symbols-outlined">arrow_forward</span></button>
                <button class="btn btn-text btn-sm danger-text" @click="removeFailureAction(selectedGraphNode.parentIndex, selectedIndex)"><span class="material-symbols-outlined">delete</span>删除</button>
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
        </Transition>

    <Transition name="ai-panel-slide">
      <aside v-if="aiEnabled && aiPanelOpen" class="ai-editor-panel" :style="{ width: aiPanelWidth + 'px' }"
        role="complementary" aria-label="AI 助手">
        <div class="ai-panel-resize" title="拖动调整宽度" @pointerdown="startAiPanelResize"></div>
        <header class="ai-editor-panel-head">
          <span class="material-symbols-outlined ai-panel-head-icon">auto_awesome</span>
          <b>AI 助手</b>
          <div class="flex-1"></div>
          <button class="icon-btn ai-panel-head-btn" title="新对话" @click="newAiConversation"><span class="material-symbols-outlined">refresh</span></button>
          <button class="icon-btn ai-panel-head-btn" title="更多" @click="aiMenuOpen = !aiMenuOpen"><span class="material-symbols-outlined">more_vert</span></button>
          <button class="icon-btn ai-panel-head-btn" title="关闭" @click="toggleAiPanel"><span class="material-symbols-outlined">close</span></button>
        </header>
        <Transition name="ai-msg">
          <div v-if="aiMenuOpen" class="ai-panel-menu-backdrop" @click="aiMenuOpen = false"></div>
        </Transition>
        <div v-if="aiMenuOpen" class="ai-panel-menu">
          <button type="button" @click="openAiSettings"><span class="material-symbols-outlined">settings</span>AI 设置</button>
          <button type="button" @click="downloadAiConversation"><span class="material-symbols-outlined">download</span>下载对话记录</button>
        </div>
        <div class="ai-editor-panel-body">
          <NaturalDraftPanel ref="aiPanelRef" :context-rule="aiContextRule" :full-rule="rule" @create="onAiDraft" @highlight-node="highlightNodeById" />
        </div>
      </aside>
    </Transition>

    </div>

    <PluginPicker :open="picker.open" :kind="picker.kind"
      :keys="picker.kind === 'trigger' ? triggerKeys : picker.kind === 'precondition' ? preconditionKeys : actionKeys"
      :groups="picker.kind === 'trigger' ? triggerGroups : picker.kind === 'precondition' ? preconditionGroups : actionGroups"
      :title="picker.title" @close="closePluginPicker" @select="choosePlugin" />
    <FolderPicker :open="folderPickerOpen" :folders="folders" :current="currentFolderName"
      @close="folderPickerOpen = false" @select="chooseFolder" />
    <DesktopRecorderDialog :open="desktopRecorderOpen"
      :plugin-id="desktopRecorder?.plugin_id" :component-id="desktopRecorder?.id || 'record'"
      @close="desktopRecorderOpen = false" @insert="insertRecordedSteps" />
  </section>
</template>
