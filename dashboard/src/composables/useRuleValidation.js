import { computed, ref, watch, onUnmounted } from 'vue'
import { store } from '../lib/store'
import { getVisibleParamDefs, pluginUnavailableReason as unavailableReason } from '../lib/utils'
import { actionOutputDefs, bindingSourceFor, buildBindingSources, expandBindingSources,
  isReference, typesCompatible } from '../lib/bindings'
import { fieldType } from '../lib/valueTypes'
import { validateRuleDraft } from '../lib/api'

export function useRuleValidation(rule) {
  const typeCatalog = computed(() => Object.entries(store.schema.data_types?.custom || {}).map(([id, definition]) => ({ id, ...definition })))
  const pluginUnavailableReason = (kind, type) => unavailableReason(store.schema[kind]?.[type])
  const actionParams = action => getVisibleParamDefs(store.schema.actions[action.type], action.params || {})
  const actionBindingSources = index => buildBindingSources(rule.value, index, store.schema)
  const clientValidationIssues = computed(() => {
    const issues = []
    const add = (message, target = '') => issues.push({ severity: 'error', message, target })
    function validateCondition(node, path = '触发条件') {
      if (!node || typeof node !== 'object') { add(`${path}格式无效`, 'trigger'); return }
      const leaf = !!node.type && !node.children && !node.events
      if (leaf) {
        const triggerReason = pluginUnavailableReason('triggers', node.type)
        if (triggerReason) add(`${path}引用了当前系统不可用的触发器（${triggerReason}）`, 'trigger')
        if (node.params != null && (typeof node.params !== 'object' || Array.isArray(node.params))) add(`${path}参数格式无效`, 'trigger')
        return
      }
      if (!['any', 'all', 'not'].includes(node.op)) add(`${path}的组合方式无效`, 'trigger')
      if (!Array.isArray(node.children) || !node.children.length) {
        add(`${path}组不能为空`, 'trigger')
        return
      }
      if (node.op === 'not') {
        if (node.children.length !== 1 || !node.children[0]?.type) add(`${path}的 NOT 需要一个事件条件`, 'trigger')
        if (!(Number(node.within_seconds) > 0)) add(`${path}的 NOT 需要等待时长`, 'trigger')
      }
      if ('within_seconds' in node) {
        const seconds = Number(node.within_seconds)
        if (!Number.isFinite(seconds) || seconds <= 0) add(`${path}的时间窗口必须大于 0`, 'trigger')
      }
      node.children.forEach((child, index) => validateCondition(child, `${path} ${index + 1}`))
    }
    if (!String(rule.value.name || '').trim()) add('请填写规则名称', 'name')
    if (!rule.value.condition) add('请选择至少一个触发条件', 'trigger')
    if (rule.value.condition) validateCondition(rule.value.condition)
    const actions = Array.isArray(rule.value.actions) ? rule.value.actions : []
    if (!Array.isArray(rule.value.actions)) add('动作列表格式无效', 'actions')
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
    function branchSources(base, siblings, index) {
      const result = [...base]
      siblings.slice(0, index).forEach(item => {
        if (item?.type === 'if') return
        actionOutputDefs(item, store.schema, rule.value).forEach(output => result.push({
          group: '当前分支的前序动作',
          label: `${store.schema.actions[item.type]?.name || item.type} · ${output.label || output.name}`,
          type: fieldType(output),
          optional: output.required === false,
          sensitive: output.sensitive,
          value: { $ref: { scope: 'step', node: item.binding_id, path: [output.name] } },
        }))
      })
      return expandBindingSources(result, store.schema.data_types?.custom)
    }
    function validateActionNode(action, label, nodeId, sources) {
      if (action?.type === 'set_variable') {
        if (!rule.value.variables?.some(item => item.id === action.variable)) add(`${label} 的赋值目标不存在`, nodeId)
        return
      }
      if (action?.type === 'if') {
        if (!action.then?.length && !action.else?.length) add(`${label} 需要至少一个分支动作`, nodeId)
        ;(action.then || []).forEach((item, index) => {
          validateActionNode(item, `${label} THEN ${index + 1}`, nodeId, branchSources(sources, action.then, index))
        })
        ;(action.else || []).forEach((item, index) => {
          validateActionNode(item, `${label} ELSE ${index + 1}`, nodeId, branchSources(sources, action.else, index))
        })
        return
      }
      const actionReason = pluginUnavailableReason('actions', action?.type)
      if (actionReason) add(`${label} 引用了当前系统不可用的插件（${actionReason}）`, nodeId)
      if (action?.params != null && (typeof action.params !== 'object' || Array.isArray(action.params))) add(`${label} 参数格式无效`, nodeId)
      validateTimeout(action, label, nodeId)
      for (const def of actionParams(action)) {
        const value = action?.params?.[def.name]
        if (!isReference(value)) continue
        const source = bindingSourceFor(value.$ref, sources, typeCatalog.value)
        if (!source) add(`${label} 的“${def.label || def.name}”引用了不可用数据`, nodeId)
        else if (!typesCompatible(source.type, fieldType(def, true))) {
          add(`${label} 的“${def.label || def.name}”数据类型不兼容`, nodeId)
        }
      }
      ;(Array.isArray(action?.failure_actions) ? action.failure_actions : []).forEach((failureAction, failureIndex) => {
        const failureLabel = `${label} 的补救动作 ${failureIndex + 1}`
        const failureReason = pluginUnavailableReason('actions', failureAction?.type)
        if (failureReason) add(`${failureLabel} 不可用（${failureReason}）`, nodeId)
        if (failureAction?.params != null && (typeof failureAction.params !== 'object' || Array.isArray(failureAction.params))) {
          add(`${failureLabel} 参数格式无效`, nodeId)
        }
        validateTimeout(failureAction, failureLabel, nodeId)
        const failureSources = [...sources]
        action.failure_actions.slice(0, failureIndex).forEach(item => {
          actionOutputDefs(item, store.schema, rule.value).forEach(output => failureSources.push({
            group: '之前的补救动作',
            label: `${item.type} · ${output.label || output.name}`,
            type: fieldType(output),
            optional: output.required === false,
            sensitive: output.sensitive,
            value: { $ref: { scope: 'step', node: item.binding_id, path: [output.name] } },
          }))
        })
        for (const def of actionParams(failureAction)) {
          const value = failureAction?.params?.[def.name]
          if (!isReference(value)) continue
          const source = bindingSourceFor(value.$ref, failureSources, typeCatalog.value)
          if (!source) add(`${failureLabel} 引用了不可用数据`, nodeId)
          else if (!typesCompatible(source.type, fieldType(def, true))) {
            add(`${failureLabel} 数据类型不兼容`, nodeId)
          }
        }
      })
    }
    actions.forEach((action, index) => {
      validateActionNode(
        action,
        `动作 ${index + 1}`,
        `action:${index}`,
        actionBindingSources(index),
      )
    })
    if (rule.value.preconditions?.length) add('运行前检查已移除，请移除旧配置并改用 NOT 或 IF', 'actions')
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
      const result = await validateRuleDraft(JSON.parse(JSON.stringify(rule.value)))
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
  watch(() => JSON.stringify(rule.value), scheduleRuleCheck, { immediate: true })
  onUnmounted(() => {
    if (checkerTimer) clearTimeout(checkerTimer)
    checkerSequence++
  })
  return { validationIssues, validationErrorCount, validationWarningCount, checkingRule, checkerError, runRuleCheck }
}
