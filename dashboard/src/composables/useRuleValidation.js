import { computed, ref, watch, onUnmounted } from 'vue'
import { normalizeWorkflow } from '../lib/workflowGraph.js'
import { validateRuleDraft } from '../lib/api'

export function useRuleValidation(rule) {
  const clientValidationIssues = computed(() => {
    const issues = []
    if (!String(rule.value.name || '').trim()) issues.push({ severity: 'error', message: '请填写规则名称', target: 'name' })
    try { normalizeWorkflow(rule.value) } catch (error) { issues.push({ severity: 'error', message: error.message }) }
    return issues
  })
  const serverValidationIssues = ref([])
  const checkingRule = ref(false)
  const checkerError = ref('')
  let checkerTimer = null
  let checkerSequence = 0

  function issueTarget(issue) {
    if (issue.node_id) return `node:${issue.node_id}`
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
