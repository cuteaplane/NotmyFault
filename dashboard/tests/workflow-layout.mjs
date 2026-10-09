import assert from 'node:assert/strict'
import { buildWorkflowGraph, predicateToGraph, connectConditionGraph } from '../src/lib/workflowGraph.js'
import { routeFlowEdge } from '../src/lib/flowGraph.js'
import { deriveDataEdges } from '../src/lib/bindings.js'

const presentation = { eventName: node => node.type, actionName: node => node.type, describeItem: () => '', predicateSummary: node => node.op }
const condition = predicateToGraph({ op: 'all', children: [{ op: 'is_true', left: true }, { op: 'not', children: [{ op: 'lte', left: 100, right: 80 }] }] })
condition.nodes.reverse()
const fork = { type: 'event_group', binding_id: 'a_fork0001', branches: [{ id: 'b_if', condition_graph: condition }, { id: 'b_elif', condition_graph: predicateToGraph({ op: 'is_true', left: false }) }, { id: 'b_else', else: true }] }
const child = { type: 'event_group', binding_id: 'a_child001', branches: [{ id: 'b_child_if', condition_graph: predicateToGraph({ op: 'is_true', left: true }) }, { id: 'b_child_else', else: true }] }
const log = id => ({ type: 'log', binding_id: id, params: {} })
const group = { type: 'action_group', binding_id: 'a_group001', workflow: { entry: child.binding_id, nodes: [child, log('a_nested01'), log('a_nested02')], edges: [{ source: child.binding_id, port: 'b_child_if', target: 'a_nested01' }, { source: child.binding_id, port: 'b_child_else', target: 'a_nested02' }] } }
const rule = { workflow: { entry: fork.binding_id, nodes: [fork, group, log('a_elif0001'), log('a_else0001')], edges: [{ source: fork.binding_id, port: 'b_if', target: group.binding_id }, { source: fork.binding_id, port: 'b_elif', target: 'a_elif0001' }, { source: fork.binding_id, port: 'b_else', target: 'a_else0001' }] } }
const snapshot = JSON.stringify(rule)
const routes = new Map([['fork', { kind: 'event_group', x: 0, y: 0, width: 400, outputs: [{ id: 'if', y: 80 }, { id: 'elif', y: 120 }, { id: 'else', y: 160 }] }], ['elif', { x: 528, y: 500, width: 228 }], ['else', { x: 528, y: 900, width: 228 }]])
const elifPath = routeFlowEdge({ from: 'fork', to: 'elif', sourcePort: 'elif', channel: 'control' }, routes)
const elsePath = routeFlowEdge({ from: 'fork', to: 'else', sourcePort: 'else', channel: 'control' }, routes)
assert(elifPath.d.includes(' V ') && elsePath.d.includes(' V '), '长分支线使用独立竖向通道')
assert(elsePath.labelX < elifPath.labelX, '下方分支在左侧绕行，不穿过上方分支的末段')
for (const expanded of [new Set(), new Set([fork.binding_id]), new Set([fork.binding_id, group.binding_id, child.binding_id])]) {
  const drawn = buildWorkflowGraph(rule, expanded, {}, presentation)
  const byId = new Map(drawn.nodes.map(node => [node.id, node]))
  assert.equal(JSON.stringify(rule), snapshot, '展开和收起不修改执行图')
  for (const edge of drawn.edges.filter(edge => edge.id.startsWith('condition:') || edge.id.startsWith('branch:') || edge.id.startsWith('dispatch:'))) {
    const from = byId.get(edge.from), to = byId.get(edge.to)
    assert(to.x + (edge.targetPort ? to.width : 0) > from.x + from.width, '条件及出口按依赖从左向右连接')
    assert(!routeFlowEdge(edge, byId).reversed, '组内部连线不绕出组右侧')
    if (edge.targetPort) {
      const y1 = from.y + ((from.outputs || from.outputAnchors)?.find(port => port.id === edge.sourcePort)?.y ?? 58)
      const y2 = to.y + to.outputs.find(port => port.id === edge.targetPort).y
      assert(y2 >= y1, '组出口按分支顺序排列')
      assert.equal(edge.label, '', '边界短线不重复显示分支名称')
    }
  }
  for (const frame of drawn.nodes.filter(node => node.frame)) {
    const children = drawn.nodes.filter(node => node.groupOwner === frame.source || node.ancestry?.some(parent => parent.id === frame.id))
    for (const child of children) {
      assert(child.x > frame.x && child.x + child.width < frame.x + frame.width, '子节点保留在展开组宽度内')
      assert(child.y > frame.y && child.y + child.fixedHeight < frame.y + frame.fixedHeight, '子节点保留在展开组高度内')
    }
    const content = drawn.nodes.filter(node => node.groupOwner === frame.source)
    for (let i = 0; i < content.length; i++) for (let j = i + 1; j < content.length; j++) {
      const a = content[i], b = content[j]
      assert(a.x + a.width <= b.x || b.x + b.width <= a.x || a.y + a.fixedHeight <= b.y || b.y + b.fixedHeight <= a.y, '组内条件卡片不重叠')
    }
  }
  for (const edge of drawn.edges.filter(edge => edge.rawEdge?.source === fork.binding_id)) assert.equal(edge.label, '', '外部连线使用端口名称，不堆叠第二份分支标签')
  if (expanded.has(fork.binding_id) && expanded.has(group.binding_id)) {
    const source = byId.get(`action-${fork.binding_id}`), target = byId.get(`action-${group.binding_id}`)
    assert(Math.abs(source.y + source.outputs[0].y - target.y - target.inputY) <= 40, '首分支动作组与 IF 出口高度相近')
  }
}

const binding = { type: 'action_group', binding_id: 'a_export01' }
const emptyGroup = { type: 'action_group', binding_id: 'a_empty001', workflow: { entry: null, nodes: [], edges: [] } }
const emptyRule = { workflow: { entry: emptyGroup.binding_id, nodes: [emptyGroup], edges: [{ source: emptyGroup.binding_id, port: 'next', target: null }] } }
const emptyDrawn = buildWorkflowGraph(emptyRule, new Set([emptyGroup.binding_id]), {}, presentation)
assert(emptyDrawn.nodes.some(node => node.id === `completion-${emptyGroup.binding_id}`), '空动作组显示返回父流程的节点')
assert(emptyDrawn.edges.some(edge => edge.to === `completion-${emptyGroup.binding_id}`), '空动作组显示完成路径')
assert.equal(emptyRule.workflow.nodes.length, 1, '返回节点只影响显示，不写入执行图')
const movable = { root: 'root', nodes: [{ id: 'root', op: 'all' }, { id: 'nested', op: 'any' }, { id: 'one', predicate: { op: 'is_true', left: true } }, { id: 'two', predicate: { op: 'is_true', left: false } }], edges: [{ source: 'one', target: 'root', order: 0 }, { source: 'nested', target: 'root', order: 1 }, { source: 'two', target: 'nested', order: 0 }] }
assert.equal(connectConditionGraph(movable, 'one', 'nested'), '', '移动条件到嵌套组')
assert.deepEqual(movable.edges.filter(edge => edge.target === 'nested').map(edge => edge.source), ['two', 'one'])
const preserved = JSON.stringify(movable)
assert(connectConditionGraph(movable, 'nested', 'root') === '', '已有连接不改变条件图')
assert(connectConditionGraph(movable, 'root', 'nested'), '拒绝条件循环')
assert(connectConditionGraph(movable, 'two', 'one'), '拒绝把条件连接到叶节点')
assert.equal(JSON.stringify(movable), preserved, '拒绝连接时保留原条件图')
const onlyChild = { root: 'root', nodes: [{ id: 'root', op: 'all' }, { id: 'not', op: 'not' }, { id: 'one', predicate: { op: 'is_true', left: true } }, { id: 'two', predicate: { op: 'is_true', left: true } }], edges: [{ source: 'not', target: 'root', order: 0 }, { source: 'one', target: 'not', order: 0 }, { source: 'two', target: 'root', order: 1 }] }
const intact = JSON.stringify(onlyChild)
assert(connectConditionGraph(onlyChild, 'two', 'not'), 'NOT 拒绝第二个输入')
assert(connectConditionGraph(onlyChild, 'one', 'root'), '移动不得留下空条件组')
assert.equal(JSON.stringify(onlyChild), intact)
const output = { id: 'action-a_export01', source: binding, kind: 'action_group', dataOutputs: [{ name: 'speed', index: 0, type: { type: 'number' }, required: true }] }
const decorative = { id: 'dispatch-a_export01', source: binding, kind: 'activation', dataOutputs: [] }
const consumer = { id: 'action-a_consume1', kind: 'action', source: { binding_id: 'a_consume1', params: { speed: { $ref: { scope: 'step', node: binding.binding_id, path: ['speed'] } } } }, availableStepIds: [binding.binding_id], dataInputs: [{ name: 'speed', index: 0, label: '速度', type: { type: 'number' } }] }
assert.equal(deriveDataEdges({}, [output, decorative, consumer])[0]?.from, output.id, '同身份的展示节点不覆盖组导出数据来源')

const compact = buildWorkflowGraph(rule, new Set([fork.binding_id, group.binding_id, child.binding_id]), {}, presentation)
assert(compact.nodes.filter(node => ['predicate', 'condition-expression'].includes(node.kind)).every(node => node.fixedHeight === 116), '无数据行的条件卡片使用正文高度')
assert(compact.nodes.filter(node => node.id.startsWith('completion-')).every(node => node.fixedHeight === 116), '返回节点不预留空白操作区')
const measured = buildWorkflowGraph(rule, new Set([fork.binding_id, group.binding_id, child.binding_id]), {}, { ...presentation, nodeHeight: node => node.kind === 'predicate' ? 218 : node.id === 'action-a_nested01' ? 194 : undefined })
for (const frame of measured.nodes.filter(node => node.frame)) {
  const children = measured.nodes.filter(node => node.groupOwner === frame.source || node.ancestry?.some(parent => parent.id === frame.id))
  const bottom = Math.max(...children.map(node => node.y + node.fixedHeight))
  assert.equal(frame.y + frame.fixedHeight - bottom, 28, '组边界按实际卡片高度保留 28px 底部间距')
}
assert.equal(measured.nodes.find(node => node.id === 'action-a_nested01').fixedHeight, 194, '展开的数据行参与动作组布局')
console.log('Workflow layout: forward conditions, aligned outlets, containment, readable labels and export wires: PASS')

const boundarySource = { id: 'event-leaf', x: 30, y: 80, width: 228, fixedHeight: 116, groupOwner: fork }
const boundaryObstacle = { id: 'and-root', x: 358, y: 80, width: 228, fixedHeight: 116, groupOwner: fork }
const boundaryFrame = { id: 'group-frame', x: 0, y: 0, width: 690, fixedHeight: 224, source: fork, frame: true, outputs: [{ id: 'b_if', y: 138 }] }
const boundaryRoute = routeFlowEdge({ from: boundarySource.id, to: boundaryFrame.id, targetPort: 'b_if', channel: 'control' }, new Map([boundarySource, boundaryObstacle, boundaryFrame].map(node => [node.id, node])))
assert(boundaryRoute.d.includes(' V ') && boundaryRoute.d.includes(' Q '), '跨列出口连线从卡片下方绕行并保留圆角')
assert(!boundaryRoute.reversed, '出口连线保持向右')
