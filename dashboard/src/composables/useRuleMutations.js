import { computed, ref } from 'vue'
import { store } from '../lib/store'
import { buildDefaultParams, isConditionLeaf } from '../lib/utils'
import { createBindingId, regenerateBindingIds } from '../lib/bindings'
import { conditionNodeId } from '../lib/flowGraph'

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
  function setRootTrigger(type) {
    if (!type) return
    const bindingId = isConditionLeaf(rule.value.condition) ? rule.value.condition.binding_id : null
    rule.value.condition = {
      binding_id: bindingId || createBindingId('trigger'),
      type,
      params: buildDefaultParams(store.schema.triggers[type]),
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
  function removeAction(index) {
    rule.value.actions.splice(index, 1)
    selectedNodeId.value = rule.value.actions.length
      ? `action-${rule.value.actions[Math.min(index, rule.value.actions.length - 1)].binding_id}`
      : null
  }
  function duplicateAction(index) {
    const source = rule.value.actions?.[index]
    if (!source) return
    const copy = JSON.parse(JSON.stringify(source))
    regenerateBindingIds(copy, 'action')
    rule.value.actions.splice(index + 1, 0, copy)
    selectedNodeId.value = `action-${copy.binding_id}`
  }
  function moveAction(index, offset) {
    const target = index + offset
    if (target < 0 || target >= rule.value.actions.length) return
    const [action] = rule.value.actions.splice(index, 1)
    rule.value.actions.splice(target, 0, action)
    selectedNodeId.value = `action-${action.binding_id}`
  }
  function addFailureAction(actionIndex, type) {
    const action = rule.value.actions?.[actionIndex]
    if (!action || !type) return
    if (!Array.isArray(action.failure_actions)) action.failure_actions = []
    const failureAction = {
      binding_id: createBindingId('action'),
      type,
      params: buildDefaultParams(store.schema.actions[type]),
    }
    action.failure_actions.push(failureAction)
    selectedNodeId.value = `failure-action-${failureAction.binding_id}`
  }
  function removeFailureAction(actionIndex, failureIndex) {
    const failureActions = rule.value.actions?.[actionIndex]?.failure_actions
    if (!Array.isArray(failureActions)) return
    failureActions.splice(failureIndex, 1)
    if (!failureActions.length) delete rule.value.actions[actionIndex].failure_actions
    selectedNodeId.value = `action-${rule.value.actions[actionIndex].binding_id}`
  }
  function moveFailureAction(actionIndex, failureIndex, offset) {
    const failureActions = rule.value.actions?.[actionIndex]?.failure_actions
    const target = failureIndex + offset
    if (!Array.isArray(failureActions) || target < 0 || target >= failureActions.length) return
    const [failureAction] = failureActions.splice(failureIndex, 1)
    failureActions.splice(target, 0, failureAction)
    selectedNodeId.value = `failure-action-${failureAction.binding_id}`
  }
  function requestAddFailureAction(actionIndex) {
    openPluginPicker('action', { mode: 'failure-action', parentIndex: actionIndex, title: '添加补救动作' })
  }
  function requestReplaceFailureAction(actionIndex, failureIndex) {
    openPluginPicker('action', { mode: 'replace-failure-action', parentIndex: actionIndex, failureIndex, title: '更换补救动作' })
  }
  function openPluginPicker(kind, options = {}) {
    picker.value = {
      open: true,
      kind,
      mode: options.mode || kind,
      index: options.index ?? null,
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
      params: buildDefaultParams(store.schema.triggers[type]),
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
      params: buildDefaultParams(store.schema.triggers[type]),
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
        node.params = buildDefaultParams(store.schema.triggers[key])
      }
    }
    else if (context.mode === 'action') addAction(key, context.index)
    else if (context.mode === 'replace-action') {
      const action = rule.value.actions?.[context.index]
      if (action) changeAction(action, key)
    }
    else if (context.mode === 'failure-action') addFailureAction(context.parentIndex, key)
    else if (context.mode === 'replace-failure-action') {
      const action = rule.value.actions?.[context.parentIndex]?.failure_actions?.[context.failureIndex]
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
  return { setRootTrigger, useSingleCondition, wrapConditionInNot, unwrapNotEvent, conditionId, changeConditionOp, removeSelectedCondition, moveSelectedCondition, duplicateSelectedCondition, addAssignment, addIf, addAction, changeAction, removeAction, duplicateAction, moveAction, addFailureAction, removeFailureAction, moveFailureAction, requestAddFailureAction, requestReplaceFailureAction, openPluginPicker, closePluginPicker, conditionAtPath, addConditionFromPicker, appendRootCondition, choosePlugin, requestConditionChild, selectedConditionNode, selectedConditionPath, selectedConditionParent, picker }
}
