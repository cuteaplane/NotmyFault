<script setup>
import { computed } from 'vue'
import { store } from '../../lib/store'
import NaturalDraftPanel from '../NaturalDraftPanel.vue'
import { normalizeRuleDraft } from '../../lib/utils'
import { ensureRuleBindingIds } from '../../lib/bindings'
import { approveRuleBeforeEditing } from '../../lib/ruleSave'

const aiEnabled = computed(() => store.aiDrafting?.enabled)

async function createDraft(draft) {
  const rule = ensureRuleBindingIds(normalizeRuleDraft(
    JSON.parse(JSON.stringify(draft))
  ))
  const approval = await approveRuleBeforeEditing(rule)
  if (!approval?.ok) return
  store.pendingRuleDraft = rule
  window.__nmf?.switchPage?.('rules')
}

function goSettings() {
  window.__nmf?.switchPage?.('settings')
}
</script>

<template>
  <section class="page active ai-view">
    <div class="page-head">
      <div>
        <h2>AI 起草</h2>
        <p class="page-subtitle">描述你想自动化的事，AI 生成规则草稿送进编辑器。</p>
      </div>
      <div class="actions">
        <button class="btn btn-text" @click="goSettings">
          <span class="material-symbols-outlined">settings</span>AI 设置
        </button>
      </div>
    </div>

    <div v-if="!aiEnabled" class="ai-view-disabled">
      <span class="material-symbols-outlined ai-view-disabled-icon">auto_awesome</span>
      <h3>AI 起草未启用</h3>
      <p>需要先在设置里填写 AI 服务地址和 API 密钥。</p>
      <button class="btn btn-tonal" @click="goSettings">
        <span class="material-symbols-outlined">settings</span>前往设置
      </button>
    </div>

    <div v-else class="ai-view-chat">
      <NaturalDraftPanel @create="createDraft" />
    </div>
  </section>
</template>
