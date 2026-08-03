<script setup>
import { computed, onMounted, ref } from 'vue'
import { store } from '../../lib/store'
import { runRule, saveConfig } from '../../lib/api'
import { snackbar } from '../../lib/notify'
import { normalizeRuleDraft } from '../../lib/utils'
import { ensureRuleBindingIds, requestTestContext } from '../../lib/bindings'
import RuleEditor from '../RuleEditor.vue'

const activeRuleIndex = ref(null)
const draftRule = ref(null)
const baseline = ref('')
const runningRuleIndex = ref(null)
const activeRule = computed(() => draftRule.value)
const isDirty = computed(() => !!draftRule.value && JSON.stringify(draftRule.value) !== baseline.value)
const isTestingActiveRule = computed(() => (
  activeRuleIndex.value !== null
  && runningRuleIndex.value === activeRuleIndex.value
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
function openRule(index) {
  activeRuleIndex.value = index
  draftRule.value = ensureRuleBindingIds(
    normalizeRuleDraft(clone(store.configData.rules[index])),
  )
  baseline.value = JSON.stringify(draftRule.value)
}
function addRule() {
  activeRuleIndex.value = -1
  draftRule.value = ensureRuleBindingIds({
    name: '新规则',
    folder: '未分类',
    event: null,
    actions: [],
  })
  baseline.value = JSON.stringify(draftRule.value)
}
function leaveEditor() {
  if (isDirty.value && !confirm('这条规则还有未保存的修改。要放弃这些修改吗？')) return
  activeRuleIndex.value = null
  draftRule.value = null
  baseline.value = ''
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
  const result = await saveConfig(nextRules)
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
    activeRuleIndex.value = savedIndex
    draftRule.value = clone(savedRules[savedIndex])
    baseline.value = JSON.stringify(draftRule.value)
    if (runAfter) {
      await runManualRule(savedIndex, savedRules[savedIndex])
    }
  } catch (error) {
    alert('保存失败: ' + error.message)
  }
}
async function deleteRule(index) {
  if (!confirm('确定删除这条规则吗？此操作将在保存后立即生效。')) return
  try {
    const nextRules = clone(store.configData.rules)
    nextRules.splice(index, 1)
    await persistRules(nextRules, '规则已删除')
    if (activeRuleIndex.value === index) leaveEditorAfterDelete()
  } catch (error) {
    alert('删除失败: ' + error.message)
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
}
async function runManualRule(index, ruleSnapshot = null) {
  if (runningRuleIndex.value !== null) return
  runningRuleIndex.value = index
  try {
    const snapshot = ruleSnapshot ? clone(ruleSnapshot) : null
    let result = await runRule(index, snapshot)
    if (result?.code === 'missing_test_context') {
      const testContext = requestTestContext(snapshot || store.configData.rules[index], store.schema)
      if (testContext === null) return
      result = await runRule(index, snapshot, testContext)
    }
    if (result.ok) snackbar(result.message ? `测试已启动：${result.message}` : '规则测试已启动')
    else alert('规则测试失败: ' + (result.error || '未知错误'))
  } catch (error) {
    alert('规则测试失败: ' + error.message)
  } finally {
    runningRuleIndex.value = null
  }
}

onMounted(() => { if (!store.configData.rules) store.configData.rules = [] })
</script>

<template>
  <Transition name="rule-route" mode="out-in">
  <RuleEditor v-if="activeRule" key="editor" :rule="activeRule" :dirty="isDirty"
    :testing="isTestingActiveRule" :folders="availableFolders"
    @back="leaveEditor" @delete="deleteActiveRule" @save="doSave(false)" @save-run="doSave(true)" />

  <section v-else key="library" class="page active rules-library">
    <div class="page-head">
      <div><h2>规则</h2><p class="page-subtitle">用“当 → 然后”描述每一条自动化。</p></div>
      <div class="actions"><button class="btn btn-filled" @click="addRule"><span class="material-symbols-outlined">add</span>新建规则</button></div>
    </div>
    <div v-if="!store.configData.rules.length" class="empty-state">
      <div class="material-symbols-outlined">rule_folder</div><h3>还没有规则</h3><p>从一个触发条件和一个动作开始。</p>
      <button class="btn btn-tonal" style="margin-top:14px" @click="addRule"><span class="material-symbols-outlined">add</span>新建规则</button>
    </div>
    <div v-else class="rules-folders">
      <section v-for="([folder, entries]) in ruleFolders" :key="folder" class="rules-folder">
        <header><span class="material-symbols-outlined">folder_open</span><b>{{ folder }}</b><small>{{ entries.length }} 条规则</small></header>
        <article v-for="({ rule, index }) in entries" :key="rule" class="rule-library-row" @click="openRule(index)">
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
  </section>
  </Transition>
</template>
