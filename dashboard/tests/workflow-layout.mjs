import assert from 'node:assert/strict'
import { buildWorkflowGraph, arrangeWorkflow, makeGroupRoom } from '../src/lib/workflowGraph.js'
import { routeFlowEdge } from '../src/lib/flowGraph.js'
import { deriveDataEdges } from '../src/lib/bindings.js'
import { trigger, action, eventGroup, wire, ruleWith, reference, presentation } from './canvas-fixtures.mjs'
const outer = eventGroup('outer001'), nested = eventGroup('nested01', 'all', outer.binding_id), a = trigger('alpha001', {}, nested.binding_id), b = trigger('beta0001', {}, nested.binding_id), fallback = action('fallback1'), group = { type: 'action_group', binding_id: 'a_group001', exports: {} }, inner = action('inner0001', 'log', {}, group.binding_id), after = action('after0001')
const rule = ruleWith([outer, nested, a, b, group, inner, fallback, after], [wire(a, nested, 'condition', 'event'), wire(b, nested, 'condition', 'event'), wire(nested, outer, 'condition', 'result', outer.branches[0].id), wire(outer, group, 'control', outer.branches[0].id), wire(outer, fallback, 'control', outer.branches.at(-1).id), wire(group, inner, 'control', 'entry'), wire(group, after)])
const expanded = new Set([outer.binding_id, nested.binding_id, group.binding_id])
arrangeWorkflow(rule.workflow, expanded)
const snapshot = JSON.stringify(rule)
for (const open of [new Set(), new Set([outer.binding_id]), expanded]) {
  const drawing = buildWorkflowGraph(rule, open, {}, presentation), ids = new Map(drawing.nodes.map(node => [node.id, node]))
  assert.equal(new Set(drawing.nodes.map(node => node.id)).size, drawing.nodes.length)
  assert.equal(JSON.stringify(rule), snapshot)
  for (const edge of drawing.edges) {
    assert(ids.has(edge.from) && ids.has(edge.to), JSON.stringify(edge))
    const route = routeFlowEdge(edge, ids)
    assert(route && !route.d.includes('NaN'))
  }
  for (const frame of drawing.nodes.filter(node => node.frame)) {
    const contents = drawing.nodes.filter(node => !node.gate && node.raw?.parent === frame.raw.binding_id || node.gate && node.raw === frame.raw)
    for (const child of contents) {
      assert(child.x >= frame.x + 28 && child.x + child.width <= frame.x + frame.width - 28, `组内宽度 ${child.id}`)
      assert(child.y >= frame.y + 78 && child.y + child.fixedHeight <= frame.y + frame.fixedHeight - 28, `组内高度 ${child.id}`)
    }
    for (let i = 0; i < contents.length; i++) for (let j = i + 1; j < contents.length; j++) {
      const one = contents[i], two = contents[j]
      assert(one.x + one.width <= two.x || two.x + two.width <= one.x || one.y + one.fixedHeight <= two.y || two.y + two.fixedHeight <= one.y, `组内卡片重叠 ${one.id}, ${two.id}`)
    }
  }
}
const empty = { type: 'action_group', binding_id: 'a_empty001', exports: {}, position: { x: 20, y: 30 } }
const drawing = buildWorkflowGraph(ruleWith([empty]), new Set([empty.binding_id]), {}, presentation)
assert(drawing.nodes.some(node => node.entryGate && node.raw === empty))
assert.equal(empty.workflow, undefined)
assert.equal(drawing.nodes.find(node => node.raw === empty && node.frame).fixedHeight, 206)
const routes = new Map([['fork', { kind: 'event_group', x: 0, y: 0, width: 400, outputs: [{ id: 'if', y: 80 }, { id: 'elif', y: 120 }, { id: 'else', y: 160 }] }], ['elif', { x: 528, y: 500, width: 228 }], ['else', { x: 528, y: 900, width: 228 }]])
const elifRoute = routeFlowEdge({ from: 'fork', to: 'elif', sourcePort: 'elif', channel: 'control' }, routes)
const elseRoute = routeFlowEdge({ from: 'fork', to: 'else', sourcePort: 'else', channel: 'control' }, routes)
assert(elifRoute.d.includes(' V ') && elseRoute.d.includes(' V '))
assert(elseRoute.labelX < elifRoute.labelX)
const output = { id: 'action-a_group001', source: group, kind: 'action_group', dataOutputs: [{ name: 'speed', index: 0, type: { type: 'number' }, required: true }] }
const decorative = { id: 'entry-a_group001', source: group, kind: 'branch', dataOutputs: [] }
const consumer = { id: 'action-a_consume1', kind: 'action', source: { binding_id: 'a_consume1', params: { speed: reference('step', group.binding_id, ['speed']) } }, availableStepIds: [group.binding_id], dataInputs: [{ name: 'speed', index: 0, label: '速度', type: { type: 'number' } }] }
assert.equal(deriveDataEdges({}, [output, decorative, consumer])[0]?.from, output.id)
console.log('原生流程图布局、折叠端点、嵌套边界、卡片间距、分支路由和导出数据线：通过')

const expandedGroup = eventGroup('roomgroup', 'all'), sibling = action('roomsibling'), descendant = action('roomdescendant', 'log', {}, expandedGroup.binding_id)
expandedGroup.position={ x:0, y:0 };sibling.position={ x:300, y:0 };descendant.position={ x:28, y:78 }
const roomGraph = ruleWith([expandedGroup, sibling, descendant]).workflow
makeGroupRoom(roomGraph, [{raw:expandedGroup,x:0,y:0,width:228,height:146},{raw:sibling,x:300,y:0,width:228,height:146}], [{raw:expandedGroup,x:0,y:0,width:650,height:300},{raw:sibling,x:300,y:0,width:228,height:146},{raw:descendant,x:28,y:78,width:228,height:146}], expandedGroup.binding_id)
assert(sibling.position.x >= 694, '展开组为相邻图块留出空间')
assert.deepEqual(descendant.position,{x:28,y:78},'展开组保留内部节点坐标')
assert.deepEqual(roomGraph.edges,[],'调整间距不添加连线')
const obstacleNodes = new Map([['source',{x:0,y:0,width:228,height:116}],['target',{x:750,y:260,width:228,height:116}],['obstacle',{x:400,y:80,width:228,height:116}]])
const avoided = routeFlowEdge({from:'source',to:'target',channel:'control'},obstacleNodes)
assert(avoided.d.includes(' Q '),'跨列执行线绕过中间卡片')
const orderedChoice = eventGroup('orderedchoice'), orderedElif = action('orderedelif'), orderedIf = action('orderedif')
const orderedGraph = ruleWith([orderedChoice,orderedElif,orderedIf],[wire(orderedChoice,orderedIf,'control',orderedChoice.branches[0].id),wire(orderedChoice,orderedElif,'control',orderedChoice.branches[1].id)]).workflow
arrangeWorkflow(orderedGraph,new Set([orderedChoice.binding_id]))
assert(orderedIf.position.y < orderedElif.position.y,'后续动作按 IF／ELIF 出口顺序排列，不按图块创建顺序排列')

const insertSource = action('insertsource'), insertTarget = action('inserttarget')
const insertGroup = { type: 'action_group', binding_id: 'a_insertgroup', exports: {}, position: { x: 340, y: 240 } }
const insertChild = action('insertchild', 'log', {}, insertGroup.binding_id)
insertSource.position = { x: 0, y: 0 }
insertTarget.position = { x: 208, y: 0 }
insertChild.position = { x: 624, y: 318 }
const insertRule = ruleWith([insertSource, insertTarget, insertGroup, insertChild], [wire(insertSource, insertTarget), wire(insertGroup, insertChild, 'control', 'entry')])
const insertExpanded = new Set([insertGroup.binding_id])
const beforeInsert = buildWorkflowGraph(insertRule, insertExpanded, {}, presentation).nodes
const inserted = action('insertnew')
inserted.position = { x: 308, y: 0 }
insertRule.workflow.nodes.push(inserted)
const childOffset = { x: insertChild.position.x - insertGroup.position.x, y: insertChild.position.y - insertGroup.position.y }
makeGroupRoom(insertRule.workflow, beforeInsert, buildWorkflowGraph(insertRule, insertExpanded, {}, presentation).nodes, inserted.binding_id)
const insertedLayout = buildWorkflowGraph(insertRule, insertExpanded, {}, presentation).nodes.filter(node => !node.gate && !node.raw.parent)
for (let i = 0; i < insertedLayout.length; i++) for (let j = i + 1; j < insertedLayout.length; j++) {
  const one = insertedLayout[i], two = insertedLayout[j]
  assert(one.x + one.width <= two.x || two.x + two.width <= one.x || one.y + one.height <= two.y || two.y + two.height <= one.y, `插入后卡片不能重叠 ${one.id}, ${two.id}`)
}
assert.deepEqual(insertSource.position, { x: 0, y: 0 }, '插入后继保留来源图块的位置')
assert.deepEqual({ x: insertChild.position.x - insertGroup.position.x, y: insertChild.position.y - insertGroup.position.y }, childOffset, '避让动作组时保持组内相对位置')
