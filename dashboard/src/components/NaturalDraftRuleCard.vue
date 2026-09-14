<script setup>
import { computed } from 'vue'
import { store } from '../lib/store'
import { computeChangeSet } from '../lib/ruleDiff'
import {
  actionNames, conditionLabel, draftEditsCurrentRule, hasHighImpactActions,
  nodeIdForChange, ruleNodeCount, ruleParamRows,
} from '../lib/naturalDraftRules'

const props = defineProps({
  message: { type: Object, required: true },
  fullRule: { type: Object, default: null },
})
const emit = defineEmits(['create', 'highlight-node'])
const draft = computed(() => props.message.result?.draft)
const changeSet = computed(() => draftEditsCurrentRule(draft.value, props.fullRule)
  ? computeChangeSet(props.fullRule, draft.value, store.schema) : [])
const conditionName = computed(() => conditionLabel(draft.value?.condition, store.schema))
const actions = computed(() => actionNames(draft.value, store.schema))
const nodeCount = computed(() => ruleNodeCount(draft.value))
const paramRows = computed(() => ruleParamRows(draft.value, store.schema))
const issues = computed(() => props.message.result?.validation?.issues?.slice(0, 5) || [])
const highImpactActions = computed(() => hasHighImpactActions(draft.value, store.schema))

function highlightNode(item) {
  const nodeId = nodeIdForChange(item, props.fullRule)
  if (nodeId) emit('highlight-node', nodeId)
}

function applyDraft() {
  if (!draft.value) return
  const next = { ...draft.value }
  if (changeSet.value.length) delete next.preconditions
  emit('create', next)
}
</script>

<template>
<div class="ai-rule-card">
  <template v-if="changeSet.length">
    <div class="ai-rule-head">
      <span class="ai-card-kicker"><span class="material-symbols-outlined">difference</span>建议更改</span>
      <span class="ai-rule-counts">{{ changeSet.length }} 项修改</span>
    </div>
    <div class="ai-changeset-list">
      <div v-for="(item, ci) in changeSet" :key="ci"
        class="ai-changeset-item" :class="'ai-changeset-' + item.op"
        :clickable="nodeIdForChange(item, fullRule) ? true : null"
        @click="highlightNode(item)">
        <span class="ai-changeset-op">{{ item.op === 'add' ? 'A' : item.op === 'delete' ? 'D' : 'M' }}</span>
        <span class="ai-changeset-label">{{ item.label }}</span>
        <span v-if="item.detail" class="ai-changeset-detail">{{ item.detail }}</span>
      </div>
    </div>
    <div v-if="highImpactActions.length" class="ai-security-warn">
      <span class="material-symbols-outlined">shield</span>
      <div>
        <b>此修改包含需要额外确认的操作</b>
        <ul><li v-for="(w, wi) in highImpactActions" :key="wi">{{ w }}</li></ul>
        <small>请在应用前检查生成内容。</small>
      </div>
    </div>
    <div class="ai-rule-foot">
      <button class="btn btn-text btn-sm" type="button" @click="message.ruleDetailsOpen = !message.ruleDetailsOpen">
        {{ message.ruleDetailsOpen ? '收起详情' : '查看详情' }}
      </button>
      <button class="btn btn-tonal btn-sm" type="button" :disabled="!message.result.draft" @click="applyDraft()">
        全部应用<span class="material-symbols-outlined">arrow_forward</span>
      </button>
    </div>
  </template>
  <template v-else>
    <div class="ai-rule-head">
      <span class="ai-card-kicker"><span class="material-symbols-outlined">layers</span>候选规则</span>
      <span class="ai-rule-counts">{{ nodeCount }}</span>
    </div>
    <div class="ai-rule-flow">
      <div class="ai-mini-node">
        <span class="material-symbols-outlined">bolt</span>
        <span class="min-w-0 flex-1 truncate">{{ conditionName }}</span>
      </div>
      <div v-for="name in actions" :key="`action-${name}`" class="ai-mini-node">
        <span class="material-symbols-outlined">play_arrow</span>
        <span class="min-w-0 flex-1 truncate">{{ name }}</span>
      </div>
    </div>
    <div class="ai-rule-foot">
      <button class="btn btn-text btn-sm" type="button" @click="message.ruleDetailsOpen = !message.ruleDetailsOpen">
        {{ message.ruleDetailsOpen ? '收起参数' : '查看参数' }}
      </button>
      <button class="btn btn-tonal btn-sm" type="button" :disabled="!message.result.draft" @click="applyDraft()">
        应用到编辑器<span class="material-symbols-outlined">arrow_forward</span>
      </button>
    </div>
  </template>
  <div v-if="issues.length" class="ai-issues">
    <div v-for="issue in issues" :key="`${issue.code}-${issue.message}`" class="ai-issue">
      <span class="material-symbols-outlined">{{ issue.severity === 'warning' ? 'warning' : 'error' }}</span>{{ issue.message }}
    </div>
  </div>
  <div v-if="message.ruleDetailsOpen" class="ai-rule-params">
    <div v-for="row in paramRows" :key="`${row.node}-${row.key}`" class="ai-rule-param-row">
      <span>{{ row.node }}</span>
      <code>{{ row.key }} = {{ row.value }}</code>
    </div>
  </div>
</div>
</template>

<style scoped>
.ai-rule-card{display:flex;flex-direction:column;gap:12px;width:100%;min-width:0;max-width:100%;padding:14px;border:1px solid var(--md-outline-variant);border-radius:var(--r-md);background:var(--md-surface-c-low)}
.ai-rule-head{display:flex;align-items:center;justify-content:space-between;gap:8px}
.ai-card-kicker{display:inline-flex;align-items:center;gap:6px;color:var(--md-primary);font:var(--ts-label-m);font-weight:500}
.ai-card-kicker .material-symbols-outlined{font-size:18px}
.ai-rule-counts{font:var(--ts-label-m);color:var(--md-on-surface-variant)}
.ai-rule-flow{display:flex;flex-direction:column;gap:0}
.ai-mini-node{display:flex;align-items:center;gap:8px;min-height:32px;min-width:0;padding:6px 10px;border:1px solid var(--md-outline-variant);border-radius:var(--r-sm);background:var(--md-surface);font-size:13px;line-height:18px;color:var(--md-on-surface)}
.ai-mini-node .material-symbols-outlined{flex:none;font-size:16px;color:var(--md-primary)}
.ai-mini-node>span:last-child{min-width:0;flex:1}
.ai-mini-node+.ai-mini-node{margin-top:10px;position:relative}
.ai-mini-node+.ai-mini-node::before{content:'';position:absolute;left:17px;top:-10px;height:10px;border-left:2px solid var(--md-outline-variant)}
.ai-rule-foot{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}
.ai-rule-params{display:flex;flex-direction:column;gap:6px;padding-top:12px;margin-top:4px;border-top:1px dashed var(--md-outline-variant)}
.ai-rule-param-row{display:grid;grid-template-columns:auto minmax(0,1fr);gap:10px;font-size:12px;line-height:17px;color:var(--md-on-surface-variant)}
.ai-rule-param-row>span:first-child{white-space:nowrap}
.ai-rule-param-row code{font-family:ui-monospace,Consolas,monospace;font-size:12px;word-break:break-all;color:var(--md-on-surface);overflow-wrap:anywhere}
.ai-issues{display:flex;flex-direction:column;gap:4px;padding:8px 10px;border-radius:var(--r-sm);background:color-mix(in srgb,var(--md-warn) 9%,transparent)}
.ai-issue{display:flex;align-items:flex-start;gap:6px;font-size:12px;line-height:17px;color:var(--md-on-surface-variant)}
.ai-issue .material-symbols-outlined{flex:none;margin-top:1px;font-size:14px;color:var(--md-warn)}
.ai-changeset-list{display:flex;flex-direction:column;gap:2px;margin-top:4px}
.ai-changeset-item{display:grid;grid-template-columns:24px auto minmax(0,1fr);align-items:center;gap:8px;padding:6px 8px;border-radius:var(--r-xs);font-size:13px;line-height:18px;color:var(--md-on-surface);transition:background .12s ease}
.ai-changeset-item[clickable]{cursor:pointer}
.ai-changeset-item[clickable]:hover{background:color-mix(in srgb,var(--md-primary) 8%,transparent)}
.ai-changeset-op{display:flex;width:20px;height:20px;align-items:center;justify-content:center;border-radius:var(--r-xs);font-size:11px;font-weight:600;font-family:ui-monospace,Consolas,monospace}
.ai-changeset-modify .ai-changeset-op{background:var(--md-primary-container);color:var(--md-on-primary-container)}
.ai-changeset-add .ai-changeset-op{background:var(--md-success-container);color:var(--md-success)}
.ai-changeset-delete .ai-changeset-op{background:var(--md-error-container);color:var(--md-on-error-container)}
.ai-changeset-label{font-weight:500;white-space:nowrap}
.ai-changeset-detail{min-width:0;color:var(--md-on-surface-variant);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ai-security-warn{display:flex;gap:10px;padding:10px 12px;border:1px solid color-mix(in srgb,var(--md-warn) 40%,var(--md-outline-variant));border-radius:var(--r-sm);background:color-mix(in srgb,var(--md-warn) 8%,var(--md-surface-c-low));font-size:13px;line-height:19px;margin-top:8px}
.ai-security-warn>.material-symbols-outlined{flex:none;font-size:18px;color:var(--md-warn);margin-top:2px}
.ai-security-warn b{display:block;font-weight:500;margin-bottom:4px}
.ai-security-warn ul{margin:0;padding-left:18px}
.ai-security-warn li{margin:2px 0;color:var(--md-on-surface-variant)}
.ai-security-warn small{display:block;margin-top:6px;color:var(--md-on-surface-variant);font-size:12px}
@media (prefers-reduced-motion:reduce){.ai-changeset-item{transition:none;animation:none}}
</style>
