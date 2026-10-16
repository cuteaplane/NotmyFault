import assert from 'node:assert/strict'
import { readFile, mkdir, rm, writeFile } from 'node:fs/promises'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'
import { build } from 'esbuild'
import { parse, compileScript } from '@vue/compiler-sfc'
import { JSDOM } from 'jsdom'

const root = fileURLToPath(new URL('../..', import.meta.url))
const temp = path.join(root, 'agent_files/temp/canvas-interaction')
await mkdir(temp, { recursive: true })
const vuePath = fileURLToPath(new URL('../node_modules/vue/index.mjs', import.meta.url))
const bundled = path.join(temp, 'canvas.mjs')
const canvasEntry = path.join(temp, 'canvas-entry.mjs')
const posix = value => value.replace(/\\/g, '/')
const relative = target => posix(path.relative(temp, target))
// 画布和共享状态从同一个入口打包，测试才能给画布准备插件清单。
await writeFile(canvasEntry, [
  `export { default as RuleCanvas } from ${JSON.stringify(relative(path.join(root, 'dashboard/src/components/RuleCanvas.vue')))}`,
  `export { store } from ${JSON.stringify(relative(path.join(root, 'dashboard/src/lib/store.js')))}`,
].join('\n'))
async function bundleEntry(entry, outfile) { await build({ entryPoints: [entry], outfile, bundle: true, format: 'esm', platform: 'node', plugins: [{ name: 'canvas-sfc', setup(builder) {
  builder.onResolve({ filter: /^vue$/ }, () => ({ path: pathToFileURL(vuePath).href, external: true }))
  builder.onLoad({ filter: /\.vue$/ }, async ({ path: filename }) => {
    const { descriptor } = parse(await readFile(filename, 'utf8'), { filename })
    return { contents: compileScript(descriptor, { id: 'canvas-test', inlineTemplate: true }).content, loader: 'js', resolveDir: path.dirname(filename) }
  })
} }] }) }
await bundleEntry(canvasEntry, bundled)
const dialogBundle = path.join(temp, 'test-dialog.mjs')
await bundleEntry(path.join(root, 'dashboard/src/components/TestRunDialog.vue'), dialogBundle)
const predicateBundle = path.join(temp, 'predicate.mjs')
await bundleEntry(path.join(root, 'dashboard/src/components/PredicateEditor.vue'), predicateBundle)
const dom = new JSDOM('<!doctype html><div id="app"></div>', { pretendToBeVisual: true })
for (const key of ['window', 'document', 'Element', 'HTMLElement', 'SVGElement', 'Node', 'Event', 'MouseEvent']) globalThis[key] = dom.window[key]
const rectangle = { left: 0, top: 0, right: 1200, bottom: 700, width: 1200, height: 700 }
globalThis.ResizeObserver = class { constructor(callback) { this.callback = callback } observe() { this.callback([{ contentRect: rectangle }]) } disconnect() {} }
let clock = 0
let nextFrame = 0
const frames = new Map()
globalThis.requestAnimationFrame = callback => { const id = ++nextFrame; frames.set(id, callback); return id }
globalThis.cancelAnimationFrame = id => frames.delete(id)
dom.window.HTMLElement.prototype.getBoundingClientRect = () => rectangle
const { createApp, h, ref, reactive, nextTick } = await import(pathToFileURL(vuePath).href)
const { RuleCanvas, store } = await import(pathToFileURL(bundled).href)
const { default: TestRunDialog } = await import(pathToFileURL(dialogBundle).href)
const { default: PredicateEditor } = await import(pathToFileURL(predicateBundle).href)
const triggerNode = { type: 'trigger', binding_id: 'a_trigger01', trigger: { type: 'missing', binding_id: 't_canvas01' }, occurrence: 'occurred', position: { x: 40, y: 900 } }
const conditionNode = { type: 'condition', binding_id: 'a_compare01', position: { x: 1600, y: 900 } }
const rule = reactive({ rule_id: 'r_canvas01', workflow: { entry: null, nodes: [triggerNode, conditionNode], edges: [] } })
const predicate = reactive({ op: 'eq', left: '', right: '' })
conditionNode.predicate = predicate
Object.assign(store.schema, {
  triggers: { missing: { name: '缺失事件', params: [], outputs: [{ name: 'state', label: '状态', type: 'text' }] } },
  actions: { log: { name: '日志', params: [{ name: 'message', label: '消息', type: 'string' }], outputs: [{ name: 'result', label: '结果', type: 'text' }] } },
})
const selected = ref(null), visible = ref(true), operations = []
const app = createApp({ setup: () => () => visible.value ? h(RuleCanvas, { rule, selectedNodeId: selected.value,
  'onUpdate:selectedNodeId': id => { selected.value = id }, 'onSelect-node': id => { selected.value = id }, onOperation: context => operations.push(context),
}) : null })
const flush = async () => { await nextTick(); await nextTick() }
function pointer(element, type, x, y, modifiers = {}) {
  const event = new dom.window.MouseEvent(type, { bubbles: true, cancelable: true, button: 0, clientX: x, clientY: y, ...modifiers })
  Object.defineProperty(event, 'pointerId', { value: 1 }); element.dispatchEvent(event); return event
}
const camera = () => document.querySelector('.node-canvas').style.transform
const panZoom = () => camera().match(/translate\(([-\d.e]+)px, ([-\d.e]+)px\) scale\(([-\d.e]+)\)/).slice(1).map(Number)
const advance = async (count = 1) => {
  for (let index = 0; index < count; index++) { clock += 16; const pending = [...frames.values()]; frames.clear(); pending.forEach(callback => callback(clock)); await flush() }
}
try {
  app.mount('#app'); await flush()
  const viewport = document.querySelector('.node-canvas-viewport')
  viewport.dispatchEvent(new dom.window.KeyboardEvent('keydown', { bubbles: true, key: 'ArrowUp' })); await flush()
  const initial = camera(), sourceElement = document.querySelector('.graph-node-trigger')
  sourceElement.click(); await flush(); assert.equal(camera(), initial, '鼠标选择节点时保持相机位置')
  sourceElement.dispatchEvent(new dom.window.FocusEvent('focus')); await flush(); assert.equal(camera(), initial, '鼠标焦点不触发居中')
  sourceElement.querySelector('.graph-node-data-summary button:last-child').click(); await flush()
  const port = sourceElement.querySelector('.data-port-dot.draggable'), beforeDrag = camera()
  pointer(port, 'pointerdown', 300, 695); pointer(port, 'pointerup', 300, 695); await flush()
  port.click(); await flush()
  assert(document.querySelector('.canvas-context-menu'), '数据端口单击后保留操作菜单')
  ;[...document.querySelectorAll('.canvas-context-menu button')].find(button => button.textContent.includes('创建变量并赋值')).click()
  await flush()
  assert.equal(operations[0].kind, 'data', '变量操作保留数据端口来源')
  assert.equal(operations[0].operation, 'create-variable')
  assert.equal(operations[0].node.id, 'action-a_trigger01')
  assert.equal(operations[0].port.name, 'state')
  const positions = [...document.querySelectorAll('.graph-node')].map(node => node.style.top)
  assert(pointer(port, 'pointerdown', 300, 695).defaultPrevented, '连接起点阻止默认焦点切换'); await flush()
  assert.equal(camera(), beforeDrag, '连接开始时不居中')
  assert.deepEqual([...document.querySelectorAll('.graph-node')].map(node => node.style.top), positions, '展开可连接输入时节点坐标保持原位')
  await advance(3); assert.equal(camera(), beforeDrag, '未拖动连接线时不进行边缘平移')
  pointer(window, 'pointermove', 1195, 695); await flush(); await advance(8)
  assert.notEqual(camera(), beforeDrag, '拖线至边缘后持续平移')
  const panned = camera(); await advance(4); assert.notEqual(camera(), panned, '指针停在边缘时继续平移')
  const [px, py, z] = panZoom(), endpoint = document.querySelector('.node-link-pending').getAttribute('d').match(/, ([\d.e-]+) ([\d.e-]+)$/).slice(1).map(Number)
  assert(Math.abs(endpoint[0] * z + px - 1195) < 1e-6, '缩放平移后的连线终点横坐标跟随指针')
  assert(Math.abs(endpoint[1] * z + py - 695) < 1e-6, '缩放平移后的连线终点纵坐标跟随指针')
  document.elementFromPoint = () => document.querySelector('[data-input-node="action-a_compare01"]')
  pointer(window, 'pointerup', 1195, 695); await flush()
  assert.deepEqual(JSON.parse(JSON.stringify(predicate.left)), { $ref: { scope: 'trigger', node: 't_canvas01', path: ['state'] } }, '连线写入真实条件对象')
  const afterDrop = camera(); await advance(3); assert.equal(camera(), afterDrop, '松开指针后停止平移')
  assert.equal(document.querySelector('.node-link-pending'), null)
  document.elementFromPoint = () => null
  pointer(port, 'pointerdown', 300, 600); pointer(window, 'pointermove', 1195, 600); await advance(2)
  window.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); await flush()
  const afterCancel = camera(); await advance(3); assert.equal(camera(), afterCancel, 'Escape取消拖线并停止平移')
  const header = sourceElement.querySelector('header'), beforeNodeDrag = camera()
  pointer(header, 'pointerdown', 250, 620); await flush(); assert.equal(camera(), beforeNodeDrag, '开始拖节点时不居中')
  pointer(header, 'pointermove', 300, 620); await flush(); pointer(header, 'pointerup', 300, 620); await flush()
  assert.notEqual(sourceElement.style.left, '40px')
  const movedPosition = { ...rule.workflow.nodes[0].position }
  pointer(header, 'pointerdown', 300, 620); pointer(header, 'pointermove', 390, 650); await flush()
  window.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key:'Escape', bubbles:true })); await flush()
  assert.deepEqual(rule.workflow.nodes[0].position,movedPosition,'取消节点拖拽恢复位置')
  await advance(30)
  assert.equal(parseFloat(sourceElement.style.left), movedPosition.x, '取消拖拽后卡片显示位置回到已保存坐标')
  const movedLeft = sourceElement.style.left

  const savedCamera = camera()
  visible.value = false; await flush(); visible.value = true; await flush()
  assert.equal(camera(), savedCamera, '卸载后重新挂载恢复视图')
  assert.equal(document.querySelector('.graph-node-trigger').style.left, movedLeft, '重新挂载恢复手动位置')
  assert(document.querySelector('.graph-node-trigger .data-port-dot.draggable'), '重新挂载恢复端口展开状态')
  const currentViewport = document.querySelector('.node-canvas-viewport')
  const editableCards = [document.querySelector('[data-node-id="action-a_trigger01"]'), document.querySelector('[data-node-id="action-a_compare01"]')]
  editableCards.forEach(card => card.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true, ctrlKey: true })))
  await flush()
  assert.equal(document.querySelectorAll('.multi-selected').length, 2, 'Ctrl 点击选择两个图块')
  currentViewport.dispatchEvent(new dom.window.MouseEvent('contextmenu', { bubbles: true, cancelable: true, button: 2, clientX: 500, clientY: 300 }))
  await flush()
  const groupButtons = [...document.querySelectorAll('.canvas-context-menu button')].filter(button => button.textContent.includes('组合'))
  assert.equal(groupButtons.length, 4, '多选菜单可组合 IF、AND、OR、NOT')
  groupButtons.find(button => button.textContent.includes('IF')).click()
  await flush()
  const grouping = operations.at(-1)
  assert.equal(grouping.operation, 'group-selection')
  assert.deepEqual(grouping.ids.sort(), ['a_compare01', 'a_trigger01'])
  assert.equal(grouping.value, 'if')
  assert.equal(document.querySelectorAll('.multi-selected').length, 0)
  const [bx, by, bz] = panZoom()
  pointer(currentViewport, 'pointerdown', bx + Math.min(triggerNode.position.x, conditionNode.position.x) * bz - 2, by + 898 * bz, { ctrlKey: true })
  pointer(currentViewport, 'pointermove', bx + (conditionNode.position.x + 228) * bz + 2, by + 1150 * bz, { ctrlKey: true })
  await flush()
  assert.equal(document.querySelectorAll('.multi-selected').length, 2, 'Ctrl 拖空白处框选两个图块')
  pointer(currentViewport, 'pointerup', bx + (conditionNode.position.x + 228) * bz + 2, by + 1150 * bz, { ctrlKey: true })
  await flush()
  assert.equal(document.querySelector('.canvas-selection-box'), null)
  currentViewport.dispatchEvent(new dom.window.MouseEvent('contextmenu', { bubbles: true, cancelable: true, button: 2, clientX: 800, clientY: 400 }))
  await flush()
  ;[...document.querySelectorAll('.canvas-context-menu button')].find(button => button.textContent.includes('创建触发器')).click()
  await flush()
  const creation = operations.at(-1)
  assert.equal(creation.operation, 'trigger')
  assert.equal(creation.kind, 'background')
  assert(Math.abs(creation.position.x - (800 - bx) / bz) < 1e-6)
  assert(Math.abs(creation.position.y - (400 - by) / bz) < 1e-6)
  app.unmount()
  const dialogHost = document.createElement('div'); document.body.append(dialogHost)
  const dialogRule = { rule_id:'r_range001', workflow:{ entry:null, nodes:[
    { type:'trigger', binding_id:'a_start001', trigger:{ type:'missing', binding_id:'t_start001', params:{} }, occurrence:'occurred' },
    { type:'action_group', binding_id:'a_group001', exports:{} },
    { type:'log', binding_id:'a_internal1', parent:'a_group001', params:{} },
    { type:'log', binding_id:'a_external1', params:{} },
  ], edges:[
    { source:'a_start001', port:'event', target:'a_group001', channel:'control' },
    { source:'a_group001', port:'entry', target:'a_internal1', channel:'control' },
    { source:'a_group001', port:'next', target:'a_external1', channel:'control' },
  ] } }
  const dialogOpen = ref(false), runPayloads = []
  const dialogApp = createApp({ setup: () => () => h(TestRunDialog, { open:dialogOpen.value, rule:dialogRule, schema:{ triggers:{ missing:{ outputs:[] } }, actions:{ log:{ outputs:[] } } }, onRun:payload => runPayloads.push(payload) }) })
  try {
    dialogApp.mount(dialogHost); dialogOpen.value = true; await flush()
    const range = document.querySelector('.test-range-controls select')
    range.value = 'until'; range.dispatchEvent(new dom.window.Event('change', { bubbles:true })); await flush()
    let destination = document.querySelectorAll('.test-range-controls select')[1]
    assert(destination.querySelector('option[value="a_internal1"]').disabled, '动作组内部节点不能作为主流程结束位置')
    assert(!destination.querySelector('option[value="a_external1"]').disabled, '主流程后续动作可作为结束位置')
    destination.value = 'a_internal1'; destination.dispatchEvent(new dom.window.Event('change', { bubbles:true })); await flush()
    document.querySelector('.test-prep-dialog .btn-filled').click(); await flush()
    assert.equal(runPayloads.length, 0)
    assert(document.querySelector('.test-prep-dialog').textContent.includes('结束节点必须在主流程中'))
    range.value = 'from'; range.dispatchEvent(new dom.window.Event('change', { bubbles:true })); await flush()
    destination = document.querySelectorAll('.test-range-controls select')[1]
    assert(!destination.querySelector('option[value="a_internal1"]').disabled, '内部节点允许从此处继续')
    destination.value = 'a_internal1'; destination.dispatchEvent(new dom.window.Event('change', { bubbles:true })); await flush()
    document.querySelector('.test-prep-dialog .btn-filled').click(); await flush()
    assert.equal(runPayloads[0]?.context.start_step_id, 'a_internal1')
    assert.equal(runPayloads[0]?.context.end_step_id, '')
  } finally { dialogApp.unmount(); dialogHost.remove() }
  const predicateHost = document.createElement('div'); document.body.append(predicateHost)
  const preboundReference = { $ref:{ scope:'step', node:'a_readtext001', path:['characters'] } }
  const preboundPredicate = reactive({ op:'eq', left:structuredClone(preboundReference), right:3 })
  const availableSources = ref([])
  const predicateApp = createApp({ setup: () => () => h(PredicateEditor, { node:preboundPredicate, rule:{ rule_id:'r_prebound', workflow:{ entry:null, nodes:[], edges:[] } }, sources:availableSources.value, shallow:true, leafOnly:true }) })
  try {
    predicateApp.mount(predicateHost); await flush()
    const operator = predicateHost.querySelector('.predicate-editor select')
    for (const value of ['gte', 'exists', 'lte']) {
      operator.value = value; operator.dispatchEvent(new dom.window.Event('change', { bubbles:true })); await flush()
      assert.deepEqual(JSON.parse(JSON.stringify(preboundPredicate.left)), preboundReference, '未接条件出口时切换判断方式保留预绑定动作输出')
    }
    availableSources.value = [{ label:'其他结果', type:'number', value:{ $ref:{ scope:'step', node:'a_other001', path:['value'] } } }]
    await flush()
    operator.value = 'gt'; operator.dispatchEvent(new dom.window.Event('change', { bubbles:true })); await flush()
    assert.deepEqual(JSON.parse(JSON.stringify(preboundPredicate.left)), preboundReference, '来源暂不可选时保留预绑定引用')
  } finally { predicateApp.unmount(); predicateHost.remove() }
  console.log('Canvas pointer focus, stable endpoints, edge pan, predicate binding, cancellation and view restoration: PASS')
} finally { if (app._instance) app.unmount(); dom.window.close(); await rm(temp, { recursive: true, force: true }) }
