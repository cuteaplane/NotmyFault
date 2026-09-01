<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { store } from '../../lib/store'
import { cancelRun, runRule } from '../../lib/api'
import { approveRuleBeforeEditing, saveRulesWithApproval } from '../../lib/ruleSave'
import { snackbar } from '../../lib/notify'
import { alertDialog, confirmDialog } from '../../lib/dialog'
import { normalizeRuleDraft } from '../../lib/utils'
import { buildAllTestInputFields, ensureRuleBindingIds } from '../../lib/bindings'
import { summarizeRuleChanges } from '../../lib/ruleDiff'
import { automationTemplates, createQuickDraft, instantiateTemplate, templateAvailability } from '../../lib/automationTemplates'
import RuleEditor from '../RuleEditor.vue'
import QuickCreateDialog from '../QuickCreateDialog.vue'
import TestRunDialog from '../TestRunDialog.vue'
import BaseDialog from '../BaseDialog.vue'

const activeRuleIndex = ref(null)
const draftRule = ref(null)
const baseline = ref('')
const pendingRuleIndex = ref(null)
const activeManualRun = ref(store.activeManualRun)
const runningRuleIndex = computed(() => pendingRuleIndex.value ?? activeManualRun.value?.index ?? null)
watch(() => store.activeManualRun, run => { activeManualRun.value = run })
const testPreparation = ref(null)
const pendingEditorNodeId = ref('')
const activeRule = computed(() => draftRule.value)
const isDirty = computed(() => !!draftRule.value && JSON.stringify(draftRule.value) !== baseline.value)
const baselineRule = computed(() => {
  try { return baseline.value ? JSON.parse(baseline.value) : null }
  catch { return null }
})
const draftChangeSummary = computed(() => summarizeRuleChanges(baselineRule.value, draftRule.value))
const draftChangeText = computed(() => draftChangeSummary.value.join('、') || '规则内容已修改')
const isTestingActiveRule = computed(() => runningRuleIndex.value !== null)
const draftHistory = ref([])
const draftHistoryIndex = ref(-1)
const draftHistoryPending = ref(false)
const canUndoDraft = computed(() => draftHistoryPending.value || draftHistoryIndex.value > 0)
const canRedoDraft = computed(() => !draftHistoryPending.value && draftHistoryIndex.value >= 0 && draftHistoryIndex.value < draftHistory.value.length - 1)
let draftHistoryTimer = null
let applyingDraftHistory = false
const DRAFT_RECOVERY_KEY = 'notmyfault.ruleDraft.v1'
const DRAFT_RECOVERY_VERSION = 2
const TEST_DATA_KEY = 'notmyfault.ruleTestData.v1'

const showCreatePanel = ref(false)
const showQuickCreate = ref(false)
const quickCreateReturnFocus = ref(null)
const createPanelVisible = computed(() => (
  store.aiDrafting.enabled && (!store.configData.rules.length || showCreatePanel.value)
))

const ruleFolders = computed(() => {
  const folders = new Map()
  store.configData.rules.forEach((rule, index) => {
    const name = String(rule.folder || '未分类')
    if (!folders.has(name)) folders.set(name, [])
    folders.get(name).push({ rule, index })
  })
  return [...folders]
})
const availableFolders = computed(() => [...new Set(
  store.configData.rules
    .map(rule => String(rule.folder || '').trim())
    .filter(name => name && name !== '未分类'),
)].sort((a, b) => a.localeCompare(b, 'zh-CN')))

function clone(value) { return JSON.parse(JSON.stringify(value)) }
function visitRuleNodes(rule, visitor) {
  const visitAction = node => {
    if (!node || typeof node !== 'object') return
    visitor(node, 'action')
    ;(node.failure_actions || []).forEach(visitAction)
  }
  const visitCondition = node => {
    if (!node || typeof node !== 'object') return
    if (node.type) visitor(node, 'trigger')
    ;(node.children || []).forEach(visitCondition)
  }
  if (rule?.event) visitor(rule.event, 'trigger')
  visitCondition(rule?.condition)
  ;(rule?.preconditions || []).forEach(visitAction)
  ;(rule?.actions || []).forEach(visitAction)
}
function sensitiveParamNames(node, kind) {
  const catalog = kind === 'trigger' ? store.schema.triggers : store.schema.actions
  return (catalog?.[node?.type]?.params || [])
    .filter(param => param.sensitive)
    .map(param => param.name)
}
function sanitizedRuleForRecovery(rule) {
  const sanitized = clone(rule)
  visitRuleNodes(sanitized, (node, kind) => {
    if (!node.params || typeof node.params !== 'object') return
    for (const name of sensitiveParamNames(node, kind)) delete node.params[name]
  })
  return sanitized
}
function restoreSensitiveParams(targetRule, currentRule) {
  const currentNodes = new Map()
  visitRuleNodes(currentRule, (node, kind) => {
    if (node.binding_id) currentNodes.set(`${kind}:${node.binding_id}`, node)
  })
  visitRuleNodes(targetRule, (node, kind) => {
    const current = node.binding_id ? currentNodes.get(`${kind}:${node.binding_id}`) : null
    if (!current || current.type !== node.type) return
    for (const name of sensitiveParamNames(node, kind)) {
      if (!Object.prototype.hasOwnProperty.call(current.params || {}, name)) continue
      if (!node.params || typeof node.params !== 'object') node.params = {}
      node.params[name] = clone(current.params[name])
    }
  })
}
function recoveryBaseline() {
  const rule = baselineRule.value
  return rule ? JSON.stringify(sanitizedRuleForRecovery(rule)) : ''
}
function readSavedTestData() {
  try {
    const value = JSON.parse(localStorage.getItem(TEST_DATA_KEY) || '{}')
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {}
  } catch { return {} }
}
function savedTestValues(rule, fields) {
  const all = readSavedTestData()
  const saved = all[rule?.rule_id]?.values
  if (!saved || typeof saved !== 'object') return {}
  const safe = Object.fromEntries(fields
    .filter(field => !field.sensitive && Object.prototype.hasOwnProperty.call(saved, field.key))
    .map(field => [field.key, saved[field.key]]))
  if (JSON.stringify(safe) !== JSON.stringify(saved)) {
    all[rule.rule_id] = { ...all[rule.rule_id], values: safe }
    try { localStorage.setItem(TEST_DATA_KEY, JSON.stringify(all)) } catch {}
  }
  return safe
}
function updateSavedTestData(rule, fields, values, remember) {
  if (!rule?.rule_id) return
  try {
    const all = readSavedTestData()
    if (!remember) delete all[rule.rule_id]
    else {
      all[rule.rule_id] = {
        savedAt: Date.now(),
        values: Object.fromEntries(fields
          .filter(field => !field.sensitive && Object.prototype.hasOwnProperty.call(values, field.key))
          .map(field => [field.key, values[field.key]])),
      }
    }
    const recent = Object.entries(all)
      .sort((left, right) => Number(right[1]?.savedAt || 0) - Number(left[1]?.savedAt || 0))
      .slice(0, 50)
    for (const key of Object.keys(all)) delete all[key]
    Object.assign(all, Object.fromEntries(recent))
    localStorage.setItem(TEST_DATA_KEY, JSON.stringify(all))
  } catch {}
}
function readDraftRecovery() {
  try {
    const value = JSON.parse(localStorage.getItem(DRAFT_RECOVERY_KEY) || 'null')
    if (!value || typeof value !== 'object') return null
    if (value.version !== DRAFT_RECOVERY_VERSION) {
      clearDraftRecovery()
      return null
    }
    return value
  } catch { return null }
}
function clearDraftRecovery() {
  try { localStorage.removeItem(DRAFT_RECOVERY_KEY) } catch {}
}
function persistDraftRecovery() {
  if (!draftRule.value || !baseline.value || JSON.stringify(draftRule.value) === baseline.value) {
    clearDraftRecovery()
    return
  }
  try {
    localStorage.setItem(DRAFT_RECOVERY_KEY, JSON.stringify({
      version: DRAFT_RECOVERY_VERSION,
      ruleId: draftRule.value.rule_id,
      isNew: activeRuleIndex.value === -1,
      baseline: recoveryBaseline(),
      draft: sanitizedRuleForRecovery(draftRule.value),
      savedAt: Date.now(),
    }))
  } catch {}
}
async function restoreDraftRecovery(index) {
  const recovery = readDraftRecovery()
  if (!recovery || !recovery.draft) return
  const matches = index === -1
    ? recovery.isNew === true
    : recovery.ruleId === draftRule.value?.rule_id
  if (!matches) return
  const currentRecoveryBaseline = recoveryBaseline()
  if (index !== -1 && recovery.baseline !== currentRecoveryBaseline) {
    clearDraftRecovery()
    return
  }
  if (index !== -1 && JSON.stringify(recovery.draft) === currentRecoveryBaseline) {
    clearDraftRecovery()
    return
  }
  const restore = await confirmDialog(
    '恢复未保存的草稿吗？',
    'NotmyFault 找到了这条规则上次关闭前的修改。',
    '恢复草稿',
  )
  if (restore) {
    const restored = ensureRuleBindingIds(normalizeRuleDraft(clone(recovery.draft)))
    restoreSensitiveParams(restored, draftRule.value)
    draftRule.value = restored
  } else clearDraftRecovery()
}
function resetDraftHistory() {
  if (draftHistoryTimer) { clearTimeout(draftHistoryTimer); draftHistoryTimer = null }
  draftHistoryPending.value = false
  draftHistory.value = draftRule.value ? [JSON.stringify(draftRule.value)] : []
  draftHistoryIndex.value = draftHistory.value.length ? 0 : -1
}
function commitDraftHistory() {
  if (!draftRule.value) return
  if (draftHistoryTimer) { clearTimeout(draftHistoryTimer); draftHistoryTimer = null }
  draftHistoryPending.value = false
  const snapshot = JSON.stringify(draftRule.value)
  if (snapshot === draftHistory.value[draftHistoryIndex.value]) return
  draftHistory.value = draftHistory.value.slice(0, draftHistoryIndex.value + 1)
  draftHistory.value.push(snapshot)
  if (draftHistory.value.length > 50) draftHistory.value.shift()
  draftHistoryIndex.value = draftHistory.value.length - 1
  persistDraftRecovery()
}
function scheduleDraftHistory() {
  if (applyingDraftHistory || !draftRule.value) return
  if (draftHistoryTimer) clearTimeout(draftHistoryTimer)
  draftHistoryPending.value = true
  draftHistoryTimer = setTimeout(commitDraftHistory, 300)
}
function applyDraftHistory(index) {
  const snapshot = draftHistory.value[index]
  if (!snapshot) return
  applyingDraftHistory = true
  draftHistoryPending.value = false
  draftHistoryIndex.value = index
  draftRule.value = JSON.parse(snapshot)
  applyingDraftHistory = false
  persistDraftRecovery()
}
function undoDraft() {
  commitDraftHistory()
  if (draftHistoryIndex.value > 0) applyDraftHistory(draftHistoryIndex.value - 1)
}
function redoDraft() {
  if (draftHistoryTimer) commitDraftHistory()
  if (draftHistoryIndex.value < draftHistory.value.length - 1) applyDraftHistory(draftHistoryIndex.value + 1)
}
async function openRule(index) {
  showCreatePanel.value = false
  activeRuleIndex.value = index
  draftRule.value = ensureRuleBindingIds(
    normalizeRuleDraft(clone(store.configData.rules[index])),
  )
  baseline.value = JSON.stringify(draftRule.value)
  await restoreDraftRecovery(index)
  resetDraftHistory()
}
async function addRule(seed = null) {
  showCreatePanel.value = false
  activeRuleIndex.value = -1
  draftRule.value = seed ? ensureRuleBindingIds(normalizeRuleDraft(clone(seed))) : ensureRuleBindingIds({
    name: '新规则',
    folder: '未分类',
    event: null,
    actions: [],
  })
  baseline.value = JSON.stringify(draftRule.value)
  if (seed) clearDraftRecovery()
  else await restoreDraftRecovery(-1)
  resetDraftHistory()
}
function createAutomation() {
  if (store.aiDrafting.enabled) showCreatePanel.value = true
  else addRule()
}
async function createWithAi() {
  showCreatePanel.value = false
  store.pendingAiPanel = true
  await addRule()
}
async function leaveEditor() {
  if (isDirty.value && !await confirmDialog('要放弃这些修改吗？', `尚未保存：${draftChangeText.value}。`, '放弃')) return
  activeRuleIndex.value = null
  draftRule.value = null
  baseline.value = ''
  clearDraftRecovery()
  resetDraftHistory()
}

function triggerCount(rule) {
  function count(node) {
    if (!node) return 0
    if (node.type && !node.children && !node.events) return 1
    return (node.children || node.events || []).reduce((sum, child) => sum + count(child), 0)
  }
  return rule.condition ? count(rule.condition) : (rule.event ? 1 : 0)
}
function triggerSummary(rule) {
  if (!rule.condition) return store.schema.triggers[rule.event?.type]?.name || rule.event?.type || '未配置触发条件'
  const op = rule.condition.op || (rule.condition.type === 'and' ? 'all' : 'any')
  return `${op === 'all' ? '全部满足' : '满足任一'} · ${triggerCount(rule)} 个条件`
}

async function persistRules(nextRules, successMessage) {
  const result = await saveRulesWithApproval(nextRules)
  if (result?.cancelled) return null
  if (!result?.ok) {
    const details = Array.isArray(result?.details) ? result.details.join(' · ') : ''
    throw new Error([result?.error || '未知错误', details].filter(Boolean).join('：'))
  }
  const savedRules = Array.isArray(result.rules) ? result.rules : nextRules
  store.configData = { ...store.configData, rules: savedRules }
  snackbar(successMessage)
  return savedRules
}
async function doSave(runAfter = false) {
  try {
    const nextRules = clone(store.configData.rules)
    let savedIndex = activeRuleIndex.value
    if (savedIndex === -1) {
      nextRules.unshift(clone(draftRule.value))
      savedIndex = 0
    } else {
      nextRules[savedIndex] = clone(draftRule.value)
    }
    const savedRules = await persistRules(
      nextRules,
      runAfter ? '规则已保存，正在启动测试' : '规则已保存',
    )
    if (!savedRules) return
    activeRuleIndex.value = savedIndex
    draftRule.value = clone(savedRules[savedIndex])
    baseline.value = JSON.stringify(draftRule.value)
    clearDraftRecovery()
    resetDraftHistory()
    if (runAfter) {
      await runManualRule(savedIndex, savedRules[savedIndex])
    }
  } catch (error) {
    alertDialog('保存失败', error.message)
  }
}
async function deleteRule(index) {
  if (!await confirmDialog('删除这条规则吗？', '此操作将在保存后立即生效。', '删除')) return
  try {
    const nextRules = clone(store.configData.rules)
    nextRules.splice(index, 1)
    const savedRules = await persistRules(nextRules, '规则已删除')
    if (!savedRules) return
    if (activeRuleIndex.value === index) leaveEditorAfterDelete()
  } catch (error) {
    alertDialog('删除失败', error.message)
  }
}
async function deleteActiveRule() {
  if (activeRuleIndex.value === -1) { leaveEditorAfterDelete(); return }
  await deleteRule(activeRuleIndex.value)
}
function leaveEditorAfterDelete() {
  activeRuleIndex.value = null
  draftRule.value = null
  baseline.value = ''
  clearDraftRecovery()
  resetDraftHistory()
}
async function runManualRule(index, ruleSnapshot = null) {
  if (runningRuleIndex.value !== null) return
  const snapshot = ruleSnapshot ? clone(ruleSnapshot) : clone(store.configData.rules[index])
  const fields = buildAllTestInputFields(snapshot, store.schema)
  testPreparation.value = {
    index,
    snapshot,
    fields,
    savedValues: savedTestValues(snapshot, fields),
  }
}
async function executeManualRule(index, snapshot, testContext) {
  pendingRuleIndex.value = index
  try {
    let watchFromSeq = latestEngineEventSeq()
    const result = await runRule(index, snapshot, testContext)
    if (result?.code === 'missing_test_context') {
      const fields = buildAllTestInputFields(snapshot, store.schema)
      if (fields.length) {
        testPreparation.value = {
          index,
          snapshot,
          fields,
          savedValues: savedTestValues(snapshot, fields),
        }
        return
      }
    }
    if (result.ok) {
      activeManualRun.value = { index, runId: result.run_id || '', ruleName: snapshot?.name || '' }
      store.activeManualRun = activeManualRun.value
      snackbar(result.message ? `测试已启动：${result.message}` : '规则测试已启动')
      startTestWatch(
        snapshot,
        index,
        Number.isInteger(result.action_count) ? result.action_count : (snapshot?.actions || []).length,
        watchFromSeq,
        result.run_id || '',
      )
    }
    else alertDialog('规则测试失败', result.error || '未知错误')
  } catch (error) {
    alertDialog('规则测试失败', error.message)
  } finally {
    pendingRuleIndex.value = null
  }
}
function cancelTestPreparation() {
  testPreparation.value = null
}
async function submitTestPreparation(payload) {
  const preparation = testPreparation.value
  if (!preparation) return
  updateSavedTestData(
    preparation.snapshot,
    payload.fields,
    payload.values,
    payload.remember,
  )
  testPreparation.value = null
  await executeManualRule(preparation.index, preparation.snapshot, payload.context)
}

const testResult = ref(null)

function latestEngineEventSeq() {
  return store.engineEvents.length
    ? store.engineEvents[store.engineEvents.length - 1].seq : 0
}

function startTestWatch(rule, ruleIndex, expected, lastSeq, runId) {
  if (testResult.value?.timer) clearTimeout(testResult.value.timer)
  testResult.value = {
    ruleName: rule?.name || `规则 #${ruleIndex + 1}`,
    ruleId: rule?.rule_id || '',
    ruleIndex,
    runId,
    expected, steps: [], assertions: null, done: false, note: '', cancelling: false,
    lastSeq, timer: null,
  }
  processTestEvents()
  if (testResult.value.done) return
  testResult.value.timer = setTimeout(() => {
    const t = testResult.value
    if (!t || t.done) return
    t.note = t.steps.length
      ? '这次运行耗时较长，仍在后台执行；可以继续等待或停止测试。'
      : '20 秒内还没收到动作结果；可以继续等待、停止测试或到运行记录查看。'
  }, 20000)
}

function failText(err) {
  if (err && typeof err === 'object') return err.message || err.code || JSON.stringify(err)
  return String(err || '').slice(-300)
}

function processTestEvents() {
  const t = testResult.value
  if (!t || t.done) return
  for (const ev of store.engineEvents) {
    if (ev.seq <= t.lastSeq) continue
    t.lastSeq = ev.seq
    if (t.runId ? ev.data?.run_id !== t.runId : ev.data?.rule_name !== t.ruleName) continue
    if (ev.name === 'action_executed') {
      t.steps.push({
        status: 'ok', type: ev.data.action_type, stepId: ev.data.step_id, detail: '',
        inputSummary: ev.data.input_summary || [],
        outputSummary: ev.data.output_summary || [],
      })
    } else if (ev.name === 'action_skipped') {
      t.steps.push({ status: 'skipped', type: ev.data.action_type, stepId: ev.data.step_id, detail: '引用的运行数据不可用，已跳过' })
    } else if (ev.name === 'action_cancelled') {
      t.steps.push({ status: 'cancelled', type: ev.data.action_type, stepId: ev.data.step_id, detail: failText(ev.data.error) })
    } else if (ev.name === 'action_timed_out') {
      t.steps.push({ status: 'timed_out', type: ev.data.action_type, stepId: ev.data.step_id, detail: failText(ev.data.error) })
    } else if (ev.name === 'workflow_failed') {
      t.steps.push({ status: 'fail', type: ev.data.action_type || '', stepId: ev.data.step_id, detail: failText(ev.data.error), inputSummary: ev.data.input_summary || [], outputSummary: [] })
      finishTestWatch()
    } else if (ev.name === 'error') {
      t.steps.push({ status: 'fail', type: ev.data.action_type || '', stepId: ev.data.step_id, detail: failText(ev.data.error) })
      finishTestWatch()
    } else if (ev.name === 'workflow_deferred') {
      t.steps.push({ status: 'deferred', type: '', detail: ev.data.reason || '前置条件未满足' })
      t.note = '前置条件暂未满足，测试会在条件满足后继续。'
    } else if (ev.name === 'test_assertions_completed') {
      t.assertions = ev.data
    } else if (ev.name === 'workflow_completed') {
      if (ev.data.status === 'cancelled') {
        t.note = '测试已停止。'
      } else if (!t.expected && ev.data.status === 'succeeded') {
        t.note = '这条规则没有动作，测试已验证触发与前置条件链路。'
      }
      finishTestWatch()
    }
  }
}

function finishTestWatch() {
  const t = testResult.value
  if (!t) return
  t.done = true
  if (t.timer) { clearTimeout(t.timer); t.timer = null }
}

function closeTestResult() {
  if (testResult.value?.timer) clearTimeout(testResult.value.timer)
  testResult.value = null
}

async function stopTestRun() {
  const t = testResult.value
  if (!t?.runId || t.done || t.cancelling) return
  t.cancelling = true
  try {
    const result = await cancelRun(t.runId)
    if (!result?.ok) {
      t.cancelling = false
      await alertDialog('无法停止测试', result?.error || '这次运行已经结束或不存在')
      return
    }
    t.note = '正在停止；已经开始的动作会在安全位置结束。'
  } catch (error) {
    t.cancelling = false
    await alertDialog('无法停止测试', error.message)
  }
}

function goLogs() {
  if (testResult.value?.runId) store.pendingRunId = testResult.value.runId
  closeTestResult()
  if (window.__nmf && window.__nmf.switchPage) window.__nmf.switchPage('logs')
}

watch(() => store.engineEvents.length
  ? store.engineEvents[store.engineEvents.length - 1].seq : 0, processTestEvents)
onUnmounted(() => { if (testResult.value?.timer) clearTimeout(testResult.value.timer) })
watch(draftRule, scheduleDraftHistory, { deep: true, flush: 'sync' })
onUnmounted(() => {
  if (draftHistoryTimer) commitDraftHistory()
  else persistDraftRecovery()
})

function stepIcon(status) {
  return ({ ok: 'check_circle', fail: 'error', skipped: 'skip_next', deferred: 'hourglass_empty', cancelled: 'stop_circle', timed_out: 'timer_off' })[status] || 'help'
}
function stepTitle(step) {
  const name = step.type ? (store.schema.actions[step.type]?.name || step.type) : ''
  if (step.status === 'ok') return `${name} 执行成功`
  if (step.status === 'fail') return name ? `${name} 执行失败` : '执行失败'
  if (step.status === 'cancelled') return name ? `${name} 已停止` : '运行已停止'
  if (step.status === 'timed_out') return name ? `${name} 已超时` : '动作已超时'
  if (step.status === 'skipped') return `${name} 被跳过`
  return '前置条件未满足，已推迟'
}
const testSummary = computed(() => {
  const t = testResult.value
  if (!t) return ''
  if (t.note) return t.note
  if (!t.done) return ''
  if (t.steps.some(s => s.status === 'cancelled')) return '测试已停止。'
  if (t.steps.some(s => s.status === 'timed_out')) return '有动作超过允许的运行时间。'
  if (t.steps.some(s => s.status === 'fail')) return '有步骤执行失败，请查看下方详情。'
  if (t.steps.some(s => s.status === 'deferred')) return '前置条件不满足，本次测试没有执行动作。'
  if (t.assertions?.total && t.assertions.passed < t.assertions.total) return `动作已执行，但 ${t.assertions.total - t.assertions.passed} 项检查未通过。`
  if (t.assertions?.total) return `全部 ${t.steps.length} 个动作执行成功，${t.assertions.total} 项检查均通过。`
  return `全部 ${t.steps.length} 个动作执行成功。`
})

const templates = computed(() => automationTemplates.map(template => ({
  ...template,
  availability: templateAvailability(template, store),
})))
async function openCandidateRule(rule) {
  const approval = await approveRuleBeforeEditing(rule)
  if (!approval?.ok) return
  await addRule(rule)
}
async function addTemplate(t) {
  if (!t.availability.available) {
    store.pendingPluginFocus = {
      kind: t.availability.kind,
      id: t.availability.id,
      state: t.availability.state,
      reason: t.availability.reason,
    }
    window.__nmf?.switchPage?.('plugins')
    return
  }
  await openCandidateRule(instantiateTemplate(t))
}
function openQuickCreate(event) {
  quickCreateReturnFocus.value = event?.currentTarget || null
  showQuickCreate.value = true
}
async function closeQuickCreate() {
  showQuickCreate.value = false
  await nextTick()
  quickCreateReturnFocus.value?.focus()
}
async function createQuickAutomation(selection) {
  showQuickCreate.value = false
  await openCandidateRule(createQuickDraft(selection.triggerType, selection.actionType, store.schema))
}
async function createNaturalDraft(draft) {
  await openCandidateRule(draft)
}
async function openTestStep(step) {
  const result = testResult.value
  if (!step?.stepId || result?.ruleIndex === undefined) return
  const ruleIndex = result.ruleIndex
  pendingEditorNodeId.value = `action-${step.stepId}`
  closeTestResult()
  await openRule(ruleIndex)
}

onMounted(() => {
  if (!store.configData.rules) store.configData.rules = []
  if (store.pendingRuleDraft) {
    const draft = clone(store.pendingRuleDraft)
    store.pendingRuleDraft = null
    addRule(draft)
    return
  }
  if (store.pendingAutomationCreate) {
    store.pendingAutomationCreate = false
    createAutomation()
  }
  // 起源彩蛋指定了待打开的规则，按名字定位后打开编辑器，用完清掉。
  if (store.pendingRuleId || store.pendingRuleName) {
    const index = store.pendingRuleId
      ? store.configData.rules.findIndex(r => r.rule_id === store.pendingRuleId)
      : store.configData.rules.findIndex(r => r.name === store.pendingRuleName)
    const stepId = store.pendingStepId
    store.pendingRuleId = ''
    store.pendingRuleName = ''
    store.pendingStepId = ''
    if (index >= 0) {
      pendingEditorNodeId.value = stepId ? `action-${stepId}` : ''
      openRule(index)
    }
  }
})
</script>

<template>
  <!-- 外层套普通容器，避免本视图的切换动画与 App 的页面过渡叠在同一个元素上。 -->
  <div class="rules-view">
  <Transition name="rule-route" mode="out-in">
  <RuleEditor v-if="activeRule" key="editor" :rule="activeRule" :dirty="isDirty"
    :testing="isTestingActiveRule" :folders="availableFolders"
    :change-summary="draftChangeSummary"
    :initial-node-id="pendingEditorNodeId"
    :can-undo="canUndoDraft" :can-redo="canRedoDraft"
    @back="leaveEditor" @delete="deleteActiveRule" @save="doSave(false)" @save-run="doSave(true)"
    @undo="undoDraft" @redo="redoDraft" @ai-draft="createNaturalDraft" />

  <section v-else key="library" class="page active rules-library">
    <div class="page-head">
      <div><h2>自动化</h2><p class="page-subtitle">创建、测试和管理这台电脑上的自动化。</p></div>
      <div class="actions">
        <button class="btn btn-filled" @click="createAutomation"><span class="material-symbols-outlined">add</span>创建自动化</button>
      </div>
    </div>
    <section v-if="createPanelVisible" class="automation-create-panel">
      <header class="automation-create-head">
        <div>
          <small>创建自动化</small>
          <h3>从想完成的事开始</h3>
          <p>选择一个常见用途，或者先指定什么时候开始、接着做什么。</p>
        </div>
        <div class="actions">
          <button v-if="store.aiDrafting.enabled" class="btn btn-tonal" @click="createWithAi"><span class="material-symbols-outlined">auto_awesome</span>AI 起草</button>
          <button class="btn btn-text" @click="addRule"><span class="material-symbols-outlined">edit_note</span>空白规则</button>
          <button class="btn btn-tonal" @click="openQuickCreate"><span class="material-symbols-outlined">account_tree</span>自己搭一个</button>
          <button v-if="store.configData.rules.length" class="icon-btn" title="收起创建区" @click="showCreatePanel = false"><span class="material-symbols-outlined">close</span></button>
        </div>
      </header>
      <div class="automation-template-grid">
        <button v-for="t in templates" :key="t.id" class="automation-template" :class="{ unavailable: !t.availability.available }" @click="addTemplate(t)">
          <span class="automation-template-icon material-symbols-outlined">{{ t.icon }}</span>
          <span class="automation-template-copy">
            <b>{{ t.title }}</b>
            <small>{{ t.description }}</small>
            <em :class="{ ready: t.availability.available }"><span class="material-symbols-outlined">{{ t.availability.available ? 'check_circle' : 'extension_off' }}</span>{{ t.availability.reason }}</em>
          </span>
          <span class="material-symbols-outlined automation-template-go">{{ t.availability.available ? 'arrow_forward' : 'extension' }}</span>
        </button>
      </div>
    </section>
    <div v-if="store.configData.rules.length" class="rules-folders">
      <h3 class="automation-list-title">已保存的自动化 <small>{{ store.configData.rules.length }}</small></h3>
      <section v-for="([folder, entries]) in ruleFolders" :key="folder" class="rules-folder">
        <header><span class="material-symbols-outlined">folder_open</span><b>{{ folder }}</b><small>{{ entries.length }} 条规则</small></header>
        <article v-for="({ rule, index }) in entries" :key="rule.rule_id || rule" class="rule-library-row" @click="openRule(index)">
          <div class="rule-library-row-main"><h3>{{ rule.name || '未命名规则' }}</h3><div class="rule-library-meta"><span><span class="material-symbols-outlined">bolt</span>当：{{ triggerSummary(rule) }}</span><span><span class="material-symbols-outlined">play_circle</span>然后：{{ rule.actions?.length || 0 }} 个动作</span></div></div>
          <div class="rule-library-actions">
            <button class="btn btn-text btn-sm rule-run-btn" :disabled="runningRuleIndex !== null"
              title="真实执行一次这条规则中的动作" @click.stop="runManualRule(index, rule)">
              <span v-if="runningRuleIndex === index" class="spinner"></span>
              <span v-else class="material-symbols-outlined">experiment</span>
              {{ runningRuleIndex === index ? '测试中…' : '测试' }}
            </button>
            <button class="icon-btn icon-btn-danger" title="删除规则" @click.stop="deleteRule(index)"><span class="material-symbols-outlined">delete</span></button>
          </div>
        </article>
      </section>
    </div>
    <QuickCreateDialog :open="showQuickCreate" @close="closeQuickCreate" @create="createQuickAutomation" />
  </section>
  </Transition>
  <TestRunDialog :open="!!testPreparation"
    :rule-name="testPreparation?.snapshot?.name"
    :rule="testPreparation?.snapshot"
    :schema="store.schema"
    :saved-values="testPreparation?.savedValues"
    @cancel="cancelTestPreparation" @run="submitTestPreparation" />
  <BaseDialog :open="!!testResult" @close="closeTestResult">
  <div v-if="testResult" class="test-result-dialog">
      <span class="material-symbols-outlined dialog-ico">experiment</span>
      <h3 class="dialog-title">规则测试</h3>
      <p class="dialog-sub">{{ testResult.ruleName }}</p>
      <div class="test-steps">
        <div v-if="!testResult.steps.length && !testResult.done" class="test-step test-step-wait">
          <span class="spinner"></span><span>已启动执行，等待结果…</span>
        </div>
        <div v-for="(s, i) in testResult.steps" :key="i" class="test-step" :class="'test-step-' + s.status">
          <span class="material-symbols-outlined">{{ stepIcon(s.status) }}</span>
          <div class="test-step-body">
            <b>{{ stepTitle(s) }}</b><p v-if="s.detail">{{ s.detail }}</p>
            <div v-if="s.inputSummary?.length || s.outputSummary?.length" class="step-summary-strip test-step-summary">
              <div v-if="s.inputSummary?.length" class="step-summary-side">
                <span class="step-summary-kind"><span class="material-symbols-outlined">login</span>输入</span>
                <span v-for="item in s.inputSummary" :key="item.name" class="step-summary-item" :class="{ redacted: item.redacted }"><b>{{ item.label }}</b><small>{{ item.display }}</small></span>
              </div>
              <span v-if="s.inputSummary?.length && s.outputSummary?.length" class="material-symbols-outlined step-summary-arrow">arrow_forward</span>
              <div v-if="s.outputSummary?.length" class="step-summary-side">
                <span class="step-summary-kind"><span class="material-symbols-outlined">logout</span>输出</span>
                <span v-for="item in s.outputSummary" :key="item.name" class="step-summary-item" :class="{ redacted: item.redacted }"><b>{{ item.label }}</b><small>{{ item.display }}</small></span>
              </div>
            </div>
          </div>
          <button v-if="s.status === 'fail' && s.stepId" class="btn btn-text btn-sm test-step-open" @click="openTestStep(s)">编辑这一步</button>
        </div>
        <div v-for="(assertion, i) in testResult.assertions?.results || []" :key="'assertion-' + i" class="test-step" :class="assertion.passed ? 'test-step-ok' : 'test-step-fail'">
          <span class="material-symbols-outlined">{{ assertion.passed ? 'fact_check' : 'rule' }}</span>
          <div><b>检查项 {{ i + 1 }}{{ assertion.path?.length ? ` · ${assertion.path.join('.')}` : '' }}</b><small>{{ assertion.message }}</small></div>
        </div>
      </div>
      <p v-if="testSummary" class="test-summary"
        :class="!testResult.done ? '' : !testResult.steps.some(s => ['fail', 'cancelled', 'timed_out'].includes(s.status)) && (!testResult.assertions?.total || testResult.assertions.passed === testResult.assertions.total) ? 'test-summary-ok' : 'test-summary-err'">{{ testSummary }}</p>
      <div class="dialog-actions">
        <button class="btn btn-text" @click="goLogs">
          <span class="material-symbols-outlined">description</span>查看日志
        </button>
        <button v-if="!testResult.done && testResult.runId" class="btn btn-outlined btn-danger" :disabled="testResult.cancelling" @click="stopTestRun">
          <span class="material-symbols-outlined">stop_circle</span>{{ testResult.cancelling ? '正在停止' : '停止测试' }}
        </button>
        <button class="btn btn-filled" @click="closeTestResult">关闭</button>
      </div>
  </div>
  </BaseDialog>
  </div>
</template>
