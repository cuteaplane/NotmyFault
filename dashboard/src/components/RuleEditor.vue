<script setup>
import { computed, ref, watchEffect } from 'vue'
import { store } from '../lib/store'
import { getVisibleParamDefs, buildDefaultParams, groupTriggerKeys } from '../lib/utils'
import ParamInput from './ParamInput.vue'
import ConditionEditor from './ConditionEditor.vue'

const props = defineProps({ rule: Object })
const emit = defineEmits(['back', 'delete', 'save'])

const activePanel = ref('trigger')
const selectedEvent = ref(null)
const selectedAction = ref(0)
const selectedPrecondition = ref(0)

const triggerKeys = computed(() => Object.keys(store.schema.triggers))
const triggerGroups = computed(() => groupTriggerKeys(triggerKeys.value))
const actionKeys = computed(() => Object.keys(store.schema.actions))
const preconditionKeys = computed(() => actionKeys.value.filter(
  key => store.schema.actions[key]?.precondition_api === 'context-v1'
))
const isCondition = computed(() => !!props.rule.condition)

function normalizeCondition(condition) {
  if (Array.isArray(condition.children)) return condition
  const legacyEvents = Array.isArray(condition.events) ? condition.events : []
  condition.op = condition.op || (condition.type === 'and' ? 'all' : 'any')
  condition.children = legacyEvents
  delete condition.events
  delete condition.type
  return condition
}
const conditionNode = computed(() => props.rule.condition ? normalizeCondition(props.rule.condition) : null)

function flattenEvents(node, depth = 0, parentOp = null, result = []) {
  for (const child of node?.children || []) {
    if (child?.type && !child.children && !child.events) {
      result.push({ event: child, depth, parentOp })
    } else {
      flattenEvents(child, depth + 1, child.op, result)
    }
  }
  return result
}
const conditionEvents = computed(() => flattenEvents(conditionNode.value))
const visibleEvents = computed(() => isCondition.value
  ? conditionEvents.value
  : (props.rule.event ? [{ event: props.rule.event, depth: 0, parentOp: null }] : []))
const currentEvent = computed(() => {
  if (!isCondition.value) return props.rule.event
  return selectedEvent.value || conditionEvents.value[0]?.event || null
})
const currentAction = computed(() => props.rule.actions?.[selectedAction.value] || null)
const currentPrecondition = computed(() => props.rule.preconditions?.[selectedPrecondition.value] || null)
const rootLogicLabel = computed(() => conditionNode.value?.op === 'all' ? 'AND · 全部满足' : 'OR · 任一满足')

watchEffect(() => {
  if (isCondition.value && !conditionEvents.value.some(item => item.event === selectedEvent.value)) {
    selectedEvent.value = conditionEvents.value[0]?.event || null
  }
  if (selectedAction.value >= (props.rule.actions?.length || 0)) selectedAction.value = Math.max(0, (props.rule.actions?.length || 1) - 1)
  if (selectedPrecondition.value >= (props.rule.preconditions?.length || 0)) selectedPrecondition.value = Math.max(0, (props.rule.preconditions?.length || 1) - 1)
})

function isAdmin(meta) { return !!(meta?.permissions || []).includes('admin') }
function eventParams(event) { return getVisibleParamDefs(store.schema.triggers[event.type], event.params) }
function actionParams(action) { return getVisibleParamDefs(store.schema.actions[action.type], action.params) }
function preconditionParams(item) { return getVisibleParamDefs(store.schema.actions[item.type], item.params) }
function eventName(event) { return store.schema.triggers[event?.type]?.name || event?.type || '未配置触发器' }
function actionName(action) { return store.schema.actions[action?.type]?.name || action?.type || '未配置动作' }
function defaultEvent() {
  const type = triggerKeys.value[0] || 'unknown'
  return { type, params: buildDefaultParams(store.schema.triggers[type]) }
}

function selectEvent(event) {
  selectedEvent.value = event
  activePanel.value = 'trigger'
}
function changeCurrentEvent(type) {
  if (!currentEvent.value) return
  currentEvent.value.type = type
  currentEvent.value.params = buildDefaultParams(store.schema.triggers[type])
}
function changeAction(type) {
  if (!currentAction.value) return
  currentAction.value.type = type
  currentAction.value.params = buildDefaultParams(store.schema.actions[type])
}
function changePrecondition(type) {
  if (!currentPrecondition.value) return
  currentPrecondition.value.type = type
  currentPrecondition.value.params = buildDefaultParams(store.schema.actions[type])
}
function upgradeToConditions() {
  const initial = props.rule.event || defaultEvent()
  props.rule.condition = { op: 'any', children: [{ type: initial.type, params: { ...initial.params } }] }
  delete props.rule.event
  selectedEvent.value = props.rule.condition.children[0]
  activePanel.value = 'trigger'
}
function useSingleEvent() {
  const first = conditionEvents.value[0]?.event || defaultEvent()
  props.rule.event = { type: first.type, params: { ...first.params } }
  delete props.rule.condition
  selectedEvent.value = null
  activePanel.value = 'trigger'
}
function addCondition() {
  if (!isCondition.value) { upgradeToConditions(); return }
  const event = defaultEvent()
  conditionNode.value.children.push(event)
  selectEvent(event)
}
function removeEvent(target) {
  function removeFrom(node) {
    const index = (node.children || []).indexOf(target)
    if (index >= 0) { node.children.splice(index, 1); return true }
    return (node.children || []).some(child => child.children && removeFrom(child))
  }
  removeFrom(conditionNode.value)
  selectedEvent.value = conditionEvents.value[0]?.event || null
}
function addAction() {
  const type = actionKeys.value[0] || 'unknown'
  props.rule.actions.push({ type, params: buildDefaultParams(store.schema.actions[type]) })
  selectedAction.value = props.rule.actions.length - 1
  activePanel.value = 'action'
}
function removeAction() {
  props.rule.actions.splice(selectedAction.value, 1)
}
function addPrecondition() {
  const type = preconditionKeys.value[0]
  if (!type) return
  if (!Array.isArray(props.rule.preconditions)) props.rule.preconditions = []
  props.rule.preconditions.push({ type, params: buildDefaultParams(store.schema.actions[type]) })
  selectedPrecondition.value = props.rule.preconditions.length - 1
  activePanel.value = 'precondition'
}
function removePrecondition() { props.rule.preconditions.splice(selectedPrecondition.value, 1) }
function actionOutputHint(action, index) {
  const outputs = store.schema.actions[action.type]?.outputs || []
  if (!outputs.length) return ''
  const step = `${action.type}_${index + 1}`
  return outputs.map(key => `{{ steps.${step}.result.${key} }}`).join('　')
}
</script>

<template>
  <section class="rule-editor-page">
    <header class="rule-editor-head">
      <button class="btn btn-text" @click="emit('back')"><span class="material-symbols-outlined">arrow_back</span>全部规则</button>
      <div class="rule-editor-title">
        <input v-model="rule.name" class="rule-editor-name" placeholder="未命名规则">
        <label class="rule-folder-field"><span class="material-symbols-outlined">folder</span><input v-model="rule.folder" placeholder="未分类"></label>
      </div>
      <div class="rule-editor-actions">
        <button class="btn btn-text danger-text" @click="emit('delete')"><span class="material-symbols-outlined">delete</span>删除</button>
        <button class="btn btn-filled" @click="emit('save')"><span class="material-symbols-outlined">save</span>保存配置</button>
      </div>
    </header>

    <div class="rule-editor-layout">
      <aside class="rule-editor-nav">
        <div class="rule-nav-label">触发条件</div>
        <button class="rule-nav-item logic-nav" :class="{ active: activePanel === 'logic' }" @click="activePanel = 'logic'">
          <span class="logic-orb" :class="conditionNode?.op || 'single'">{{ isCondition ? (conditionNode.op === 'all' ? 'AND' : 'OR') : 'ONE' }}</span>
          <span><b>{{ isCondition ? rootLogicLabel : '单个触发器' }}</b><small>条件关系</small></span>
        </button>
        <div class="rule-nav-tree">
          <button v-for="(item, index) in visibleEvents" :key="item.event" class="rule-nav-item trigger-nav"
            :class="{ active: activePanel === 'trigger' && currentEvent === item.event }"
            :style="{ '--tree-depth': item.depth }" @click="selectEvent(item.event)">
            <span class="trigger-index">{{ String(index + 1).padStart(2, '0') }}</span>
            <span><b>{{ eventName(item.event) }}</b><small>{{ item.parentOp === 'all' ? '需同时满足' : item.parentOp === 'any' ? '任一即可' : '唯一入口' }}</small></span>
          </button>
        </div>
        <button class="rule-nav-add" @click="addCondition"><span class="material-symbols-outlined">add</span>{{ isCondition ? '添加触发条件' : '改为多条件' }}</button>

        <div v-if="preconditionKeys.length" class="rule-nav-label separated">执行前检查</div>
        <button v-for="(item, index) in (rule.preconditions || [])" :key="item" class="rule-nav-item compact-nav"
          :class="{ active: activePanel === 'precondition' && selectedPrecondition === index }" @click="selectedPrecondition = index; activePanel = 'precondition'">
          <span class="material-symbols-outlined">verified_user</span><span>{{ actionName(item) }}</span>
        </button>
        <button v-if="preconditionKeys.length" class="rule-nav-add" @click="addPrecondition"><span class="material-symbols-outlined">add</span>添加检查</button>

        <div class="rule-nav-label separated">执行动作</div>
        <button v-for="(item, index) in rule.actions" :key="item" class="rule-nav-item compact-nav"
          :class="{ active: activePanel === 'action' && selectedAction === index }" @click="selectedAction = index; activePanel = 'action'">
          <span class="step-dot">{{ index + 1 }}</span><span>{{ actionName(item) }}</span>
        </button>
        <button class="rule-nav-add" @click="addAction"><span class="material-symbols-outlined">add</span>添加动作</button>
      </aside>

      <main class="rule-editor-content">
        <template v-if="activePanel === 'trigger' && currentEvent">
          <div class="inspector-topline"><span class="section-caption">触发条件</span><span class="status-pill">条件 {{ visibleEvents.findIndex(item => item.event === currentEvent) + 1 }}</span></div>
          <h2>配置这个触发器</h2>
          <p class="inspector-lead">选择触发器类型并填写参数。</p>
          <section class="inspector-card trigger-detail-card">
            <label class="field field-wide"><span class="field-label">触发器类型</span>
              <select class="select" :value="currentEvent.type" @change="changeCurrentEvent($event.target.value)">
                <optgroup v-for="([group, keys]) in triggerGroups" :key="group" :label="group">
                  <option v-for="key in keys" :key="key" :value="key">{{ store.schema.triggers[key].name || key }}{{ isAdmin(store.schema.triggers[key]) ? ' [管理员]' : '' }}</option>
                </optgroup>
              </select>
            </label>
            <div class="param-grid">
              <ParamInput v-for="param in eventParams(currentEvent)" :key="param.name" :def="param" v-model="currentEvent.params[param.name]" />
            </div>
            <div v-if="isAdmin(store.schema.triggers[currentEvent.type])" class="perm-hint"><span class="material-symbols-outlined">admin_panel_settings</span>此触发器需要管理员权限</div>
          </section>
          <div class="inspector-footer">
            <button v-if="isCondition" class="btn btn-text danger-text" @click="removeEvent(currentEvent)"><span class="material-symbols-outlined">delete</span>移除此条件</button>
            <button v-else class="btn btn-tonal" @click="upgradeToConditions"><span class="material-symbols-outlined">alt_route</span>升级为 AND / OR 条件</button>
          </div>
        </template>

        <template v-else-if="activePanel === 'logic'">
          <div class="inspector-topline"><span class="section-caption">触发条件</span><span class="status-pill logic-status">{{ isCondition ? rootLogicLabel : '单个触发器' }}</span></div>
          <h2>条件关系</h2>
          <p class="inspector-lead">设置条件之间的 AND / OR 关系；具体参数在左侧选择对应触发器后编辑。</p>
          <section v-if="isCondition" class="logic-editor-card"><ConditionEditor :node="conditionNode" /></section>
          <section v-else class="logic-empty-card">
            <span class="material-symbols-outlined">alt_route</span><div><b>当前只有一个触发器</b><p>增加条件后，可自由选择 OR（任一）或 AND（全部）关系。</p></div>
            <button class="btn btn-filled" @click="upgradeToConditions">开始组合</button>
          </section>
          <div v-if="isCondition" class="inspector-footer"><button class="btn btn-tonal" @click="useSingleEvent"><span class="material-symbols-outlined">filter_1</span>改回单个触发器</button></div>
        </template>

        <template v-else-if="activePanel === 'action' && currentAction">
          <div class="inspector-topline"><span class="section-caption">执行动作</span><span class="status-pill">步骤 {{ selectedAction + 1 }}</span></div>
          <h2>{{ actionName(currentAction) }}</h2>
          <p class="inspector-lead">动作将按左侧顺序执行。</p>
          <section class="inspector-card">
            <label class="field field-wide"><span class="field-label">动作类型</span>
              <select class="select" :value="currentAction.type" @change="changeAction($event.target.value)">
                <option v-for="key in actionKeys" :key="key" :value="key">{{ store.schema.actions[key].name || key }}{{ isAdmin(store.schema.actions[key]) ? ' [管理员]' : '' }}</option>
              </select>
            </label>
            <div class="param-grid"><ParamInput v-for="param in actionParams(currentAction)" :key="param.name" :def="param" v-model="currentAction.params[param.name]" /></div>
            <div v-if="isAdmin(store.schema.actions[currentAction.type])" class="perm-hint"><span class="material-symbols-outlined">admin_panel_settings</span>此动作需要管理员权限</div>
            <div v-if="actionOutputHint(currentAction, selectedAction)" class="workflow-hint workflow-output-hint">后续步骤可引用：<code>{{ actionOutputHint(currentAction, selectedAction) }}</code></div>
          </section>
          <div class="inspector-footer"><button class="btn btn-text danger-text" @click="removeAction"><span class="material-symbols-outlined">delete</span>移除此动作</button></div>
        </template>

        <template v-else-if="activePanel === 'precondition' && currentPrecondition">
          <div class="inspector-topline"><span class="section-caption">执行前检查</span><span class="status-pill">检查</span></div>
          <h2>{{ actionName(currentPrecondition) }}</h2>
          <p class="inspector-lead">所有检查通过后才启动动作；不通过时引擎会安全地延后重试。</p>
          <section class="inspector-card">
            <label class="field field-wide"><span class="field-label">检查类型</span>
              <select class="select" :value="currentPrecondition.type" @change="changePrecondition($event.target.value)">
                <option v-for="key in preconditionKeys" :key="key" :value="key">{{ store.schema.actions[key].name || key }}</option>
              </select>
            </label>
            <div class="param-grid"><ParamInput v-for="param in preconditionParams(currentPrecondition)" :key="param.name" :def="param" v-model="currentPrecondition.params[param.name]" /></div>
          </section>
          <div class="inspector-footer"><button class="btn btn-text danger-text" @click="removePrecondition"><span class="material-symbols-outlined">delete</span>移除此检查</button></div>
        </template>

        <section v-else class="empty-state rule-editor-empty"><span class="material-symbols-outlined">account_tree</span><h3>请选择一项</h3><p>从左侧选择触发条件、检查或动作进行编辑。</p></section>
      </main>
    </div>
  </section>
</template>
