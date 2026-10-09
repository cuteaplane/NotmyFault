import assert from 'node:assert/strict'
import { normalizeWorkflow, createGraphAction, insertGraphNode, connectGraph, copyGraphNode, predicateToGraph, graphPredicate, getGraphTriggers, buildWorkflowGraph, removeGraphNode, graphExecutionActions, connectTriggerCondition } from '../src/lib/workflowGraph.js'
import { collectTriggerLeaves, buildTestInputFields, graphBindingSources, actionOutputDefs } from '../src/lib/bindings.js'
const schema = { triggers: { process: { name: '进程事件', outputs: [{ name: 'state', value_type: 'text', required: true }] } }, actions: { measure: { outputs: [{ name: 'latency', value_type: 'number', required: true }] }, log: { params: [{ name: 'text', value_type: 'text', type: 'string' }] } }, data_types: { custom: {} } }
const predicate = { op: 'all', children: [{ op: 'event_occurred', trigger_id: 't_process01', expected: true }, { op: 'not', children: [{ op: 'lte', left: { $ref: { scope: 'step', node: 'a_measure01', path: ['latency'] } }, right: 80000 }] }] }
const rule = { rule_id: 'r_groups001', condition: { type: 'process', binding_id: 't_process01', params: {} }, actions: [{ type: 'measure', binding_id: 'a_measure01', params: {} }, { type: 'if', binding_id: 'a_if000001', condition: predicate, then: [{ type: 'log', binding_id: 'a_log00001', params: {} }], else: [] }, { type: 'log', binding_id: 'a_after001', params: {} }] }
const graph = normalizeWorkflow(rule)
assert(!Object.hasOwn(rule, 'condition') && !Object.hasOwn(rule, 'actions'))
assert.equal(normalizeWorkflow(rule), graph)
const entry = graph.nodes.find(node => node.binding_id === graph.entry), branch = graph.nodes.find(node => node.binding_id === 'a_if000001')
assert.equal(entry.activation.binding_id, 't_process01')
assert.deepEqual(graphPredicate(branch.branches[0].condition_graph), predicate)
assert.equal(graph.edges.find(edge => edge.source === 'a_log00001').target, 'a_after001')
assert.equal(graph.edges.find(edge => edge.source === branch.binding_id && edge.port === branch.branches[1].id).target, 'a_after001')
const group = createGraphAction('action_group')
insertGraphNode(graph, group, 'a_measure01')
const nested = createGraphAction('event_group')
nested.triggers.push({ type: 'process', binding_id: 't_middle001', params: {} })
insertGraphNode(group.workflow, nested)
assert.equal(graph.edges.find(edge => edge.source === group.binding_id).target, branch.binding_id)
assert.equal(collectTriggerLeaves(rule).length, 2)
assert.equal(getGraphTriggers(rule).length, 2)
const nestedAction = createGraphAction('log', { text: { $ref: { scope: 'trigger', node: 't_middle001', path: ['state'] } } })
insertGraphNode(group.workflow, nestedAction, nested.binding_id, nested.branches[0].id)
assert(graphBindingSources(rule, nestedAction, schema).some(source => source.value.$ref.node === 'a_measure01'))
assert.equal(connectGraph(graph, 'a_after001', 'next', 'a_measure01'), false)
const beforePorts = graph.edges.filter(edge => edge.source === branch.binding_id).map(edge => [edge.port, edge.target])
const present = { eventName: node => node.type, actionName: node => node.type, describeItem: () => '', predicateSummary: node => node.op }
const expanded = new Set([entry.binding_id, group.binding_id, nested.binding_id, branch.binding_id])
const drawn = buildWorkflowGraph(rule, expanded, schema, present)
assert.equal(new Set(drawn.nodes.map(node => node.id)).size, drawn.nodes.length)
assert(drawn.nodes.some(node => node.source === nestedAction))
assert.deepEqual(graph.edges.filter(edge => edge.source === branch.binding_id).map(edge => [edge.port, edge.target]), beforePorts)
const copy = copyGraphNode(group)
assert.notEqual(copy.binding_id, group.binding_id)
assert.notEqual(copy.workflow.entry, group.workflow.entry)
assert.equal(copy.workflow.edges[0].source, copy.workflow.nodes[0].binding_id)
const copiedTrigger = copy.workflow.nodes.find(node => node.type === 'event_group').triggers[0]
assert.notEqual(copiedTrigger.binding_id, 't_middle001')
const copiedLog = copy.workflow.nodes.find(node => node.type === 'log')
assert.equal(copiedLog.params.text.$ref.node, copiedTrigger.binding_id)
const testRule = { rule_id: 'r_testgroup', workflow: { entry: 'a_upstream', nodes: [{ type: 'measure', binding_id: 'a_upstream', params: {} }, { type: 'log', binding_id: 'a_selected', params: { text: { $ref: { scope: 'step', node: 'a_upstream', path: ['latency'] } } } }], edges: [{ source: 'a_upstream', port: 'next', target: 'a_selected' }] } }
assert(buildTestInputFields(testRule, schema, { startStepId: 'a_selected' }).some(field => field.scope === 'step' && field.node === 'a_upstream'))
removeGraphNode(graph, group)
assert.equal(graph.edges.find(edge => edge.source === 'a_measure01').target, branch.binding_id)
assert.deepEqual(graphPredicate(predicateToGraph(predicate)), predicate)
console.log('Workflow graphs, stable outlets, nesting, copy references, insertion, cycles and partial test sources: PASS')

const groupTest = createGraphAction('action_group'); groupTest.workflow = testRule.workflow; groupTest.exports = { speed: { $ref: { scope: 'step', node: 'a_upstream', path: ['latency'] } }, token: { $ref: { scope: 'variable', node: 'v_secret01', path: [], on_missing: 'skip' } } }; const exportRule = { workflow: { entry: groupTest.binding_id, nodes: [groupTest], edges: [] }, variables: [{ id: 'v_secret01', name: '密码', sensitive: true, value_type: 'text' }] }
const outputs = actionOutputDefs(groupTest, schema, exportRule)
assert.equal(outputs.find(output => output.name === 'speed').value_type.type, 'number')
assert.equal(outputs.find(output => output.name === 'token').sensitive, true)
assert.equal(outputs.find(output => output.name === 'token').required, false)
assert.deepEqual(graphExecutionActions(exportRule, { startStepId: 'a_selected' }).map(node => node.binding_id), ['a_selected'])
assert(!drawn.nodes.some(node => node.id.startsWith('dispatch-')), '事件组不显示内部调度卡片')
assert(drawn.edges.some(edge => edge.from === 'trigger-t_process01' && edge.kind === 'activation'))

const { branchRemovalImpact, hasExternalRemovalReferences, removeWorkflowBranch } = await import('../src/lib/workflowInspectorOps.js')
function removalFixture() { return { entry: 'a_gate001', nodes: [{ binding_id: 'a_gate001', type: 'event_group', branches: [{ id: 'b_if001', condition_graph: predicateToGraph({ op: 'is_true', left: true }) }, { id: 'b_else1', else: true }] }, { binding_id: 'a_left001', type: 'log' }, { binding_id: 'a_right01', type: 'log' }, { binding_id: 'a_shared1', type: 'log' }], edges: [{ source: 'a_gate001', port: 'b_if001', target: 'a_left001' }, { source: 'a_gate001', port: 'b_else1', target: 'a_right01' }, { source: 'a_left001', port: 'next', target: 'a_shared1' }, { source: 'a_right01', port: 'next', target: 'a_shared1' }, { source: 'a_shared1', port: 'next', target: null }] } }
const migration = removalFixture(), migratingGroup = migration.nodes[0], migratingBranch = migratingGroup.branches[0]
assert.deepEqual(branchRemovalImpact(migration, migratingGroup, migratingBranch).nodes.map(node => node.binding_id), ['a_left001'])
assert.equal(removeWorkflowBranch(migration, migratingGroup, migratingBranch), false)
assert.equal(removeWorkflowBranch(migration, migratingGroup, migratingBranch, { destination: 'b_else1' }), true)
assert.equal(migration.edges.find(edge => edge.source === 'a_gate001').target, 'a_left001')
assert.equal(migration.edges.find(edge => edge.source === 'a_left001').target, 'a_right01')
assert.deepEqual(graphExecutionActions({ workflow: migration }).map(node => node.binding_id), ['a_gate001', 'a_left001', 'a_right01', 'a_shared1'])
const deleting = removalFixture(), deletingGroup = deleting.nodes[0], deletingBranch = deletingGroup.branches[0], deletedNodes = branchRemovalImpact(deleting, deletingGroup, deletingBranch).nodes
deleting.nodes.at(-1).params = { value: { $ref: { scope: 'step', node: 'a_left001', path: [] } } }
assert.equal(hasExternalRemovalReferences({ workflow: deleting }, deletedNodes, deletingBranch), true)
delete deleting.nodes.at(-1).params
assert.equal(hasExternalRemovalReferences({ workflow: deleting }, deletedNodes, deletingBranch), false)
assert.equal(removeWorkflowBranch(deleting, deletingGroup, deletingBranch, { deleteNodes: true }), true)
assert.deepEqual(deleting.nodes.map(node => node.binding_id), ['a_gate001', 'a_right01', 'a_shared1'])
assert.equal(deleting.edges.find(edge => edge.source === 'a_right01').target, 'a_shared1')
console.log('Branch removal preserves shared continuations, requires an explicit destination, and detects external references: PASS')

const { branchMigrationBlock, nodeRemovalImpact, nodeRemovalBlock, removeWorkflowNode, removalReferences } = await import('../src/lib/workflowInspectorOps.js')
const ending = removalFixture(), endingGroup = ending.nodes[0], endingBranch = endingGroup.branches[0]
ending.nodes.find(node => node.binding_id === 'a_left001').type = 'end'
ending.edges = ending.edges.filter(edge => edge.source !== 'a_left001')
assert(branchMigrationBlock(ending, endingGroup, endingBranch, 'b_else1').includes('结束节点'))
const endingSnapshot = JSON.stringify(ending)
assert.equal(removeWorkflowBranch(ending, endingGroup, endingBranch, { destination: 'b_else1' }), false)
assert.equal(JSON.stringify(ending), endingSnapshot)
endingGroup.branches.splice(1, 0, { id: 'b_empty01', condition_graph: predicateToGraph({ op: 'is_true', left: false }) }); ending.edges.push({ source: endingGroup.binding_id, port: 'b_empty01', target: null })
assert.equal(branchMigrationBlock(ending, endingGroup, endingBranch, 'b_empty01'), '')
assert.equal(removeWorkflowBranch(ending, endingGroup, endingBranch, { destination: 'b_empty01' }), true)
assert.equal(new Set(graphExecutionActions({ workflow: ending }).map(node => node.binding_id)).size, ending.nodes.length)
const whole = removalFixture(), wholeGroup = whole.nodes[0], wholeRule = { workflow: whole }
assert.equal(nodeRemovalImpact(whole, wholeGroup), null)
assert.equal(nodeRemovalImpact(whole, wholeGroup, ''), null)
const wholeImpact = nodeRemovalImpact(whole, wholeGroup, 'b_else1')
assert.deepEqual(wholeImpact.detached.map(node => node.binding_id), ['a_left001'])
assert.equal(removeWorkflowNode(wholeRule, whole, wholeGroup, { port: 'b_else1' }), true)
assert.equal(whole.entry, 'a_right01')
assert.deepEqual(whole.nodes.map(node => node.binding_id), ['a_right01', 'a_shared1'])
assert.deepEqual(graphExecutionActions(wholeRule).map(node => node.binding_id), ['a_right01', 'a_shared1'])
const exported = createGraphAction('action_group'), external = createGraphAction('log', { value: { $ref: { scope: 'step', node: exported.binding_id, path: ['result'] } } }), guardedRule = { workflow: { entry: exported.binding_id, nodes: [exported, external], edges: [{ source: exported.binding_id, port: 'next', target: external.binding_id }, { source: external.binding_id, port: 'next', target: null }] } }
const exportImpact = nodeRemovalImpact(guardedRule.workflow, exported, 'next')
assert(nodeRemovalBlock(guardedRule, guardedRule.workflow, exportImpact).includes('引用'))
assert.equal(removalReferences(guardedRule, [exported])[0].owner, external.binding_id)
const guardedSnapshot = JSON.stringify(guardedRule)
assert.equal(removeWorkflowNode(guardedRule, guardedRule.workflow, exported, { port: 'next' }), false)
assert.equal(JSON.stringify(guardedRule), guardedSnapshot)
const triggerRule = { workflow: { entry: 'a_entry01', nodes: [{ binding_id: 'a_entry01', type: 'event_group', activation: { type: 'process', binding_id: 't_entry01', params: {} }, branches: [{ id: 'b_start01', else: true }] }, { binding_id: 'a_next001', type: 'log' }], edges: [{ source: 'a_entry01', port: 'b_start01', target: 'a_next001' }, { source: 'a_next001', port: 'next', target: null }] } }
assert(nodeRemovalBlock(triggerRule, triggerRule.workflow, nodeRemovalImpact(triggerRule.workflow, triggerRule.workflow.nodes[0], 'b_start01')).includes('启动事件'))
console.log('Explicit end migration, selected group continuation, shared nodes, export references and startup event protection: PASS')

const { connectActivationNode } = await import('../src/lib/workflowInspectorOps.js')
const eventA = { type: 'process', binding_id: 't_movea01', params: {} }, eventB = { type: 'process', binding_id: 't_moveb01', params: {} }, eventC = { type: 'process', binding_id: 't_movec01', params: {} }, sourceEvents = { op: 'all', within_seconds: 15, ttl_seconds: 600, consume: 'none', children: [eventA, eventB] }, targetEvents = { op: 'any', pick: 'first', children: [eventC] }, activationGroup = { activation: { op: 'any', pick: 'latest', children: [sourceEvents, targetEvents] } }
assert.equal(connectActivationNode(activationGroup, eventA, targetEvents), '')
assert.deepEqual(sourceEvents.children, [eventB])
assert.equal(targetEvents.children.at(-1), eventA)
assert.equal(sourceEvents.consume, 'none'); assert.equal(sourceEvents.ttl_seconds, 600); assert.equal(targetEvents.pick, 'first')
function refusedMove(source, target) { const before = JSON.stringify(activationGroup); assert(connectActivationNode(activationGroup, source, target)); assert.equal(JSON.stringify(activationGroup), before) }
refusedMove(eventB, targetEvents)
refusedMove(targetEvents, targetEvents)
refusedMove(activationGroup.activation, targetEvents)
refusedMove(eventA, { op: 'all', children: [] })
refusedMove(eventA, eventC)
const notEvents = { op: 'not', within_seconds: 20, repeat: 'every_window', children: [] }; activationGroup.activation.children.push(notEvents)
assert.equal(connectActivationNode(activationGroup, eventA, notEvents), '')
assert.equal(notEvents.children[0], eventA); assert.equal(notEvents.repeat, 'every_window')
refusedMove(eventC, notEvents)
refusedMove(sourceEvents, notEvents)
console.log('Activation connections preserve original objects and matcher settings; invalid moves leave the tree unchanged: PASS')

const { removeEventSource, removeConditionNode } = await import('../src/lib/workflowInspectorOps.js')
const removableEvent = { type: 'process', binding_id: 't_remove01', params: {} }, keptEvent = { type: 'process', binding_id: 't_keep001', params: {} }
const removableGroup = { type: 'event_group', binding_id: 'a_remove01', activation: { op: 'all', children: [removableEvent, keptEvent] }, triggers: [], branches: [] }
const eventRule = { workflow: { nodes: [removableGroup], edges: [] } }
assert.deepEqual(removeEventSource(eventRule, removableGroup, removableEvent), [])
assert.equal(removableGroup.activation, keptEvent)
removableGroup.branches.push({ condition_graph: predicateToGraph({ op: 'event_occurred', trigger_id: keptEvent.binding_id }) })
const eventBefore = JSON.stringify(eventRule)
assert.equal(removeEventSource(eventRule, removableGroup, keptEvent).length, 1)
assert.equal(JSON.stringify(eventRule), eventBefore)
removableGroup.branches = []
assert.deepEqual(removeEventSource(eventRule, removableGroup, keptEvent), [])
assert(!removableGroup.activation)
const sharedCondition = { root: 'p_root01', nodes: [{ id: 'p_root01', op: 'any' }, { id: 'p_left01', op: 'all' }, { id: 'p_right1', op: 'not' }, { id: 'p_shared', predicate: { op: 'event_occurred', trigger_id: 't_keep001' } }], edges: [{ source: 'p_left01', target: 'p_root01', order: 0 }, { source: 'p_right1', target: 'p_root01', order: 1 }, { source: 'p_shared', target: 'p_left01', order: 0 }, { source: 'p_shared', target: 'p_right1', order: 0 }] }
removeConditionNode(sharedCondition, 'p_left01')
assert(sharedCondition.nodes.some(node => node.id === 'p_shared'))
assert.equal(sharedCondition.edges.length, 2)
removeConditionNode(sharedCondition, sharedCondition.root)
assert.deepEqual(sharedCondition, { root: null, nodes: [], edges: [] })
assert.equal(graphPredicate(predicateToGraph()).op, 'event_occurred')
console.log('Event source deletion checks references, preserves remaining events and shared conditions; empty graphs require a new condition: PASS')

const directGroup = { binding_id: 'a_direct001', type: 'event_group', mode: 'immediate', activation: { binding_id: 't_start001', type: 'process', params: {} }, triggers: [{ binding_id: 't_steam002', type: 'process', params: {} }], branches: [{ id: 'b_if', condition_graph: predicateToGraph({ op: 'all', children: [{ op: 'eq', left: { $ref: { scope: 'variable', node: 'v_limit001', path: [] } }, right: 80 }] }) }, { id: 'b_else', else: true }] }
const directGraph = directGroup.branches[0].condition_graph, activationBefore = JSON.stringify(directGroup.activation)
assert.equal(connectTriggerCondition(directGraph, 't_steam002', directGraph.root), '')
const savedDirect = JSON.stringify(directGraph)
assert.equal(connectTriggerCondition(directGraph, 't_steam002', directGraph.root), '')
assert.equal(JSON.stringify(directGraph), savedDirect, '重复连接事件不新增节点')
assert.equal(JSON.stringify(directGroup.activation), activationBefore, '分支事件连接不修改启动条件')
const directRule = { workflow: { entry: directGroup.binding_id, nodes: [directGroup], edges: [] } }
const directDrawing = buildWorkflowGraph(directRule, new Set([directGroup.binding_id]), schema, present)
const eventEdge = directDrawing.edges.find(edge => edge.from === 'trigger-t_steam002' && edge.kind === 'condition')
assert(eventEdge && eventEdge.to === `predicate-${directGroup.binding_id}-${directGraph.root}`, '事件直接连接 AND')
assert(directDrawing.inspectionNodes.some(node => node.id === eventEdge.inspectionId), '直接连线保留事件判断编辑入口')
assert(!directDrawing.nodes.some(node => node.source?.op === 'event_occurred'), '已选事件来源不重复显示判断卡片')
assert(directDrawing.nodes.filter(node => node.conditionGraph).every(node => node.kicker !== 'IF'), '内部节点按用途命名')
assert(directDrawing.edges.some(edge => edge.from === eventEdge.to && edge.targetPort === 'b_if'), '条件结果直接连接组出口')
const directNot = { root: 'p_not', nodes: [{ id: 'p_not', op: 'not' }], edges: [] }
assert.equal(connectTriggerCondition(directNot, 't_steam002', 'p_not'), '')
const notBefore = JSON.stringify(directNot)
assert(connectTriggerCondition(directNot, 't_start001', 'p_not'), 'NOT 拒绝第二个事件')
assert.equal(JSON.stringify(directNot), notBefore, '拒绝连接保留原图')
assert(connectTriggerCondition(directGraph, 't_start001', directGraph.nodes.find(node => node.predicate?.op === 'eq').id), '事件不能连接数据判断输入')
console.log('Direct event connections, editable event wires, branch outlets and unchanged activation: PASS')

const pendingEvents = predicateToGraph({ op: 'all', children: [{ op: 'event_occurred', trigger_id: '', expected: true }, { op: 'event_occurred', trigger_id: '', expected: true }] })
assert.equal(connectTriggerCondition(pendingEvents, 't_steam002', pendingEvents.root), '')
assert.equal(connectTriggerCondition(pendingEvents, 't_start001', pendingEvents.root), '')
assert.equal(pendingEvents.nodes.length, 3, '拖入事件填充未配置的事件输入')
assert(pendingEvents.nodes.filter(node => node.predicate).every(node => node.predicate.trigger_id), '填充后的组合没有空事件来源')
