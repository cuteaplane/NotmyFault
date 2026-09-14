<script setup>
import { computed, nextTick, ref, toRef, watch, onMounted, onUnmounted } from 'vue'
import RuleCanvas from './RuleCanvas.vue'
import RuleNodeInspector from './RuleNodeInspector.vue'
import RuleValidationSummary from './RuleValidationSummary.vue'
import { isAdmin, eventName, actionName } from '../lib/rulePresentation'
import { useRuleMutations } from '../composables/useRuleMutations'
import { store } from '../lib/store'
import { groupActionKeys, groupTriggerKeys, isConditionLeaf, normalizeRuleDraft, pluginUnavailableReason as getPluginUnavailableReason } from '../lib/utils'

import { ensureRuleBindingIds, variableBindingSources, expandBindingSources } from '../lib/bindings'

import ConditionEditor from './ConditionEditor.vue'
import ActionForm from './ActionForm.vue'
import TriggerForm from './TriggerForm.vue'
import VariablesEditor from './VariablesEditor.vue'
import PluginPicker from './PluginPicker.vue'
import FolderPicker from './FolderPicker.vue'
import NaturalDraftPanel from './NaturalDraftPanel.vue'
import { useRuleValidation } from '../composables/useRuleValidation'

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
  const nodes = 1 + (rule.actions?.length || 0)
  return { name: rule.name || '未命名规则', nodes }
})

const folderPickerOpen = ref(false)
const editingRuleName = ref(false)
const ruleNameInput = ref(null)

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
const concurrencyMode = computed(() => props.rule.concurrency?.mode || 'parallel')
function setConcurrencyMode(mode) {
  if (mode === 'parallel') delete props.rule.concurrency
  else props.rule.concurrency = { ...(props.rule.concurrency || {}), mode }
}
// 平台不匹配、缺能力或被用户禁用都算不可选，原因展示给已引用它的规则
function pluginUnavailableReason(kind, type) {
  return getPluginUnavailableReason(store.schema[kind]?.[type])
}
const triggerKeys = computed(() => Object.keys(store.schema.triggers).filter(
  key => !pluginUnavailableReason('triggers', key)
))
const triggerGroups = computed(() => groupTriggerKeys(triggerKeys.value))
const actionKeys = computed(() => Object.keys(store.schema.actions).filter(
  key => !pluginUnavailableReason('actions', key)
))
const actionGroups = computed(() => groupActionKeys(actionKeys.value))
normalizeRuleDraft(props.rule)
ensureRuleBindingIds(props.rule)
const conditionNode = computed(() => props.rule.condition || null)
const rootIsGroup = computed(() => !!conditionNode.value && !isConditionLeaf(conditionNode.value))

const constantSources = computed(() => expandBindingSources(variableBindingSources(props.rule, { constantsOnly: true }), store.schema.data_types?.custom))

function actionFailureSummary(action) {
  const parts = [action?.on_error === 'continue' ? '失败后继续' : '失败后停止']
  const retries = Math.min(Math.max(Number(action?.retry || 0), 0), 3)
  if (retries) parts.push(`最多重试 ${retries} 次`)
  if (action?.timeout_seconds) parts.push(`最多运行 ${action.timeout_seconds} 秒`)
  if (action?.failure_actions?.length) parts.push(`${action.failure_actions.length} 个补救动作`)
  return parts.join(' · ')
}

const canvasRef = ref(null)
const layoutNodes = computed(() => canvasRef.value?.nodes || [])
const selectedGraphNode = computed(() => layoutNodes.value.find(node => node.id === selectedNodeId.value) || null)

const mutations = useRuleMutations(toRef(props, 'rule'), selectedNodeId, selectedGraphNode)
const {
  useSingleCondition, wrapConditionInNot, conditionId, addAssignment, addIf, removeAction, moveAction,
  removeFailureAction, moveFailureAction, requestAddFailureAction, requestReplaceFailureAction,
  openPluginPicker, closePluginPicker, choosePlugin, requestConditionChild, picker,
} = mutations

const { validationIssues, validationErrorCount, validationWarningCount, checkingRule, checkerError, runRuleCheck } = useRuleValidation(toRef(props, 'rule'))

function focusValidationIssue(issue) {
  if (issue.target === 'name') { startRuleNameEdit(); return }
  editorMode.value = 'canvas'
  nextTick(() => {
    if (issue.target === 'trigger') selectNode(conditionId([]))
    else if (issue.target === 'actions') selectNode('add-action')
    else if (issue.target?.startsWith('action:')) {
      const action = props.rule.actions?.[Number(issue.target.split(':')[1])]
      if (action) selectNode(`action-${action.binding_id}`)

    }
  })
}

function saveFromEditor(event, run = false) {
  const root = event.currentTarget.closest('.rule-editor-page')
  if (root && [...root.querySelectorAll('input,textarea,select')].some(control => !control.reportValidity())) return
  emit(run ? 'save-run' : 'save')
}

function selectNode(id) {

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
watch([() => props.initialNodeId, canvasRef], ([id]) => focusInitialNode(id), { immediate: true })

watch(editorMode, mode => {
  try { localStorage.setItem('notmyfault.ruleEditorMode', mode) } catch {}
})

onMounted(() => {
  window.addEventListener('keydown', onEditorKeydown)
  aiPanelOpen.value = store.pendingAiPanel === true
  store.pendingAiPanel = false
  let savedWidth = 0
  try { savedWidth = Number(localStorage.getItem('notmyfault.aiPanelWidth')) }
  catch { savedWidth = 0 }
  if (savedWidth >= AI_PANEL_MIN_WIDTH && savedWidth <= AI_PANEL_MAX_WIDTH) {
    aiPanelWidth.value = savedWidth
  }
  wideLayoutMedia = window.matchMedia('(min-width: 1360px)')
  wideLayout.value = wideLayoutMedia.matches
  wideLayoutMedia.addEventListener?.('change', syncWideLayout)
})
onUnmounted(() => {
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
          title="保存当前修改，并真实执行一次规则中的动作" @click="saveFromEditor($event, true)">
          <span v-if="testing" class="spinner"></span>
          <span v-else class="material-symbols-outlined">experiment</span>
          {{ testing ? '测试中…' : '测试规则' }}
        </button>
        <button class="btn btn-filled" :disabled="validationErrorCount || !dirty" @click="saveFromEditor($event)"><span class="material-symbols-outlined">save</span>保存规则</button>
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

    <VariablesEditor :rule="rule" />
    <div class="rule-editor-main-grid">
    <div class="editor-canvas-column">
    <RuleCanvas ref="canvasRef" :rule="rule" :active="editorMode === 'canvas'"
      v-model:selected-node-id="selectedNodeId" @select-node="selectNode"
      @add-if="addIf" @add-assignment="addAssignment" @open-picker="openPluginPicker"
      @move-action="moveAction" @remove-action="removeAction"
      @move-failure-action="moveFailureAction" @remove-failure-action="removeFailureAction"
      @add-condition-child="requestConditionChild" />

    <main v-show="editorMode === 'form'" class="automation-flow classic-rule-editor" :class="{ 'editor-mode-active': editorMode === 'form' }">
      <section class="automation-stage stage-when">
        <div class="stage-rail"><span class="stage-node"><span class="material-symbols-outlined">bolt</span></span></div>
        <div class="stage-content">
          <header class="stage-head"><div><span class="stage-kicker">当</span><h2>什么情况会触发这条规则？</h2></div><span class="stage-required">必填</span></header>

          <ConditionEditor v-if="rootIsGroup" :node="conditionNode" :constants="constantSources" />
          <details v-else-if="conditionNode" class="flow-card condition-flow-card" open>
            <summary>
              <span class="flow-card-index">1</span>
              <span class="flow-card-copy"><b>{{ eventName(conditionNode) }}</b><small>唯一触发条件</small></span>
              <span v-if="isAdmin(store.schema.triggers[conditionNode.type])" class="chip chip-admin">管理员</span>
              <span class="material-symbols-outlined flow-expand">expand_more</span>
            </summary>
            <div class="flow-card-body">
              <TriggerForm :node="conditionNode" :sources="constantSources"
                @replace="openPluginPicker('trigger', { mode: 'replace-condition-trigger', path: [], title: '更换触发方式' })" />
            </div>
          </details>
          <div v-else class="flow-empty-card">
            <span class="material-symbols-outlined">touch_app</span>
            <div><b>选择触发方式</b><p>先说明什么时候开始执行，不默认替你选择。</p></div>
            <button class="btn btn-filled" @click="openPluginPicker('trigger', { mode: 'set-trigger', title: '选择触发方式' })">选择</button>
          </div>
          <div class="flow-stage-actions">
            <button v-if="rootIsGroup" class="btn btn-text btn-sm" @click="useSingleCondition"><span class="material-symbols-outlined">filter_1</span>改为单个条件</button>
            <template v-if="conditionNode && !rootIsGroup">
              <button class="btn btn-text btn-sm" @click="openPluginPicker('trigger', { mode: 'combine-root', op: 'all', title: '添加“并且”条件' })"><span class="material-symbols-outlined">done_all</span>并且满足</button>
              <button class="btn btn-text btn-sm" @click="openPluginPicker('trigger', { mode: 'combine-root', op: 'any', title: '添加“或者”条件' })"><span class="material-symbols-outlined">alt_route</span>或者满足</button>
            </template>
            <label class="field rule-concurrency-field"><span class="field-label">重复触发</span>
              <select class="select" :value="concurrencyMode" @change="setConcurrencyMode($event.target.value)">
                <option value="parallel">同时运行（默认）</option>
                <option value="single">运行中忽略新触发</option>
                <option value="queue">排队依次执行</option>
                <option value="replace">取消旧的执行最新的</option>
              </select>
            </label>
          </div>
        </div>
      </section>

      <section class="automation-stage stage-then">
        <div class="stage-rail stage-rail-last"><span class="stage-node"><span class="material-symbols-outlined">play_arrow</span></span></div>
        <div class="stage-content">
          <header class="stage-head"><div><span class="stage-kicker">然后</span><h2>按顺序执行这些动作</h2></div><div class="stage-head-tools"><span class="stage-required">必填</span></div></header>
          <details v-for="(action, index) in rule.actions" :key="action" class="flow-card action-flow-card" :open="rule.actions.length === 1">
            <summary>
              <span class="flow-card-index">{{ index + 1 }}</span><span class="flow-card-copy"><b>{{ actionName(action) }}</b><small>第 {{ index + 1 }} 步 · {{ actionFailureSummary(action) }}</small></span>
              <span v-if="isAdmin(store.schema.actions[action.type])" class="chip chip-admin">管理员</span>
              <span class="flow-card-tools"><button class="icon-btn" :disabled="index === 0" title="上移" @click.prevent.stop="moveAction(index, -1)"><span class="material-symbols-outlined">arrow_upward</span></button><button class="icon-btn" :disabled="index === rule.actions.length - 1" title="下移" @click.prevent.stop="moveAction(index, 1)"><span class="material-symbols-outlined">arrow_downward</span></button><button class="icon-btn icon-btn-danger" title="移除动作" @click.prevent.stop="removeAction(index)"><span class="material-symbols-outlined">delete</span></button></span>
              <span class="material-symbols-outlined flow-expand">expand_more</span>
            </summary>
            <div class="flow-card-body">
              <ActionForm :action="action" :rule="rule" :index="index"
                @replace="openPluginPicker('action', { mode: 'replace-action', index: index, title: '更换动作类型' })"
                @add-failure-action="requestAddFailureAction(index)"
                @replace-failure-action="failureIndex => requestReplaceFailureAction(index, failureIndex)"
                @remove-failure-action="failureIndex => removeFailureAction(index, failureIndex)"
                @move-failure-action="(failureIndex, offset) => moveFailureAction(index, failureIndex, offset)" />
            </div>
          </details>
          <div v-if="!rule.actions?.length" class="flow-inline-empty">还没有动作。规则触发后不会执行任何操作。</div>
          <div class="flow-add-control"><button class="btn btn-tonal" @click="openPluginPicker('action', { mode: 'action', index: rule.actions?.length || 0, title: '添加动作' })"><span class="material-symbols-outlined">add</span>添加动作</button><button class="btn btn-text" @click="addIf">添加 IF</button><button class="btn btn-text" @click="addAssignment">变量赋值</button></div>
        </div>
      </section>
    </main>

    <div v-if="rule.preconditions?.length" class="flow-inline-empty">旧的运行前检查已停用。请配置 NOT 或 IF 后移除旧检查。
      <button class="btn btn-text danger-text" @click="delete rule.preconditions">移除旧检查</button>
    </div>
    <RuleValidationSummary :validation-issues="validationIssues" :validation-error-count="validationErrorCount"
      :validation-warning-count="validationWarningCount" :checking-rule="checkingRule" :checker-error="checkerError"
      @focus-issue="focusValidationIssue" />
    </div>

    <Transition name="node-inspector-slide" mode="out-in">
      <RuleNodeInspector v-if="selectedNodeId && editorMode === 'canvas'" :key="selectedNodeId"
        :rule="rule" :node="selectedGraphNode" :mutations="mutations" :constant-sources="constantSources"
        :has-triggers="!!triggerKeys.length" @select-node="selectNode" />
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
      :keys="picker.kind === 'trigger' ? triggerKeys : actionKeys"
      :groups="picker.kind === 'trigger' ? triggerGroups : actionGroups"
      :title="picker.title" @close="closePluginPicker" @select="choosePlugin" />
    <FolderPicker :open="folderPickerOpen" :folders="folders" :current="currentFolderName"
      @close="folderPickerOpen = false" @select="chooseFolder" />
  </section>
</template>
