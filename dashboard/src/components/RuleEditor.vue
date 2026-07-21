<script setup>
import { ref, computed } from 'vue'
import { store } from '../lib/store'
import { getVisibleParamDefs, buildDefaultParams } from '../lib/utils'
import ParamInput from './ParamInput.vue'

const props = defineProps({ rule: Object })
const emit = defineEmits(['delete'])

const expanded = ref(false)
const triggerKeys = computed(() => Object.keys(store.schema.triggers))
const actionKeys = computed(() => Object.keys(store.schema.actions))
const isCondition = computed(() => !!props.rule.condition)

// 折叠状态摘要
const triggerSummary = computed(() => {
  if (isCondition.value) {
    const events = props.rule.condition?.events || []
    if (!events.length) return '未配置'
    const names = events.map(e => store.schema.triggers[e.type]?.name || e.type)
    return names.join(' / ') + ' (OR)'
  }
  const t = props.rule.event?.type
  return t ? (store.schema.triggers[t]?.name || t) : '未配置'
})
const actionSummary = computed(() => {
  const n = props.rule.actions?.length || 0
  return n + ' 个动作'
})

function isAdmin(m) { return !!(m && (m.permissions || []).includes('admin')) }
function evParams(ev) { return getVisibleParamDefs(store.schema.triggers[ev.type], ev.params) }
function actParams(a) { return getVisibleParamDefs(store.schema.actions[a.type], a.params) }

function changeTrigger(t) {
  props.rule.event = { type: t, params: buildDefaultParams(store.schema.triggers[t]) }
}
function changeAction(a, t) {
  props.rule.actions[a] = { type: t, params: buildDefaultParams(store.schema.actions[t]) }
}
function addAction() {
  const d = actionKeys.value[0] || 'unknown'
  props.rule.actions.push({ type: d, params: buildDefaultParams(store.schema.actions[d]) })
}
function removeAction(a) { props.rule.actions.splice(a, 1) }

function changeCondTrigger(e, t) {
  props.rule.condition.events[e] = { type: t, params: buildDefaultParams(store.schema.triggers[t]) }
}
function addCondition() {
  const k = triggerKeys.value[0] || ''
  if (!props.rule.condition) props.rule.condition = { type: 'or', events: [] }
  props.rule.condition.events.push({ type: k, params: buildDefaultParams(store.schema.triggers[k]) })
}
function removeCondition(e) {
  const c = props.rule.condition
  if (c) { c.events.splice(e, 1); if (!c.events.length) delete props.rule.condition }
}

function toggleMode() {
  const r = props.rule
  if (r.condition) {
    const f = (r.condition.events || [])[0]
    r.event = f ? { type: f.type, params: { ...f.params } } : { type: '', params: {} }
    delete r.condition
  } else if (r.event) {
    r.condition = { type: 'or', events: [{ type: r.event.type, params: { ...r.event.params } }] }
    delete r.event
  }
}
</script>

<template>
  <!-- 折叠状态：摘要行，点击展开 -->
  <div v-if="!expanded" class="card rule-collapsed" @click="expanded = true">
    <div class="rule-summary-left">
      <span class="material-symbols-outlined toggle-ico">expand_more</span>
      <div>
        <div class="rule-summary-name">{{ rule.name || '未命名规则' }}</div>
        <div class="rule-summary-meta">
          <span class="rule-tag"><span class="material-symbols-outlined">bolt</span>{{ triggerSummary }}</span>
          <span class="rule-tag"><span class="material-symbols-outlined">play_circle</span>{{ actionSummary }}</span>
        </div>
      </div>
    </div>
    <button class="icon-btn icon-btn-danger" @click.stop="emit('delete')" title="删除规则">
      <span class="material-symbols-outlined">delete</span></button>
  </div>

  <!-- 展开状态：完整编辑 -->
  <div v-else class="card" style="margin-bottom:16px">
    <div class="rule-head" @click="expanded = false" style="cursor:pointer">
      <span class="material-symbols-outlined toggle-ico">expand_less</span>
      <input class="text-field" type="text" v-model="rule.name" placeholder="规则名称" @click.stop style="flex:1">
      <button class="icon-btn icon-btn-danger" @click.stop="emit('delete')" title="删除规则">
        <span class="material-symbols-outlined">delete</span></button>
    </div>

    <h4 class="card-section-title" style="margin-top:16px">
      <span class="material-symbols-outlined" style="font-size:18px;vertical-align:-3px;margin-right:4px;color:var(--md-primary)">
        {{ isCondition ? 'alt_route' : 'bolt' }}</span>
      {{ isCondition ? '触发条件 (OR)' : '触发事件' }}
    </h4>

    <template v-if="isCondition">
      <div v-for="(ev, eIdx) in rule.condition.events" :key="eIdx" class="rule-item">
        <label class="field field-narrow"><span class="field-label">触发器 {{ eIdx + 1 }}</span>
          <select class="select" :value="ev.type" @change="changeCondTrigger(eIdx, $event.target.value)">
            <option v-for="k in triggerKeys" :key="k" :value="k">{{ store.schema.triggers[k].name || k }}</option>
          </select></label>
        <ParamInput v-for="p in evParams(ev)" :key="p.name" :def="p" v-model="ev.params[p.name]" />
        <button class="icon-btn icon-btn-danger" @click="removeCondition(eIdx)" title="移除">
          <span class="material-symbols-outlined">close</span></button>
      </div>
      <div class="rule-actions-bar">
        <button class="btn btn-text btn-sm" @click="addCondition"><span class="material-symbols-outlined">add</span>添加条件</button>
        <button class="btn btn-text btn-sm" @click="toggleMode"><span class="material-symbols-outlined">swap_horiz</span>切换单事件</button>
      </div>
    </template>

    <template v-else>
      <div class="rule-item">
        <label class="field field-narrow"><span class="field-label">选择触发器</span>
          <select class="select" :value="rule.event.type" @change="changeTrigger($event.target.value)">
            <option v-for="k in triggerKeys" :key="k" :value="k">{{ store.schema.triggers[k].name || k }}{{ isAdmin(store.schema.triggers[k]) ? ' [管理员]' : '' }}</option>
          </select></label>
        <ParamInput v-for="p in evParams(rule.event)" :key="p.name" :def="p" v-model="rule.event.params[p.name]" />
      </div>
      <div class="rule-actions-bar">
        <button class="btn btn-text btn-sm" @click="toggleMode"><span class="material-symbols-outlined">alt_route</span>改用 OR 条件</button>
      </div>
    </template>

    <h4 class="card-section-title" style="margin-top:20px">
      <span class="material-symbols-outlined" style="font-size:18px;vertical-align:-3px;margin-right:4px;color:var(--md-primary)">play_circle</span>
      执行动作
    </h4>

    <div v-for="(a, aIdx) in rule.actions" :key="aIdx" class="rule-item">
      <label class="field field-narrow"><span class="field-label">动作 {{ aIdx + 1 }}</span>
        <select class="select" :value="a.type" @change="changeAction(aIdx, $event.target.value)">
          <option v-for="k in actionKeys" :key="k" :value="k">{{ store.schema.actions[k].name || k }}{{ isAdmin(store.schema.actions[k]) ? ' [管理员]' : '' }}</option>
        </select></label>
      <ParamInput v-for="p in actParams(a)" :key="p.name" :def="p" v-model="a.params[p.name]" />
      <button class="icon-btn icon-btn-danger" @click="removeAction(aIdx)" title="移除动作">
        <span class="material-symbols-outlined">close</span></button>
      <div v-if="isAdmin(store.schema.actions[a.type])" class="perm-hint" style="width:100%">
        <span class="material-symbols-outlined">admin_panel_settings</span>需要管理员权限</div>
    </div>
    <div v-if="!rule.actions.length" class="rule-empty-hint">暂无动作，点击下方添加</div>
    <div class="rule-actions-bar">
      <button class="btn btn-text btn-sm" @click="addAction"><span class="material-symbols-outlined">add</span>添加动作</button>
    </div>
  </div>
</template>
