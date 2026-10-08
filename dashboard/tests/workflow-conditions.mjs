import assert from 'node:assert/strict'
import { buildFlowGraph, buildIfSubflowGraph } from '../src/lib/flowGraph.js'
import { ensureRuleBindingIds, regenerateBindingIds } from '../src/lib/bindings.js'
import { ifBranches, precedingSources, predicateSummary, findWorkflowAction } from '../src/lib/workflowConditions.js'
import { hasHighImpactActions, ruleNodeCount } from '../src/lib/naturalDraftRules.js'

const reference = (scope, node, path) => ({ $ref: { scope, node, path } })
const schema = { triggers: { process_state: { name: 'Steam 进程', outputs: [{ name: 'state', type: 'string' }] } }, actions: {
  speed: { name: '测速', outputs: [{ name: 'latency_us', type: 'number' }, { name: 'connected', type: 'bool' }] },
  pause: { name: '暂停下载', outputs: [{ name: 'result', type: 'string' }], permissions: ['admin'] },
} }
const inner = { binding_id: 'a_inner01', type: 'if', condition: { op: 'exists', left: reference('step', 'a_pause01', ['result']) }, then: [], elif: [], else: [] }
const conditional = { binding_id: 'a_outer01', type: 'if', condition: { op: 'any', children: [
  { op: 'all', children: [
    { op: 'eq', left: reference('trigger', 't_steam01', ['state']), right: 'running' },
    { op: 'run_status', rule_id: 'r_game001', status: 'running', comparison: 'eq' },
    { op: 'not', children: [{ op: 'lte', left: reference('step', 'a_speed01', ['latency_us']), right: reference('variable', 'v_limit01', []) }] },
  ] },
  { op: 'eq', left: reference('step', 'a_speed01', ['connected']), right: false },
] }, then: [], elif: [{ condition: { op: 'is_true', left: true }, then: [{ binding_id: 'a_pause01', type: 'pause', params: {} }, inner] }], else: [] }
const rule = { rule_id: 'r_sample01', name: '启动游戏', variables: [{ id: 'v_limit01', name: '可接受延迟', value_type: 'number', initial: 150 }], condition: { type: 'process_state', binding_id: 't_steam01', params: {} }, actions: [{ binding_id: 'a_speed01', type: 'speed', params: {} }, conditional, { binding_id: 'a_after01', type: 'pause', params: {} }] }
ensureRuleBindingIds(rule)
assert.equal(ifBranches(conditional).map(branch => branch.key).join(','), 'then,elif[0],else')
const innerSources = precedingSources(rule, inner, schema)
assert(innerSources.some(source => source.value.$ref.node === 'a_pause01'))
assert(innerSources.some(source => source.value.$ref.node === 'a_speed01'))
assert(!innerSources.some(source => source.value.$ref.node === 'a_after01'))
const afterSources = precedingSources(rule, rule.actions[2], schema)
assert(!afterSources.some(source => source.value.$ref.node === 'a_pause01'))
assert.equal(findWorkflowAction(rule, 'a_inner01').trail[0].field, 'elif[0]')
assert(hasHighImpactActions({ actions: [conditional] }, schema).length === 1)
assert.equal(ruleNodeCount({ condition: rule.condition, actions: [conditional] }), '4 个节点')
const copy = regenerateBindingIds(JSON.parse(JSON.stringify(conditional)), 'action')
assert.notEqual(copy.elif[0].then[0].binding_id, 'a_pause01')
assert.equal(copy.elif[0].then[1].condition.left.$ref.node, copy.elif[0].then[0].binding_id)
assert.equal(copy.condition.children[1].left.$ref.node, 'a_speed01')
const sources = precedingSources(rule, conditional, schema)
const graph = buildIfSubflowGraph({ rule, action: conditional, schema, sources, rules: [{ rule_id: 'r_game001', name: '游戏下载' }], actionName: action => schema.actions[action.type]?.name || action.type, describeItem: () => '' })
assert.equal(graph.nodes.filter(node => node.kind === 'predicate').length, 8)
assert(graph.nodes.some(node => node.source === conditional.condition && node.kind === 'predicate'))
assert(graph.nodes.some(node => node.source === inner && node.kind === 'action'))
assert(graph.edges.some(edge => edge.label === '取反'))
assert(graph.edges.some(edge => edge.channel === 'data' && edge.targetPortName === 'left'))
assert.equal(graph.nodes.filter(node => node.kind === 'if-merge').length, 1)
assert.equal(graph.nodes.filter(node => node.addBranch).length, 3)
assert(graph.nodes.filter(node => node.kind === 'predicate').every(node => node.availableStepIds.includes('a_speed01')))
const nodesById = new Map(graph.nodes.map(node => [node.id, node]))
assert.equal(nodesById.size, graph.nodes.length)
assert(graph.edges.every(edge => nodesById.has(edge.from) && nodesById.has(edge.to)))
const summary = predicateSummary(conditional.condition, sources, [{ rule_id: 'r_game001', name: '游戏下载' }])
assert(summary.includes('游戏下载') && summary.includes('Steam 进程') && summary.includes('可接受延迟') && summary.includes('NOT'))
const main = buildFlowGraph({ rule, schema, rules: [{ rule_id: 'r_game001', name: '游戏下载' }], condition: rule.condition, eventName: () => 'Steam', actionName: action => action.type, describeItem: () => '', isAdmin: () => false })
assert(main.nodes.some(node => node.source === conditional && node.meta.includes('3 个出口')))
assert(!main.nodes.some(node => node.source === inner))
assert(main.nodes.find(node => node.source === conditional).meta.includes('游戏下载'))
assert(main.nodes.find(node => node.source === conditional).meta.includes('可接受延迟'))
console.log('Workflow condition groups, ELIF references, path sources, summary and subflow graph: PASS')

const { workflowEventChoices } = await import('../src/lib/workflowConditions.js')
const { buildTestInputFields, buildPreparedTestContext } = await import('../src/lib/bindings.js')
const triggerSchema = { triggers: { hotkey: { name: '全局热键', outputs: [] } }, actions: { pause: { params: [], outputs: [] } } }
const fired = { op: 'event_occurred', trigger_id: 't_hotkey01', expected: false }
const firedAction = { type: 'if', binding_id: 'a_branch01', condition: fired, then: [{ type: 'pause', binding_id: 'a_pause01', params: {} }], else: [] }
const firedRule = { condition: { type: 'hotkey', binding_id: 't_hotkey01', params: {} }, actions: [firedAction] }
const choices = workflowEventChoices(firedRule, triggerSchema)
assert.equal(choices[0].label, '触发 1 · 全局热键')
assert(predicateSummary(fired, [], [], choices).includes('未发生'))
const firedGraph = buildIfSubflowGraph({ rule: firedRule, action: firedAction, schema: triggerSchema, sources: [], rules: [], actionName: action => action.type, describeItem: () => '' })
assert(firedGraph.nodes.some(node => node.kind === 'trigger' && node.source === firedRule.condition))
assert.deepEqual(firedGraph.nodes.find(node => node.kind === 'predicate').dataInputs, [])
assert(firedGraph.edges.some(edge => edge.label === '事件发生记录'))
const firedFields = buildTestInputFields(firedRule, triggerSchema)
assert.equal(firedFields.length, 1)
assert.equal(firedFields[0].scope, 'event_occurrence')
assert.deepEqual(JSON.parse(JSON.stringify(buildPreparedTestContext(firedFields, { [firedFields[0].key]: true }).trigger_payloads)), { t_hotkey01: {} })
assert.deepEqual(JSON.parse(JSON.stringify(buildPreparedTestContext(firedFields, { [firedFields[0].key]: false }).trigger_payloads)), {})
triggerSchema.triggers.hotkey.outputs = [{ name: 'count', type: 'number' }]
firedAction.condition = { op: 'all', children: [
  { op: 'eq', left: { $ref: { scope: 'trigger', node: 't_hotkey01', path: ['count'], on_missing: 'default', default: 0 } }, right: 0 }, fired,
] }
const suppliedFields = buildTestInputFields(firedRule, triggerSchema)
const countField = suppliedFields.find(field => field.scope === 'trigger')
assert.equal(countField.required, true)
assert.equal(countField.defaultValue, 0)
const suppliedValues = Object.fromEntries(suppliedFields.map(field => [field.key, field.defaultValue]))
assert.equal(buildPreparedTestContext(suppliedFields, suppliedValues).trigger_payloads.t_hotkey01.count, 0)
console.log('无输出字段的事件判断、条件图和手动测试事件记录：通过')
