<script setup>
import { computed } from 'vue'
import { store } from '../lib/store'
import { getParamDefs, buildDefaultParams } from '../lib/utils'
import ParamInput from './ParamInput.vue'

const props = defineProps({ rule: Object })
const emit = defineEmits(['delete'])

const triggerKeys = computed(() => Object.keys(store.schema.triggers))
const actionKeys = computed(() => Object.keys(store.schema.actions))
const isCondition = computed(() => !!props.rule.condition)

function isAdmin(m) { return !!(m && (m.permissions || []).includes('admin')) }
function evParams(ev) { return getParamDefs(store.schema.triggers[ev.type]) }
function actParams(a) { return getParamDefs(store.schema.actions[a.type]) }

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
  <div class="rule-card">
    <div class="rule-head">
      <label class="field field-grow"><span class="field-label">规则名称</span>
        <input class="text-field" type="text" v-model="rule.name" placeholder="输入规则名称"></label>
      <button class="icon-btn icon-btn-danger" @click="emit('delete')" title="删除规则">
        <span class="material-symbols-outlined">delete</span></button>
    </div>

    <!-- OR 条件模式 -->
    <div v-if="isCondition" class="rule-section">
      <div class="rule-section-title"><span><span class="material-symbols-outlined">alt_route</span>触发条件 (OR)</span>
        <button class="btn btn-text btn-sm" @click="addCondition"><span class="material-symbols-outlined">add</span>添加条件</button></div>
      <div v-for="(ev, eIdx) in rule.condition.events" :key="eIdx" class="input-row bordered">
        <label class="field field-narrow"><span class="field-label">触发器 {{ eIdx + 1 }}</span>
          <select class="select" :value="ev.type" @change="changeCondTrigger(eIdx, $event.target.value)">
            <option v-for="k in triggerKeys" :key="k" :value="k">{{ store.schema.triggers[k].name || k }}</option>
          </select></label>
        <ParamInput v-for="p in evParams(ev)" :key="p.name" :def="p" v-model="ev.params[p.name]" />
        <button class="icon-btn icon-btn-danger" @click="removeCondition(eIdx)" title="移除">
          <span class="material-symbols-outlined">close</span></button>
      </div>
      <button class="btn btn-text btn-sm" @click="toggleMode" style="margin-top:6px">
        <span class="material-symbols-outlined">swap_horiz</span>切换到单事件模式</button>
    </div>

    <!-- 单事件模式 -->
    <div v-else class="rule-section">
      <div class="rule-section-title"><span><span class="material-symbols-outlined">memory</span>触发事件</span>
        <button class="btn btn-text btn-sm" @click="toggleMode"><span class="material-symbols-outlined">alt_route</span>改用 OR 条件</button></div>
      <div class="input-row">
        <label class="field field-narrow"><span class="field-label">选择触发器</span>
          <select class="select" :value="rule.event.type" @change="changeTrigger($event.target.value)">
            <option v-for="k in triggerKeys" :key="k" :value="k">{{ store.schema.triggers[k].name || k }}{{ isAdmin(store.schema.triggers[k]) ? ' [管理员]' : '' }}</option>
          </select></label>
        <ParamInput v-for="p in evParams(rule.event)" :key="p.name" :def="p" v-model="rule.event.params[p.name]" />
      </div>
    </div>

    <!-- 执行动作 -->
    <div class="rule-section">
      <div class="rule-section-title"><span><span class="material-symbols-outlined">bolt</span>执行动作</span>
        <button class="btn btn-text btn-sm" @click="addAction"><span class="material-symbols-outlined">add</span>添加动作</button></div>
      <div v-for="(a, aIdx) in rule.actions" :key="aIdx" class="action-row">
        <label class="field field-narrow"><span class="field-label">动作类型</span>
          <select class="select" :value="a.type" @change="changeAction(aIdx, $event.target.value)">
            <option v-for="k in actionKeys" :key="k" :value="k">{{ store.schema.actions[k].name || k }}{{ isAdmin(store.schema.actions[k]) ? ' [管理员]' : '' }}</option>
          </select></label>
        <div v-if="isAdmin(store.schema.actions[a.type])" class="perm-hint">
          <span class="material-symbols-outlined">admin_panel_settings</span>需要管理员权限</div>
        <ParamInput v-for="p in actParams(a)" :key="p.name" :def="p" v-model="a.params[p.name]" />
        <button class="icon-btn icon-btn-danger" @click="removeAction(aIdx)" title="移除动作">
          <span class="material-symbols-outlined">close</span></button>
      </div>
    </div>
  </div>
</template>
