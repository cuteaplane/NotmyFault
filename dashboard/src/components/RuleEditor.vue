<script setup>
import { computed, ref } from 'vue'
import { store } from '../lib/store'
import { getVisibleParamDefs, buildDefaultParams, groupTriggerKeys } from '../lib/utils'
import ParamInput from './ParamInput.vue'
import ConditionEditor from './ConditionEditor.vue'

const props = defineProps({ rule: Object, dirty: Boolean })
const emit = defineEmits(['back', 'delete', 'save', 'save-run'])

const newTriggerType = ref('')
const newActionType = ref('')
const newPreconditionType = ref('')
const triggerKeys = computed(() => Object.keys(store.schema.triggers))
const triggerGroups = computed(() => groupTriggerKeys(triggerKeys.value))
const actionKeys = computed(() => Object.keys(store.schema.actions))
const preconditionKeys = computed(() => actionKeys.value.filter(
  key => store.schema.actions[key]?.precondition_api === 'context-v1'
))
const isCondition = computed(() => (
  !!props.rule.condition
  && typeof props.rule.condition === 'object'
  && !Array.isArray(props.rule.condition)
))

function normalizeCondition(condition) {
  if (!condition || typeof condition !== 'object' || Array.isArray(condition)) return condition
  if (!Array.isArray(condition.children)) {
    condition.op = condition.op || (condition.type === 'and' ? 'all' : 'any')
    condition.children = Array.isArray(condition.events) ? condition.events : []
    delete condition.events
    delete condition.type
  }
  condition.children.forEach(child => {
    if (child && typeof child === 'object' && (child.children || child.events)) normalizeCondition(child)
  })
  return condition
}
const conditionNode = computed(() => props.rule.condition ? normalizeCondition(props.rule.condition) : null)
const isAdmin = (meta) => !!(meta?.permissions || []).includes('admin')
const eventParams = (event) => getVisibleParamDefs(store.schema.triggers[event.type], event.params)
const actionParams = (action) => getVisibleParamDefs(store.schema.actions[action.type], action.params)
const eventName = (event) => store.schema.triggers[event?.type]?.name || event?.type || '未选择触发器'
const actionName = (action) => store.schema.actions[action?.type]?.name || action?.type || '未选择动作'

const validationIssues = computed(() => {
  const issues = []
  function validateCondition(node, path = '触发条件') {
    if (!node || typeof node !== 'object') { issues.push(`${path}格式无效`); return }
    const leaf = !!node.type && !node.children && !node.events
    if (leaf) {
      if (!store.schema.triggers[node.type]) issues.push(`${path}引用了不可用的触发器`)
      if (node.params != null && (typeof node.params !== 'object' || Array.isArray(node.params))) issues.push(`${path}参数格式无效`)
      return
    }
    if (!['any', 'all'].includes(node.op)) issues.push(`${path}的组合方式无效`)
    if (!Array.isArray(node.children) || !node.children.length) {
      issues.push(`${path}组不能为空`)
      return
    }
    if ('within_seconds' in node) {
      const seconds = Number(node.within_seconds)
      if (!Number.isFinite(seconds) || seconds <= 0) issues.push(`${path}的时间窗口必须大于 0`)
    }
    node.children.forEach((child, index) => validateCondition(child, `${path} ${index + 1}`))
  }
  if (!String(props.rule.name || '').trim()) issues.push('请填写规则名称')
  if (props.rule.event && props.rule.condition) issues.push('单个触发条件和组合条件不能同时存在')
  if (!props.rule.event && !props.rule.condition) issues.push('请选择至少一个触发条件')
  if (props.rule.event) validateCondition(props.rule.event)
  if (props.rule.condition) validateCondition(conditionNode.value)
  const actions = Array.isArray(props.rule.actions) ? props.rule.actions : []
  if (!Array.isArray(props.rule.actions)) issues.push('动作列表格式无效')
  if (!actions.length) issues.push('请添加至少一个执行动作')
  actions.forEach((action, index) => {
    if (!store.schema.actions[action?.type]) issues.push(`动作 ${index + 1} 引用了不可用的插件`)
    if (action?.params != null && (typeof action.params !== 'object' || Array.isArray(action.params))) issues.push(`动作 ${index + 1} 参数格式无效`)
  })
  const preconditions = props.rule.preconditions == null
    ? []
    : Array.isArray(props.rule.preconditions) ? props.rule.preconditions : null
  if (preconditions === null) issues.push('开始前确认列表格式无效')
  ;(preconditions || []).forEach((item, index) => {
    if (!preconditionKeys.value.includes(item?.type)) issues.push(`开始前确认 ${index + 1} 不可用`)
    if (item?.params != null && (typeof item.params !== 'object' || Array.isArray(item.params))) issues.push(`开始前确认 ${index + 1} 参数格式无效`)
  })
  return issues
})

function setSingleTrigger(type) {
  if (!type) return
  props.rule.event = { type, params: buildDefaultParams(store.schema.triggers[type]) }
  delete props.rule.condition
  newTriggerType.value = ''
}
function changeSingleTrigger(type) { setSingleTrigger(type) }
function upgradeToConditions() {
  const initial = props.rule.event
  props.rule.condition = {
    op: 'any',
    children: initial ? [{ type: initial.type, params: { ...initial.params } }] : [],
  }
  delete props.rule.event
}
function useSingleEvent() {
  function firstEvent(node) {
    for (const child of node?.children || []) {
      if (child?.type && !child.children && !child.events) return child
      const nested = firstEvent(child)
      if (nested) return nested
    }
    return null
  }
  const first = firstEvent(conditionNode.value)
  if (first) props.rule.event = { type: first.type, params: { ...first.params } }
  else delete props.rule.event
  delete props.rule.condition
}
function addAction() {
  const type = newActionType.value || actionKeys.value[0]
  if (!type) return
  if (!Array.isArray(props.rule.actions)) props.rule.actions = []
  props.rule.actions.push({ type, params: buildDefaultParams(store.schema.actions[type]) })
  newActionType.value = ''
}
function changeAction(action, type) {
  action.type = type
  action.params = buildDefaultParams(store.schema.actions[type])
}
function removeAction(index) { props.rule.actions.splice(index, 1) }
function moveAction(index, offset) {
  const target = index + offset
  if (target < 0 || target >= props.rule.actions.length) return
  const [action] = props.rule.actions.splice(index, 1)
  props.rule.actions.splice(target, 0, action)
}
function addPrecondition() {
  const type = newPreconditionType.value || preconditionKeys.value[0]
  if (!type) return
  if (!Array.isArray(props.rule.preconditions)) props.rule.preconditions = []
  props.rule.preconditions.push({ type, params: buildDefaultParams(store.schema.actions[type]) })
  newPreconditionType.value = ''
}
function changePrecondition(item, type) {
  item.type = type
  item.params = buildDefaultParams(store.schema.actions[type])
}
function actionOutputHint(action, index) {
  const outputs = store.schema.actions[action.type]?.outputs || []
  const step = `${action.type}_${index + 1}`
  return outputs.map(key => `{{ steps.${step}.result.${key} }}`).join('　')
}
</script>

<template>
  <section class="rule-editor-page flow-rule-editor">
    <header class="rule-editor-head flow-editor-head">
      <button class="btn btn-text" @click="emit('back')"><span class="material-symbols-outlined">arrow_back</span>全部规则</button>
      <div class="rule-editor-title">
        <input v-model="rule.name" class="rule-editor-name" placeholder="给这条规则起个名字">
        <label class="rule-folder-field"><span class="material-symbols-outlined">folder</span><input v-model="rule.folder" placeholder="未分类"></label>
      </div>
      <div class="rule-editor-actions">
        <span v-if="dirty" class="draft-state"><span></span>未保存</span>
        <button class="btn btn-text danger-text" @click="emit('delete')"><span class="material-symbols-outlined">delete</span>删除</button>
        <button class="btn btn-tonal" :disabled="validationIssues.length" @click="emit('save-run')"><span class="material-symbols-outlined">play_arrow</span>保存并运行</button>
        <button class="btn btn-filled" :disabled="validationIssues.length || !dirty" @click="emit('save')"><span class="material-symbols-outlined">save</span>保存规则</button>
      </div>
    </header>

    <main class="automation-flow">
      <section class="automation-stage stage-when">
        <div class="stage-rail"><span class="stage-node"><span class="material-symbols-outlined">bolt</span></span></div>
        <div class="stage-content">
          <header class="stage-head"><div><span class="stage-kicker">当</span><h2>什么情况会触发这条规则？</h2></div><span class="stage-required">必填</span></header>

          <ConditionEditor v-if="isCondition" :node="conditionNode" />
          <details v-else-if="rule.event" class="flow-card condition-flow-card" open>
            <summary>
              <span class="flow-card-index">1</span>
              <span class="flow-card-copy"><b>{{ eventName(rule.event) }}</b><small>唯一触发条件</small></span>
              <span v-if="isAdmin(store.schema.triggers[rule.event.type])" class="chip chip-admin">管理员</span>
              <span class="material-symbols-outlined flow-expand">expand_more</span>
            </summary>
            <div class="flow-card-body">
              <label class="field field-wide"><span class="field-label">触发方式</span>
                <select class="select" :value="rule.event.type" @change="changeSingleTrigger($event.target.value)">
                  <optgroup v-for="([group, keys]) in triggerGroups" :key="group" :label="group">
                    <option v-for="key in keys" :key="key" :value="key">{{ store.schema.triggers[key].name || key }}</option>
                  </optgroup>
                </select>
              </label>
              <div class="param-grid"><ParamInput v-for="param in eventParams(rule.event)" :key="param.name" :def="param" v-model="rule.event.params[param.name]" /></div>
            </div>
          </details>
          <div v-else class="flow-empty-card">
            <span class="material-symbols-outlined">touch_app</span>
            <div><b>选择触发方式</b><p>先说明什么时候开始执行，不默认替你选择。</p></div>
            <select v-model="newTriggerType" class="select">
              <option value="" disabled>选择触发器…</option>
              <optgroup v-for="([group, keys]) in triggerGroups" :key="group" :label="group">
                <option v-for="key in keys" :key="key" :value="key">{{ store.schema.triggers[key].name || key }}</option>
              </optgroup>
            </select>
            <button class="btn btn-filled" :disabled="!newTriggerType" @click="setSingleTrigger(newTriggerType)">添加</button>
          </div>
          <div class="flow-stage-actions">
            <button v-if="rule.event" class="btn btn-text btn-sm" @click="upgradeToConditions"><span class="material-symbols-outlined">alt_route</span>添加 AND / OR 条件</button>
            <button v-if="isCondition" class="btn btn-text btn-sm" @click="useSingleEvent"><span class="material-symbols-outlined">filter_1</span>改为单个条件</button>
          </div>
        </div>
      </section>

      <section v-if="preconditionKeys.length" class="automation-stage stage-check">
        <div class="stage-rail"><span class="stage-node"><span class="material-symbols-outlined">verified_user</span></span></div>
        <div class="stage-content">
          <header class="stage-head"><div><span class="stage-kicker">开始前确认</span><h2>执行前还需要确认什么？</h2><p>未通过时，引擎会稍后重试。</p></div><span class="stage-optional">可选</span></header>
          <details v-for="(item, index) in (rule.preconditions || [])" :key="item" class="flow-card check-flow-card">
            <summary><span class="material-symbols-outlined flow-kind-icon">verified</span><span class="flow-card-copy"><b>{{ actionName(item) }}</b><small>开始前确认</small></span><button class="icon-btn icon-btn-danger" title="移除确认" @click.prevent.stop="rule.preconditions.splice(index, 1)"><span class="material-symbols-outlined">delete</span></button><span class="material-symbols-outlined flow-expand">expand_more</span></summary>
            <div class="flow-card-body">
              <label class="field field-wide"><span class="field-label">确认方式</span><select class="select" :value="item.type" @change="changePrecondition(item, $event.target.value)"><option v-for="key in preconditionKeys" :key="key" :value="key">{{ store.schema.actions[key].name || key }}</option></select></label>
              <div class="param-grid"><ParamInput v-for="param in actionParams(item)" :key="param.name" :def="param" v-model="item.params[param.name]" /></div>
            </div>
          </details>
          <div class="flow-add-control"><select v-model="newPreconditionType" class="select"><option value="" disabled>选择确认方式…</option><option v-for="key in preconditionKeys" :key="key" :value="key">{{ store.schema.actions[key].name || key }}</option></select><button class="btn btn-tonal" @click="addPrecondition"><span class="material-symbols-outlined">add</span>添加确认</button></div>
        </div>
      </section>

      <section class="automation-stage stage-then">
        <div class="stage-rail stage-rail-last"><span class="stage-node"><span class="material-symbols-outlined">play_arrow</span></span></div>
        <div class="stage-content">
          <header class="stage-head"><div><span class="stage-kicker">然后</span><h2>按顺序执行这些动作</h2></div><span class="stage-required">必填</span></header>
          <details v-for="(action, index) in rule.actions" :key="action" class="flow-card action-flow-card" :open="rule.actions.length === 1">
            <summary>
              <span class="flow-card-index">{{ index + 1 }}</span><span class="flow-card-copy"><b>{{ actionName(action) }}</b><small>第 {{ index + 1 }} 步</small></span>
              <span v-if="isAdmin(store.schema.actions[action.type])" class="chip chip-admin">管理员</span>
              <span class="flow-card-tools"><button class="icon-btn" :disabled="index === 0" title="上移" @click.prevent.stop="moveAction(index, -1)"><span class="material-symbols-outlined">arrow_upward</span></button><button class="icon-btn" :disabled="index === rule.actions.length - 1" title="下移" @click.prevent.stop="moveAction(index, 1)"><span class="material-symbols-outlined">arrow_downward</span></button><button class="icon-btn icon-btn-danger" title="移除动作" @click.prevent.stop="removeAction(index)"><span class="material-symbols-outlined">delete</span></button></span>
              <span class="material-symbols-outlined flow-expand">expand_more</span>
            </summary>
            <div class="flow-card-body">
              <label class="field field-wide"><span class="field-label">动作类型</span><select class="select" :value="action.type" @change="changeAction(action, $event.target.value)"><option v-for="key in actionKeys" :key="key" :value="key">{{ store.schema.actions[key].name || key }}</option></select></label>
              <div class="param-grid"><ParamInput v-for="param in actionParams(action)" :key="param.name" :def="param" v-model="action.params[param.name]" /></div>
              <div v-if="actionOutputHint(action, index)" class="workflow-output-hint">后续步骤可引用：<code>{{ actionOutputHint(action, index) }}</code></div>
            </div>
          </details>
          <div v-if="!rule.actions?.length" class="flow-inline-empty">还没有动作。规则触发后不会执行任何操作。</div>
          <div class="flow-add-control"><select v-model="newActionType" class="select"><option value="" disabled>选择动作…</option><option v-for="key in actionKeys" :key="key" :value="key">{{ store.schema.actions[key].name || key }}</option></select><button class="btn btn-tonal" @click="addAction"><span class="material-symbols-outlined">add</span>添加动作</button></div>
        </div>
      </section>
    </main>

    <footer class="flow-validation" :class="{ valid: !validationIssues.length }">
      <span class="material-symbols-outlined">{{ validationIssues.length ? 'error' : 'check_circle' }}</span>
      <div><b>{{ validationIssues.length ? `${validationIssues.length} 项需要处理` : '规则可以保存' }}</b><p v-if="validationIssues.length">{{ validationIssues.join(' · ') }}</p><p v-else>修改只会在保存后应用到引擎。</p></div>
    </footer>
  </section>
</template>
