<script setup>
import { computed, watch, unref } from 'vue'
import { store } from '../lib/store'
import { ensureParams, getVisibleParamDefs } from '../lib/utils'
import { parameterAllowsBinding } from '../lib/bindings'
import { actionName } from '../lib/rulePresentation'
import ActionForm from './ActionForm.vue'
import ActionRunLimits from './ActionRunLimits.vue'
import { precedingSources, ifBranches } from '../lib/workflowConditions'
import TriggerForm from './TriggerForm.vue'
import ParamInput from './ParamInput.vue'

const props = defineProps({ rule: Object, node: Object, mutations: Object, constantSources: Array, hasTriggers: Boolean, layout: String })
const emit = defineEmits(['select-node', 'open-if'])
const selectedGraphNode = computed(() => props.node)
const selectedKind = computed(() => {
  return selectedGraphNode.value?.kind || 'trigger'
})
const selectedIndex = computed(() => selectedGraphNode.value?.index ?? -1)
const selectedAction = computed(() => selectedKind.value === 'action' ? selectedGraphNode.value.source : null)
const selectedFailureAction = computed(() => selectedKind.value === 'failure-action'
  ? selectedGraphNode.value.source
  : null)
watch(selectedFailureAction, action => { if (action) ensureParams(action) }, { immediate: true })
const actionParams = (action) => getVisibleParamDefs(store.schema.actions[action.type], action.params || {})
const actionParamAllowsBinding = (action, param) => parameterAllowsBinding(
  store.schema.actions[action?.type],
  param.name,
)
const selectedFailureOwner = computed(() => selectedGraphNode.value?.owner)
const selectedActionItems = computed(() => selectedGraphNode.value?.items || props.rule.actions)
const selectedBranch = computed(() => selectedKind.value === 'if-branch' ? ifBranches(selectedGraphNode.value.source).find(branch => branch.key === selectedGraphNode.value.branchKey) : null)
const actionSources = computed(() => {
  const action = selectedAction.value || selectedFailureAction.value
  return action ? precedingSources(props.rule, action, store.schema) : []
})
const useSingleCondition = (...args) => props.mutations.useSingleCondition(...args)
const wrapConditionInNot = (...args) => props.mutations.wrapConditionInNot(...args)
const unwrapNotEvent = (...args) => props.mutations.unwrapNotEvent(...args)
const changeConditionOp = (...args) => props.mutations.changeConditionOp(...args)
const removeSelectedCondition = (...args) => props.mutations.removeSelectedCondition(...args)
const moveSelectedCondition = (...args) => props.mutations.moveSelectedCondition(...args)
const duplicateSelectedCondition = (...args) => props.mutations.duplicateSelectedCondition(...args)
const removeAction = (...args) => props.mutations.removeAction(...args)
const duplicateAction = (...args) => props.mutations.duplicateAction(...args)
const moveAction = (...args) => props.mutations.moveAction(...args)
const removeFailureAction = (...args) => props.mutations.removeFailureAction(...args)
const duplicateFailureAction = (...args) => props.mutations.duplicateFailureAction(...args)
const moveFailureAction = (...args) => props.mutations.moveFailureAction(...args)
const requestAddFailureAction = (...args) => props.mutations.requestAddFailureAction(...args)
const requestReplaceFailureAction = (...args) => props.mutations.requestReplaceFailureAction(...args)
const openPluginPicker = (...args) => props.mutations.openPluginPicker(...args)
const requestConditionChild = (...args) => props.mutations.requestConditionChild(...args)
const selectedConditionNode = computed(() => unref(props.mutations.selectedConditionNode))
const selectedConditionPath = computed(() => unref(props.mutations.selectedConditionPath))
const selectedConditionParent = computed(() => unref(props.mutations.selectedConditionParent))
function setWithinSeconds(value) {
  if (value.trim() === '') delete selectedConditionNode.value.within_seconds
  else selectedConditionNode.value.within_seconds = Number(value)
}
function setTtlSeconds(value) {
  if (value.trim() === '') delete selectedConditionNode.value.ttl_seconds
  else selectedConditionNode.value.ttl_seconds = Number(value)
}
</script>

<template>
        <aside class="node-inspector" role="complementary" aria-label="节点设置" @pointerdown.stop>
          <header class="node-inspector-head">
            <span class="material-symbols-outlined">
              {{ selectedKind === 'invalid' ? 'error' : selectedKind === 'condition' ? 'alt_route' : selectedKind === 'trigger' ? 'bolt' : selectedKind === 'failure-action' ? 'build' : selectedKind === 'action' ? 'play_arrow' : 'add' }}
            </span>
            <div><small>节点设置</small><h2>
              {{ selectedKind === 'invalid' ? '无效条件' : selectedKind === 'condition' ? '逻辑组' : selectedKind === 'trigger' ? '触发条件' : selectedKind === 'failure-action' ? `补救动作 ${selectedIndex + 1}` : selectedKind === 'action' ? `动作 ${selectedIndex + 1}` : ['if-branch', 'if-merge'].includes(selectedKind) ? selectedGraphNode.kicker : '添加动作' }}
            </h2></div>
            <button class="icon-btn node-inspector-close" title="关闭设置" @click="emit('select-node', null)"><span class="material-symbols-outlined">close</span></button>
          </header>
          <div class="node-inspector-body">
            <template v-if="selectedKind === 'invalid'">
              <p class="inspector-lead">这个条件节点格式无效，无法编辑。删除后可从所属逻辑组重新添加。</p>
              <button class="btn btn-text btn-sm danger-text" @click="removeSelectedCondition"><span class="material-symbols-outlined">delete</span>删除无效节点</button>
            </template>

            <template v-else-if="selectedKind === 'condition' && selectedConditionNode">
              <p class="inspector-lead">进入此节点的分支会按这里的逻辑汇合，再执行后续步骤。</p>
              <label class="field"><span class="field-label">组合方式</span>
                <select v-model="selectedConditionNode.op" class="select" @change="changeConditionOp">
                  <option value="any">任一满足（OR）</option>
                  <option value="all">全部满足（AND）</option>
                  <option value="not">未发生（NOT）</option>
                </select>
              </label>
              <label v-if="['all', 'not'].includes(selectedConditionNode.op)" class="field"><span class="field-label">{{ selectedConditionNode.op === 'not' ? '等待时长（秒）' : '完成时间窗口（秒，可选）' }}</span>
                <input :value="selectedConditionNode.within_seconds ?? ''" @input="setWithinSeconds($event.target.value)" type="number" min="0.001" step="any" class="text-field" placeholder="不限制">
              </label>
              <template v-if="selectedConditionNode.op === 'all'">
                <label class="field"><span class="field-label">满足后保留哪些事件</span>
                  <select v-model="selectedConditionNode.consume" class="select">
                    <option value="matched">移除本次使用的事件</option>
                    <option value="all">移除组内已收到的事件</option>
                    <option value="none">保留，供后续组合使用</option>
                  </select>
                </label>
                <label class="field"><span class="field-label">事件保留时长（秒）</span>
                  <input :value="selectedConditionNode.ttl_seconds ?? ''" @input="setTtlSeconds($event.target.value)" type="number" min="0.001" step="any" class="text-field" :placeholder="selectedConditionNode.within_seconds ? '使用完成时间窗口' : '3600'">
                </label>
                <p class="inspector-lead">过期事件不参与后续组合。没有时间窗口时默认保留一小时。</p>
              </template>
              <label v-if="selectedConditionNode.op === 'any'" class="field"><span class="field-label">多条分支满足时</span>
                <select v-model="selectedConditionNode.pick" class="select">
                  <option value="latest">使用最新满足的分支</option>
                  <option value="first">使用列表中第一条满足的分支</option>
                </select>
              </label>
              <template v-if="selectedConditionNode.op === 'not'">
                <label class="field"><span class="field-label">持续没有事件时</span>
                  <select v-model="selectedConditionNode.repeat" class="select">
                    <option value="once_until_seen">触发一次，收到事件后重新计时</option>
                    <option value="every_window">每个等待窗口触发一次</option>
                  </select>
                </label>
                <p class="inspector-lead">只放一个事件。等待期间收到事件会重新计时。</p>
              </template>
              <button v-if="selectedConditionNode.op === 'not'" class="btn btn-text btn-sm" @click="unwrapNotEvent">改为事件发生时</button>
              <div v-if="selectedConditionNode.op !== 'not' || !selectedConditionNode.children?.length" class="inspector-add-grid">
                <button class="btn btn-tonal btn-sm" :disabled="!hasTriggers" @click="requestConditionChild(selectedGraphNode, false)"><span class="material-symbols-outlined">add</span>添加条件</button>
                <button v-if="selectedConditionNode.op !== 'not'" class="btn btn-tonal btn-sm" :disabled="!hasTriggers" @click="requestConditionChild(selectedGraphNode, true)"><span class="material-symbols-outlined">account_tree</span>添加子组</button>
              </div>
              <div v-if="selectedConditionPath.length" class="inspector-action-row">
                <button class="btn btn-text btn-sm" @click="moveSelectedCondition(-1)"><span class="material-symbols-outlined">arrow_upward</span>上移</button>
                <button class="btn btn-text btn-sm" @click="moveSelectedCondition(1)"><span class="material-symbols-outlined">arrow_downward</span>下移</button>
                <button class="btn btn-text btn-sm" @click="duplicateSelectedCondition"><span class="material-symbols-outlined">content_copy</span>复制</button>
                <button class="btn btn-text btn-sm danger-text" @click="removeSelectedCondition"><span class="material-symbols-outlined">delete</span>删除</button>
              </div>
              <button v-else class="btn btn-text btn-sm inspector-switch" @click="useSingleCondition"><span class="material-symbols-outlined">filter_1</span>改为单个条件</button>
            </template>

            <template v-else-if="selectedKind === 'trigger'">
              <template v-if="selectedConditionNode">
                <p class="inspector-lead">{{ selectedConditionPath.length ? '此事件连接到所属逻辑组。' : '此事件是规则的触发条件。' }}</p>
                <button v-if="selectedConditionParent?.op !== 'not'" class="btn btn-tonal btn-sm" @click="wrapConditionInNot()">未发生时（NOT）</button>
                <TriggerForm :node="selectedConditionNode" :rule="rule" :sources="constantSources"
                  @replace="openPluginPicker('trigger', { mode: 'replace-condition-trigger', path: selectedConditionPath, title: '更换触发方式' })" />
                <div v-if="selectedConditionPath.length" class="inspector-action-row">
                  <button class="btn btn-text btn-sm" @click="moveSelectedCondition(-1)"><span class="material-symbols-outlined">arrow_upward</span>上移</button>
                  <button class="btn btn-text btn-sm" @click="moveSelectedCondition(1)"><span class="material-symbols-outlined">arrow_downward</span>下移</button>
                  <button class="btn btn-text btn-sm" @click="duplicateSelectedCondition"><span class="material-symbols-outlined">content_copy</span>复制</button>
                  <button class="btn btn-text btn-sm danger-text" @click="removeSelectedCondition"><span class="material-symbols-outlined">delete</span>删除</button>
                </div>
                <div v-else class="inspector-upgrade-grid">
                  <button class="btn btn-tonal btn-sm" @click="openPluginPicker('trigger', { mode: 'combine-root', op: 'all', title: '添加“并且”条件' })"><span class="material-symbols-outlined">done_all</span>并且满足</button>
                  <button class="btn btn-tonal btn-sm" @click="openPluginPicker('trigger', { mode: 'combine-root', op: 'any', title: '添加“或者”条件' })"><span class="material-symbols-outlined">alt_route</span>或者满足</button>
                </div>
              </template>
              <template v-else>
                <p class="inspector-lead">选择一个事件作为流程起点。</p>
                <button class="btn btn-filled inspector-primary" @click="openPluginPicker('trigger', { mode: 'set-trigger', title: '选择触发方式' })">选择触发方式</button>
              </template>
            </template>

            <template v-else-if="selectedKind === 'if-branch' && selectedBranch">
              <h3>{{ selectedBranch.label }}</h3>
              <p class="inspector-lead">{{ selectedBranch.actions.length ? '动作按连线顺序执行，结束后接回公共后续流程。' : '此分支为空，直接接回公共后续流程。' }}</p>
              <button v-if="selectedBranch.condition" class="btn btn-text btn-sm" @click="emit('open-if', selectedGraphNode.source, selectedBranch.key)">编辑分支条件</button>
              <div class="flow-add-row">
                <button class="btn btn-tonal btn-sm" @click="mutations.requestBranchAction(selectedGraphNode)">添加动作</button>
                <button class="btn btn-text btn-sm" @click="mutations.addBranchAction(selectedGraphNode, 'if')">添加 IF</button>
                <button class="btn btn-text btn-sm" @click="mutations.addBranchAction(selectedGraphNode, 'set_variable')">变量赋值</button>
                <button class="btn btn-text btn-sm" @click="mutations.addBranchAction(selectedGraphNode, 'wait')">等待</button>
              </div>
            </template>
            <template v-else-if="selectedKind === 'if-merge'">
              <p class="inspector-lead">IF、ELIF、ELSE 中执行的分支在这里接回公共后续流程。</p>
            </template>

            <template v-else-if="selectedKind === 'action' && selectedAction">
              <template v-if="selectedAction.type === 'if' && layout === 'canvas'">
                <p class="inspector-lead">{{ selectedGraphNode.meta }}</p>
                <button class="btn btn-tonal btn-sm" @click="emit('open-if', selectedAction)">编辑条件子图</button>
              </template>
              <ActionForm v-else :action="selectedAction" :rule="rule" :index="selectedIndex" :binding-sources="actionSources"
                :failure-binding-sources="index => precedingSources(rule, selectedAction.failure_actions[index], store.schema)"
                @replace="openPluginPicker('action', { mode: 'replace-action', actionId: selectedAction.binding_id, index: selectedIndex, title: '更换动作类型' })"
                @add-failure-action="requestAddFailureAction(selectedIndex)"
                @replace-failure-action="failureIndex => requestReplaceFailureAction(selectedIndex, failureIndex)"
                @remove-failure-action="failureIndex => removeFailureAction(selectedIndex, failureIndex)"
                @move-failure-action="(failureIndex, offset) => moveFailureAction(selectedIndex, failureIndex, offset)" />
              <div class="inspector-action-row">
                <button class="btn btn-text btn-sm" :disabled="selectedIndex === 0" @click="moveAction(selectedIndex, -1)"><span class="material-symbols-outlined">arrow_back</span>提前</button>
                <button class="btn btn-text btn-sm" :disabled="selectedIndex === selectedActionItems.length - 1" @click="moveAction(selectedIndex, 1)">稍后<span class="material-symbols-outlined">arrow_forward</span></button>
                <button class="btn btn-text btn-sm" @click="duplicateAction(selectedIndex)"><span class="material-symbols-outlined">content_copy</span>复制</button>
                <button class="btn btn-text btn-sm danger-text" @click="removeAction(selectedIndex)"><span class="material-symbols-outlined">delete</span>删除</button>
              </div>
            </template>

            <template v-else-if="selectedKind === 'failure-action' && selectedFailureAction">
              <p class="inspector-lead">这个动作只会在上方主动作最终失败时执行。</p>
              <div class="field"><span class="field-label">补救动作类型</span>
                <button class="plugin-type-button" type="button"
                  @click="requestReplaceFailureAction(selectedGraphNode.parentIndex, selectedIndex)">
                  <span class="material-symbols-outlined">build</span>
                  <span>{{ actionName(selectedFailureAction) }}</span>
                  <span class="material-symbols-outlined">arrow_forward</span>
                </button>
              </div>
              <div class="param-grid"><ParamInput v-for="param in actionParams(selectedFailureAction)" :key="param.name" :def="param" :plugin-id="selectedFailureAction.type" v-model="selectedFailureAction.params[param.name]" :allow-binding="actionParamAllowsBinding(selectedFailureAction, param)" :binding-sources="actionSources" /></div>
              <label class="field"><span class="field-label">这个补救动作也失败时</span><select class="select" :value="selectedFailureAction.on_error === 'continue' ? 'continue' : 'stop'" @change="selectedFailureAction.on_error = $event.target.value"><option value="stop">停止剩余补救动作</option><option value="continue">继续执行剩余补救动作</option></select></label>
              <ActionRunLimits :action="selectedFailureAction" :meta="store.schema.actions[selectedFailureAction.type]" />
              <p class="failure-action-note">{{ selectedFailureAction.on_error === 'continue' ? '如果它也失败，会继续执行剩余补救动作。' : '如果它也失败，会停止剩余补救动作。' }}</p>
              <button class="btn btn-tonal btn-sm" @click="emit('select-node', `action-${selectedFailureOwner.binding_id}`)"><span class="material-symbols-outlined">tune</span>设置重试和失败处理</button>
              <div class="inspector-action-row">
                <button class="btn btn-text btn-sm" :disabled="selectedIndex === 0" @click="moveFailureAction(selectedGraphNode.parentIndex, selectedIndex, -1)"><span class="material-symbols-outlined">arrow_back</span>提前</button>
                <button class="btn btn-text btn-sm" :disabled="selectedIndex === selectedFailureOwner.failure_actions.length - 1" @click="moveFailureAction(selectedGraphNode.parentIndex, selectedIndex, 1)">稍后<span class="material-symbols-outlined">arrow_forward</span></button>
                <button class="btn btn-text btn-sm" @click="duplicateFailureAction(selectedGraphNode.parentIndex, selectedIndex)"><span class="material-symbols-outlined">content_copy</span>复制</button>
                <button class="btn btn-text btn-sm danger-text" @click="removeFailureAction(selectedGraphNode.parentIndex, selectedIndex)"><span class="material-symbols-outlined">delete</span>删除</button>
              </div>
            </template>

          </div>
        </aside>
</template>
