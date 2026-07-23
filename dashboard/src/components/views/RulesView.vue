<script setup>
import { computed, onMounted, ref } from 'vue'
import { store } from '../../lib/store'
import { runRule, saveConfig } from '../../lib/api'
import { snackbar } from '../../lib/notify'
import { buildDefaultParams } from '../../lib/utils'
import RuleEditor from '../RuleEditor.vue'

const activeRuleIndex = ref(null)
const runningRuleIndex = ref(null)
const activeRule = computed(() => activeRuleIndex.value === null ? null : store.configData.rules[activeRuleIndex.value])
const ruleFolders = computed(() => {
  const folders = new Map()
  store.configData.rules.forEach((rule, index) => {
    const name = String(rule.folder || '未分类')
    if (!folders.has(name)) folders.set(name, [])
    folders.get(name).push({ rule, index })
  })
  return [...folders]
})

function triggerCount(rule) {
  function count(node) {
    if (!node) return 0
    if (node.type && !node.children && !node.events) return 1
    return (node.children || node.events || []).reduce((sum, child) => sum + count(child), 0)
  }
  return rule.condition ? count(rule.condition) : (rule.event ? 1 : 0)
}
function triggerSummary(rule) {
  if (!rule.condition) return store.schema.triggers[rule.event?.type]?.name || rule.event?.type || '未配置触发器'
  const op = rule.condition.op || (rule.condition.type === 'and' ? 'all' : 'any')
  return `${op === 'all' ? 'AND · 全部满足' : 'OR · 任一满足'} · ${triggerCount(rule)} 项`
}
function hasManualTrigger(rule) {
  function includesManual(node) {
    if (!node) return false
    if (node.type && !node.children && !node.events) return node.type === 'manual'
    return (node.children || node.events || []).some(includesManual)
  }
  return rule.condition ? includesManual(rule.condition) : rule.event?.type === 'manual'
}
function addRule() {
  const type = Object.keys(store.schema.triggers)[0] || 'unknown'
  store.configData.rules.unshift({
    name: '新规则',
    folder: '未分类',
    event: { type, params: buildDefaultParams(store.schema.triggers[type]) },
    actions: [],
  })
  activeRuleIndex.value = 0
}
function deleteRule(index) {
  store.configData.rules.splice(index, 1)
  activeRuleIndex.value = null
}
async function doSave() {
  try {
    const result = await saveConfig(store.configData.rules)
    if (result.ok) { snackbar('配置已保存'); return }
    let message = '保存失败: ' + (result.error || '未知错误')
    if (result.error?.includes('Forbidden')) message += ' - 浏览器模式不支持写入，请使用桌面端 Dashboard'
    alert(message)
  } catch (error) { alert('保存失败: ' + error.message) }
}
async function runManualRule(index) {
  runningRuleIndex.value = index
  try {
    const result = await runRule(index)
    if (result.ok) snackbar(result.message || '规则已开始执行')
    else alert('无法执行规则: ' + (result.error || '未知错误'))
  } catch (error) { alert('无法执行规则: ' + error.message) }
  finally { runningRuleIndex.value = null }
}

onMounted(() => { if (!store.configData.rules) store.configData.rules = [] })
</script>

<template>
  <RuleEditor v-if="activeRule" :rule="activeRule" @back="activeRuleIndex = null" @delete="deleteRule(activeRuleIndex)" @save="doSave" />

  <section v-else class="page active rules-library">
    <div class="page-head">
      <h2>规则配置</h2>
      <div class="actions"><button class="btn btn-filled" @click="addRule"><span class="material-symbols-outlined">add</span>新建规则</button></div>
    </div>
    <div v-if="!store.configData.rules.length" class="empty-state">
      <div class="material-symbols-outlined">rule_folder</div><h3>还没有规则</h3><p>新建一条，给自动化立个规矩。</p>
      <button class="btn btn-tonal" style="margin-top:14px" @click="addRule"><span class="material-symbols-outlined">add</span>新建规则</button>
    </div>
    <div v-else class="rules-folders">
      <section v-for="([folder, entries]) in ruleFolders" :key="folder" class="rules-folder">
        <header><span class="material-symbols-outlined">folder_open</span><b>{{ folder }}</b><small>{{ entries.length }} 条规则</small></header>
        <article v-for="({ rule, index }) in entries" :key="rule" class="rule-library-row" @click="activeRuleIndex = index">
          <div class="rule-library-row-main"><h3>{{ rule.name || '未命名规则' }}</h3><div class="rule-library-meta"><span><span class="material-symbols-outlined">bolt</span>{{ triggerSummary(rule) }}</span><span><span class="material-symbols-outlined">play_circle</span>{{ rule.actions?.length || 0 }} 个动作</span></div></div>
          <div class="rule-library-actions">
            <button v-if="hasManualTrigger(rule)" class="icon-btn rule-run-btn" :class="{ spin: runningRuleIndex === index }" :disabled="runningRuleIndex === index" title="运行规则" @click.stop="runManualRule(index)"><span class="material-symbols-outlined">play_arrow</span></button>
            <button class="icon-btn icon-btn-danger" title="删除规则" @click.stop="deleteRule(index)"><span class="material-symbols-outlined">delete</span></button>
          </div>
        </article>
      </section>
    </div>
  </section>
</template>
