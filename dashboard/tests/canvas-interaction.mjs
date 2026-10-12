import assert from 'node:assert/strict'
import { readFile, mkdir, rm } from 'node:fs/promises'
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
await build({ entryPoints: [path.join(root, 'dashboard/src/components/RuleCanvas.vue')], outfile: bundled, bundle: true, format: 'esm', platform: 'node', plugins: [{ name: 'canvas-sfc', setup(builder) {
  builder.onResolve({ filter: /^vue$/ }, () => ({ path: vuePath, external: true }))
  builder.onLoad({ filter: /\.vue$/ }, async ({ path: filename }) => {
    const { descriptor } = parse(await readFile(filename, 'utf8'), { filename })
    return { contents: compileScript(descriptor, { id: 'canvas-test', inlineTemplate: true }).content, loader: 'js', resolveDir: path.dirname(filename) }
  })
} }] })
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
const { default: RuleCanvas } = await import(pathToFileURL(bundled).href)
const rule = reactive({ rule_id: 'r_canvas01', condition: { type: 'missing', binding_id: 't_canvas01' }, actions: [] })
const predicate = reactive({ op: 'eq', left: '', right: '' })
const source = { id: 'trigger-t_canvas01', kind: 'trigger', source: rule.condition, x: 40, y: 900, label: '触发数据', dataInputs: [], dataOutputs: [{ id: 'output:state', name: 'state', index: 0, type: 'text', required: true }] }
const target = { id: 'predicate-a_canvas01-then-root', kind: 'predicate', source: predicate, x: 1600, y: 900, label: '判断状态', dataInputs: [{ id: 'input:left', name: 'left', index: 0, type: 'any', label: '判断数据' }], dataOutputs: [] }
const nested = { type: 'if', binding_id: 'a_nested01', condition: { op: 'eq', left: 1, right: 1 }, then: [], else: [] }
const override = { nodes: [source, target, { id: 'action-a_nested01', kind: 'action', readOnly: true, source: nested, index: 0, x: 2000, y: 900, dataInputs: [], dataOutputs: [], label: '只读来源' }], edges: [] }
const selected = ref(null), view = ref('main'), visible = ref(true), opened = [], operations = []
const app = createApp({ setup: () => () => visible.value ? h(RuleCanvas, { rule, graphOverride: override, selectedNodeId: selected.value, viewKey: view.value,
  'onUpdate:selectedNodeId': id => { selected.value = id }, 'onSelect-node': id => { selected.value = id }, 'onOpen-if': action => opened.push(action), onOperation: context => operations.push(context),
}) : null })
const flush = async () => { await nextTick(); await nextTick() }
function pointer(element, type, x, y) {
  const event = new dom.window.MouseEvent(type, { bubbles: true, cancelable: true, button: 0, clientX: x, clientY: y })
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
  assert.equal(operations[0].node.id, source.id)
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
  document.elementFromPoint = () => document.querySelector('[data-input-node="predicate-a_canvas01-then-root"]')
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
  const movedLeft = sourceElement.style.left; assert.notEqual(movedLeft, '40px')
  const savedCamera = camera(); view.value = 'child'; await flush(); view.value = 'main'; await flush()
  assert.equal(camera(), savedCamera, '返回主图恢复相机')
  assert.equal(document.querySelector('.graph-node-trigger').style.left, movedLeft, '返回主图恢复手动位置')
  assert(document.querySelector('.graph-node-trigger .data-port-dot.draggable'), '返回主图恢复端口展开状态')
  visible.value = false; await flush(); visible.value = true; await flush(); assert.equal(camera(), savedCamera, '卸载子图组件后恢复视图')
  const readonly = document.querySelector('.graph-node-action'); assert.equal(readonly.querySelector('footer'), null, '只读前序动作无修改按钮')
  readonly.click(); await flush(); assert.equal(opened.length, 0, '只读IF来源不进入子图')
  console.log('Canvas pointer focus, stable endpoints, edge pan, predicate binding, cancellation and view restoration: PASS')
} finally { app.unmount(); dom.window.close(); await rm(temp, { recursive: true, force: true }) }
