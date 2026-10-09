import { computed, ref } from 'vue'
import { store } from '../lib/store'
import { buildDefaultParams, isConditionLeaf } from '../lib/utils'
import { createBindingId, regenerateBindingIds } from '../lib/bindings'
import { conditionNodeId } from '../lib/flowGraph'
import { findWorkflowAction, ifBranches } from '../lib/workflowConditions'

export function useRuleMutations(rule, selectedNodeId, selectedGraphNode) {
  const selectedKind = computed(() => selectedGraphNode.value?.kind || 'trigger')
  const picker = ref({ open: false, kind: 'action', mode: 'append', index: null, path: [], op: 'any' })
  const selectedConditionNode = computed(() => (
    ['condition', 'trigger'].includes(selectedKind.value) && selectedGraphNode.value?.path
      ? selectedGraphNode.value.source
      : null
  ))
  const selectedConditionPath = computed(() => selectedGraphNode.value?.path || [])
  const selectedConditionParent = computed(() => {
    const path = selectedConditionPath.value
    if (!path.length || !rule.value.condition) return null
    let parent = rule.value.condition
    for (const index of path.slice(0, -1)) parent = parent?.children?.[index]
    return parent
  })

  function triggerParams(type) {
    const params = buildDefaultParams(store.schema.triggers[type])
    if (type === 'rule_finished' && rule.value?.rule_id) {
      params.owner_rule_id = rule.value.rule_id
    }
    return params
  }
  function setRootTrigger(type) {
    if (!type) return
    const bindingId = isConditionLeaf(rule.value.condition) ? rule.value.condition.binding_id : null
    rule.value.condition = {
      binding_id: bindingId || createBindingId('trigger'),
      type,
      params: triggerParams(type),
    }
    selectedNodeId.value = conditionId([])
  }
  function useSingleCondition() {
    function firstLeaf(node) {
      if (isConditionLeaf(node)) return node
      for (const child of node?.children || []) {
        const leaf = firstLeaf(child)
        if (leaf) return leaf
      }
      return null
    }
    const first = firstLeaf(rule.value.condition)
    if (first) rule.value.condition = first
    else delete rule.value.condition
    selectedNodeId.value = conditionId([])
  }
  function wrapConditionInNot(path = selectedConditionPath.value) {
    const node = conditionAtPath(path)
    if (!isConditionLeaf(node)) return
    const wrapped = { op: 'not', within_seconds: 60, children: [node] }
    if (path.length) {
      const parent = conditionAtPath(path.slice(0, -1))
      if (parent?.op === 'not') return
      parent.children[path.at(-1)] = wrapped
    } else rule.value.condition = wrapped
    selectedNodeId.value = conditionId(path)
  }
  function unwrapNotEvent() {
    const path = selectedConditionPath.value
    const node = selectedConditionNode.value
    if (node?.op !== 'not' || node.children?.length !== 1) return
    const parent = selectedConditionParent.value
    if (parent) parent.children[path.at(-1)] = node.children[0]
    else rule.value.condition = node.children[0]
    selectedNodeId.value = conditionId(path)
  }
  function conditionId(path) {
    return conditionNodeId(conditionAtPath(path), path)
  }
  function changeConditionOp() {
    const node = selectedConditionNode.value
    if (!node || selectedKind.value !== 'condition') return
    if (node.op !== 'all') {
      delete node.consume
      delete node.ttl_seconds
    }
    if (node.op !== 'any') delete node.pick
    if (node.op !== 'not') delete node.repeat
    if (node.op === 'all') node.consume ??= 'matched'
    if (node.op === 'any') node.pick ??= 'latest'
    if (node.op === 'not') node.repeat ??= 'once_until_seen'
    if (node.op === 'not') node.within_seconds ||= 60
    else if (node.op !== 'all') delete node.within_seconds
  }
  function removeSelectedCondition() {
    const parent = selectedConditionParent.value
    const path = selectedConditionPath.value
    if (!parent || !path.length) return
    parent.children.splice(path[path.length - 1], 1)
    selectedNodeId.value = conditionId(path.slice(0, -1))
  }
  function moveSelectedCondition(offset) {
    const parent = selectedConditionParent.value
    const path = selectedConditionPath.value
    if (!parent || !path.length) return
    const index = path[path.length - 1]
    const target = index + offset
    if (target < 0 || target >= parent.children.length) return
    const [node] = parent.children.splice(index, 1)
    parent.children.splice(target, 0, node)
    selectedNodeId.value = conditionId([...path.slice(0, -1), target])
  }
  function duplicateSelectedCondition() {
    const parent = selectedConditionParent.value
    const path = selectedConditionPath.value
    if (!parent || !path.length) return
    const index = path[path.length - 1]
    const copy = JSON.parse(JSON.stringify(parent.children[index]))
    regenerateBindingIds(copy, 'trigger')
    parent.children.splice(index + 1, 0, copy)
    selectedNodeId.value = conditionId([...path.slice(0, -1), index + 1])
  }
  function addAssignment() {
    const action = { binding_id: createBindingId('action'), type: 'set_variable', variable: rule.value.variables?.[0]?.id || '', value: '' }
    ;(rule.value.actions ||= []).push(action)
    selectedNodeId.value = `action-${action.binding_id}`
  }
  function addIf() {
    const action = { binding_id: createBindingId('action'), type: 'if', condition: { op: 'is_true', left: false }, then: [], else: [] }
    if (!Array.isArray(rule.value.actions)) rule.value.actions = []
    rule.value.actions.push(action)
    selectedNodeId.value = `action-${action.binding_id}`
  }
  function addWait() {
    addAction('wait')
  }
  function addAction(type, insertIndex = null) {
    if (!type) return
    if (!Array.isArray(rule.value.actions)) rule.value.actions = []
    const action = {
      binding_id: createBindingId('action'),
      type,
      params: buildDefaultParams(store.schema.actions[type]),
    }
    const index = insertIndex == null
      ? rule.value.actions.length
      : Math.max(0, Math.min(rule.value.actions.length, insertIndex))
    rule.value.actions.splice(index, 0, action)
    selectedNodeId.value = `action-${action.binding_id}`
  }
  function changeAction(action, type) {
    action.type = type
    action.params = buildDefaultParams(store.schema.actions[type])
    if (store.schema.actions[type]?.cancellation_api !== 'runtime-v1') delete action.timeout_seconds
  }
  function actionItems(node = selectedGraphNode.value) {
    return node?.kind === 'action' ? node.items || rule.value.actions : rule.value.actions
  }
  function failureOwner(actionIndex, node = selectedGraphNode.value) {
    return node?.kind === 'failure-action' ? node.owner : node?.kind === 'action' ? node.source : rule.value.actions?.[actionIndex]
  }
  function branchItems(ownerId, key) {
    const owner = findWorkflowAction(rule.value, ownerId)?.action
    if (owner?.type !== 'if') return null
    const branch = ifBranches(owner).find(branch => branch.key === key)
    if (!branch) return null
    return branch.branch ? (branch.branch.then ||= []) : (owner[key] ||= [])
  }
  function createAction(type) {
    return { binding_id: createBindingId('action'), type,
      ...(type === 'if' ? { condition: { op: 'is_true', left: false }, then: [], else: [] }
        : type === 'set_variable' ? { variable: rule.value.variables?.[0]?.id || '', value: '' }
          : { params: buildDefaultParams(store.schema.actions[type]) }),
    }
  }
  function addBranchAction(node, type, index = null) {
    const items = branchItems(node.source.binding_id, node.branchKey)
    if (!items) return
    const action = createAction(type)
    items.splice(index == null ? items.length : index, 0, action)
    selectedNodeId.value = `action-${action.binding_id}`
  }
  function requestBranchAction(node) {
    openPluginPicker('action', { mode: 'branch-action', ownerId: node.source.binding_id, branchKey: node.branchKey, title: '添加分支动作' })
  }
  function removeAction(index, node = selectedGraphNode.value) {
    const items = actionItems(node)
    items.splice(index, 1)
    selectedNodeId.value = items.length ? `action-${items[Math.min(index, items.length - 1)].binding_id}`
      : node?.parentIf ? `branch-add-${node.parentIf.binding_id}-${node.branchKey}` : null
  }
  function duplicateAction(index, node = selectedGraphNode.value) {
    const items = actionItems(node)
    const source = items?.[index]
    if (!source) return
    const copy = JSON.parse(JSON.stringify(source))
    regenerateBindingIds(copy, 'action')
    items.splice(index + 1, 0, copy)
    selectedNodeId.value = `action-${copy.binding_id}`
  }
  function moveAction(index, offset, node = selectedGraphNode.value) {
    const items = actionItems(node)
    const target = index + offset
    if (target < 0 || target >= items.length) return
    const [action] = items.splice(index, 1)
    items.splice(target, 0, action)
    selectedNodeId.value = `action-${action.binding_id}`
  }
  function addFailureAction(actionIndex, type, owner = failureOwner(actionIndex)) {
    if (!owner || !type) return
    const action = createAction(type)
    ;(owner.failure_actions ||= []).push(action)
    selectedNodeId.value = `failure-action-${action.binding_id}`
  }
  function removeFailureAction(actionIndex, failureIndex, node = selectedGraphNode.value) {
    const owner = failureOwner(actionIndex, node)
    if (!owner?.failure_actions) return
    owner.failure_actions.splice(failureIndex, 1)
    if (!owner.failure_actions.length) delete owner.failure_actions
    selectedNodeId.value = `action-${owner.binding_id}`
  }
  function duplicateFailureAction(actionIndex, failureIndex) {
    const owner = failureOwner(actionIndex)
    const source = owner?.failure_actions?.[failureIndex]
    if (!source) return
    const copy = JSON.parse(JSON.stringify(source))
    regenerateBindingIds(copy, 'action')
    owner.failure_actions.splice(failureIndex + 1, 0, copy)
    selectedNodeId.value = `failure-action-${copy.binding_id}`
  }
  function moveFailureAction(actionIndex, failureIndex, offset, node = selectedGraphNode.value) {
    const items = failureOwner(actionIndex, node)?.failure_actions
    const target = failureIndex + offset
    if (!items || target < 0 || target >= items.length) return
    const [action] = items.splice(failureIndex, 1)
    items.splice(target, 0, action)
    selectedNodeId.value = `failure-action-${action.binding_id}`
  }
  function requestAddFailureAction(actionIndex) {
    openPluginPicker('action', { mode: 'failure-action', ownerId: failureOwner(actionIndex)?.binding_id, title: '添加补救动作' })
  }
  function requestReplaceFailureAction(actionIndex, failureIndex) {
    openPluginPicker('action', { mode: 'replace-failure-action', ownerId: failureOwner(actionIndex)?.binding_id, failureIndex, title: '更换补救动作' })
  }
  function openPluginPicker(kind, options = {}) {
    picker.value = {
      open: true,
      kind,
      mode: options.mode || kind,
      index: options.index ?? null,
      ownerId: options.ownerId ?? null,
      actionId: options.actionId ?? null,
      branchKey: options.branchKey ?? null,
      parentIndex: options.parentIndex ?? null,
      failureIndex: options.failureIndex ?? null,
      path: options.path || [],
      op: options.op || 'any',
      title: options.title || '',
    }
  }
  function closePluginPicker() { picker.value = { ...picker.value, open: false } }
  function conditionAtPath(path) {
    let node = rule.value.condition
    for (const index of path || []) node = node?.children?.[index]
    return node
  }
  function addConditionFromPicker(type, { path, group = false } = {}) {
    const parent = conditionAtPath(path)
    if (!parent || !Array.isArray(parent.children)) return
    const event = {
      binding_id: createBindingId('trigger'),
      type,
      params: triggerParams(type),
    }
    parent.children.push(group ? { op: 'any', children: [event] } : event)
    const eventPath = group
      ? [...path, parent.children.length - 1, 0]
      : [...path, parent.children.length - 1]
    selectedNodeId.value = conditionId(eventPath)
  }
  function appendRootCondition(type, op) {
    const added = {
      binding_id: createBindingId('trigger'),
      type,
      params: triggerParams(type),
    }
    rule.value.condition = { op, children: [...(rule.value.condition ? [rule.value.condition] : []), added] }
    selectedNodeId.value = conditionId([rule.value.condition.children.length - 1])
  }
  function choosePlugin(key) {
    const context = { ...picker.value }
    closePluginPicker()
    if (context.mode === 'set-trigger' || context.mode === 'replace-trigger') setRootTrigger(key)
    else if (context.mode === 'replace-condition-trigger') {
      const node = conditionAtPath(context.path)
      if (node?.type) {
        node.type = key
        node.params = triggerParams(key)
      }
    }
    else if (context.mode === 'branch-action' || context.mode === 'action' && context.ownerId) {
      const owner = findWorkflowAction(rule.value, context.ownerId)?.action
      if (owner) addBranchAction({ source: owner, branchKey: context.branchKey }, key, context.index)
    }
    else if (context.mode === 'action') addAction(key, context.index)
    else if (context.mode === 'replace-action') {
      const action = context.actionId ? findWorkflowAction(rule.value, context.actionId)?.action : rule.value.actions?.[context.index]
      if (action) changeAction(action, key)
    }
    else if (context.mode === 'failure-action') addFailureAction(context.parentIndex, key, findWorkflowAction(rule.value, context.ownerId)?.action)
    else if (context.mode === 'replace-failure-action') {
      const action = findWorkflowAction(rule.value, context.ownerId)?.action.failure_actions?.[context.failureIndex]
      if (action) changeAction(action, key)
    }
    else if (context.mode === 'condition-child') addConditionFromPicker(key, { path: context.path })
    else if (context.mode === 'condition-group') addConditionFromPicker(key, { path: context.path, group: true })
    else if (context.mode === 'combine-root') appendRootCondition(key, context.op)
  }
  function requestConditionChild(node, group = false) {
    openPluginPicker('trigger', {
      mode: group ? 'condition-group' : 'condition-child',
      path: node.path || [],
      title: group ? '添加条件组的第一个条件' : '添加条件',
    })
  }
  return { addBranchAction, requestBranchAction, duplicateFailureAction, setRootTrigger, useSingleCondition, wrapConditionInNot, unwrapNotEvent, conditionId, changeConditionOp, removeSelectedCondition, moveSelectedCondition, duplicateSelectedCondition, addAssignment, addIf, addWait, addAction, changeAction, removeAction, duplicateAction, moveAction, addFailureAction, removeFailureAction, moveFailureAction, requestAddFailureAction, requestReplaceFailureAction, openPluginPicker, closePluginPicker, conditionAtPath, addConditionFromPicker, appendRootCondition, choosePlugin, requestConditionChild, selectedConditionNode, selectedConditionPath, selectedConditionParent, picker }
}
