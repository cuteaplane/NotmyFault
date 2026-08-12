<script setup>
import { ref } from 'vue'
import { store } from '../lib/store'
import { saveConfig } from '../lib/api'
import { snackbar } from '../lib/notify'
import { ORIGIN_STORY, ORIGIN_RULES } from '../lib/origin'

const emit = defineEmits(['close'])
const saving = ref(false)

function clone(value) { return JSON.parse(JSON.stringify(value)) }

// 点规则卡片：已存在就跳去编辑，缺的几条一次性追加保存后再跳。
async function openOriginRule(rule) {
  if (saving.value) return
  const rules = store.configData?.rules || []
  const existing = rules.find(r => r.name === rule.name)
  if (existing) { jumpToRule(existing); return }
  saving.value = true
  try {
    const missing = ORIGIN_RULES.filter(
      r => !rules.some(e => e.name === r.name),
    )
    const nextRules = [...clone(rules), ...missing.map(clone)]
    const result = await saveConfig(nextRules)
    if (!result?.ok) {
      snackbar(result?.error || '导入起源规则失败')
      return
    }
    const savedRules = Array.isArray(result.rules) ? result.rules : nextRules
    store.configData = { ...store.configData, rules: savedRules }
    snackbar(`已导入 ${missing.length} 条起源规则`)
    jumpToRule(savedRules.find(item => item.name === rule.name) || rule)
  } catch (error) {
    snackbar(error.message || '导入起源规则失败')
  } finally {
    saving.value = false
  }
}

function jumpToRule(rule) {
  store.pendingRuleId = rule.rule_id || ''
  store.pendingRuleName = rule.name || ''
  emit('close')
  if (window.__nmf?.switchPage) window.__nmf.switchPage('rules')
}
</script>

<template>
  <div class="modal-overlay" @click.self="emit('close')">
    <div class="origin-dialog">
      <button class="icon-btn origin-close" title="关闭" @click="emit('close')">
        <span class="material-symbols-outlined">close</span>
      </button>
      <header class="origin-head">
        <span class="material-symbols-outlined origin-head-ico">history_edu</span>
        <h3>{{ ORIGIN_STORY.title }}</h3>
      </header>
      <div class="origin-story">
        <p v-for="(text, i) in ORIGIN_STORY.paragraphs" :key="i">{{ text }}</p>
      </div>
      <div class="origin-rules">
        <button v-for="rule in ORIGIN_RULES" :key="rule.name" class="origin-rule-card"
          :disabled="saving" @click="openOriginRule(rule)">
          <span class="material-symbols-outlined">rule</span>
          <div>
            <b>{{ rule.name }}</b>
            <p>当 {{ rule.event.params.process_name }} {{ rule.event.params.state === 'running' ? '运行' : '退出' }} · {{ rule.actions.length }} 个动作</p>
          </div>
          <span class="material-symbols-outlined origin-rule-go">arrow_forward</span>
        </button>
      </div>
      <p class="origin-hint">↑ ↑ ↓ ↓ ← → ← → B A  感动不指甲强</p>
    </div>
  </div>
</template>
