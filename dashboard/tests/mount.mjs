// Dashboard 挂载冒烟测试：在 jsdom 中加载构建产物，验证应用挂载与渲染。
// 运行：npm run build && npm test
import { JSDOM } from 'jsdom'
import fs from 'fs'
import path from 'path'
import { pathToFileURL } from 'url'

const distDir = process.env.DASHBOARD_DIST_DIR || 'dist'
const dom = new JSDOM('<!DOCTYPE html><html><head></head><body><div id="app"></div></body></html>', {
  url: 'http://127.0.0.1:19199/', pretendToBeVisual: true,
})
const { window } = dom

// mock API + SSE（不依赖真实引擎）
const bridgeCalls = []
let savedRulesPayload = null
let mockEngineRunning = true
window.matchMedia = () => ({ matches: false, addEventListener(){}, removeEventListener(){} })
window.ResizeObserver = class {
  constructor(callback) { this.callback = callback }
  observe() { this.callback([{ contentRect: { width: 818, height: 640 } }]) }
  disconnect() {}
}
window.EventSource = class { constructor(){} addEventListener(){} close(){} }
// Dashboard 只支持 pywebview；提供完整的最小 bridge 契约。
window.pywebview = { api: {
  get_config: async () => ({ rules: [{
    name: '挂载测试规则',
    folder: '测试',
    condition: {
      op: 'all',
      children: [
        { type: 'window_title', params: {} },
        {
          op: 'all',
          within_seconds: 30,
          children: [
            { type: 'window_title', params: {} },
            { type: 'window_title', params: {} },
          ],
        },
      ],
    },
    actions: [{ type: 'notify', params: {} }],
  }] }),
  save_config: async (rules) => {
    savedRulesPayload = JSON.parse(JSON.stringify(rules))
    return { ok: true }
  },
  get_api_token: async () => 'test-token',
  get_engine_status: async () => ({ api_alive:true, engine_running:mockEngineRunning, engine_state:mockEngineRunning ? 'running' : 'stopped', security_mode:'permissive', rules_count:1, triggers_count:1, actions_count:1, pid:1234 }),
  stop_engine: async () => { mockEngineRunning = false; return { ok:true, stopping:false } },
  launch_engine: async () => { mockEngineRunning = true; return { ok:true, api_alive:true, engine_running:true, engine_state:'running' } },
  shutdown_engine: async () => ({ ok:true }),
  request_api: async (path, method, data) => {
    bridgeCalls.push({ path, method, data })
    if (path === '/api/config/security-status') return { status:'ok', reason:'', summary:null }
    if (path.includes('/api/plugins/list')) return { triggers: T, actions: A }
    if (path.includes('/api/plugins')) return { triggers: T, actions: A }
    return { ok:true }
  },
} }
const T = {
  window_title: { id:'window_title', name:'窗口标题检测', description:'d', origin:'builtin', enabled:true, version_code:1, permissions:['native_api'], params:[], outputs:[{ name:'matched_title', label:'匹配标题', type:'string', sensitive:true }] },
  window_title_alt: { id:'window_title_alt', name:'窗口标题备用', description:'d', origin:'builtin', enabled:true, version_code:1, permissions:['native_api'], params:[], outputs:[{ name:'matched_title', label:'匹配标题', type:'string', sensitive:true }] },
  time_schedule: { id:'time_schedule', name:'定时', description:'d', origin:'builtin', enabled:true, version_code:1, permissions:[], params:[], outputs:[] },
  clipboard: { id:'clipboard', name:'剪贴板监控', description:'d', origin:'builtin', enabled:true, version_code:1, permissions:['clipboard','native_api'], params:[], outputs:[] },
}
const A = { notify: { id:'notify', name:'显示通知', description:'d', origin:'builtin', enabled:true, version_code:1, permissions:[], params:[{ name:'message', label:'消息', type:'string', default:'' }] } }
window.fetch = async (url) => {
  const u = String(url); const j = (o) => ({ json: async () => o, ok: true, status: 200 })
  if (u.includes('/api/plugins/list')) return j({ triggers: T, actions: A })
  if (u.includes('/api/plugins')) return j({ triggers: T, actions: A })
  if (u.includes('/api/engine/status')) return j({ running:true, engine_running:true, security_mode:'permissive', rules_count:1, triggers_count:1, actions_count:1, pid:1234 })
  if (u.includes('/api/rules')) return j({ rules: [] })
  return j({})
}

global.window = window
global.document = window.document
for (const key of Object.getOwnPropertyNames(window)) {
  if (key === 'navigator') continue
  if (!(key in global)) { try { global[key] = window[key] } catch (e) {} }
}
Object.defineProperty(global, 'navigator', { value: window.navigator, configurable: true })
global.requestAnimationFrame = (cb) => setTimeout(cb, 0)
global.cancelAnimationFrame = (id) => clearTimeout(id)
global.fetch = window.fetch  // 覆盖 node 原生 fetch，确保用 mock

const jsFile = fs.readdirSync(path.join(distDir, 'assets')).find(f => f.endsWith('.js'))
await import(pathToFileURL(path.resolve(distDir, 'assets', jsFile)).href)
await new Promise(r => setTimeout(r, 1000))

const html = document.getElementById('app').innerHTML
const versionSource = fs.readFileSync(path.resolve('../notmyfault/version.py'), 'utf8')
const expectedVersion = versionSource.match(/^__version__\s*=\s*["']([^"']+)["']/m)?.[1]
const checks = [
  ['nav-rail', html.includes('nav-rail')],
  ['brand NotmyFault', html.includes('NotmyFault')],
  ['global logo appears in navigation', !!document.querySelector('.nav-brand img.brand-icon')?.getAttribute('src')],
  ['global logo is used as favicon', !!document.querySelector('link[rel="icon"]')?.getAttribute('href')],
  ['apatch-hero', html.includes('apatch-hero')],
  ['home heading', html.includes('引擎状态')],
  ['engine running', html.includes('运行中')],
  ['security mode', html.includes('宽松')],
  ['dashboard version follows Python package', !!expectedVersion && html.includes(expectedVersion)],
  ['full engine stop stays hidden while automation runs', !html.includes('彻底停止引擎')],
]
let ok = true
for (const [name, pass] of checks) { console.log((pass?'PASS':'FAIL')+' - '+name); if(!pass) ok=false }
if (!ok) { console.error(html.substring(0, 600)); process.exit(1) }

const pauseAutomation = [...document.querySelectorAll('button')].find(
  button => button.textContent.includes('暂停自动化'),
)
pauseAutomation?.click()
await new Promise(r => setTimeout(r, 50))
const pausedControls = [...document.querySelectorAll('.apatch-hero .hero-control')]
const pausedControlsOk = pausedControls.length === 2
  && pausedControls.some(button => button.textContent.includes('启动自动化'))
  && pausedControls.some(button => button.textContent.includes('彻底停止引擎'))
console.log((pausedControlsOk?'PASS':'FAIL')+' - paused automation reveals two unified engine controls')
if (!pausedControlsOk) process.exit(1)

const rulesNav = [...document.querySelectorAll('button')].find(
  button => button.textContent.includes('规则'),
)
rulesNav?.click()
await new Promise(r => setTimeout(r, 50))
document.querySelector('.rule-library-row')?.click()
await new Promise(r => setTimeout(r, 50))
const editorHeaderOk = document.querySelector('.rule-title-capsule')?.textContent.includes('挂载测试规则')
  && document.querySelector('.rule-title-edit .material-symbols-outlined')?.textContent === 'edit'
  && document.querySelector('.rule-folder-button')?.textContent.includes('测试')
  && !document.querySelector('.editor-mode-bar')?.textContent.includes('保留原有分步编辑')
console.log((editorHeaderOk?'PASS':'FAIL')+' - rule header uses title capsule and aligned folder control')
if (!editorHeaderOk) process.exit(1)
document.querySelector('.rule-title-edit')?.click()
await new Promise(r => setTimeout(r, 20))
const ruleNameInput = document.querySelector('.rule-title-capsule input')
const cleanRuleNameInput = ruleNameInput?.type === 'text'
  && ruleNameInput?.getAttribute('autocomplete') === 'off'
  && ruleNameInput?.getAttribute('spellcheck') === 'false'
console.log((cleanRuleNameInput?'PASS':'FAIL')+' - rule name editor suppresses native input decorations')
if (!cleanRuleNameInput) process.exit(1)
window.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
await new Promise(r => setTimeout(r, 20))
document.querySelector('.rule-folder-button')?.click()
await new Promise(r => setTimeout(r, 20))
const folderPickerOk = document.querySelector('.folder-picker-dialog')?.textContent.includes('管理文件夹')
  && document.querySelector('.folder-picker-dialog')?.textContent.includes('新建并使用') === false
  && document.querySelector('.folder-picker-dialog')?.textContent.includes('测试')
console.log((folderPickerOk?'PASS':'FAIL')+' - folder button opens quick folder manager')
if (!folderPickerOk) process.exit(1)
document.querySelector('.folder-picker-dialog .plugin-picker-head .icon-btn')?.click()
await new Promise(r => setTimeout(r, 20))
const canvasMode = document.querySelector('.node-editor-workspace')
const formMode = document.querySelector('.classic-rule-editor')
const canvasModeButton = [...document.querySelectorAll('button')].find(
  button => button.textContent.includes('节点编辑'),
)
const formModeButtonInitial = [...document.querySelectorAll('button')].find(
  button => button.textContent.includes('普通模式'),
)
const canvasStartsVisible = canvasMode?.style.display !== 'none'
formModeButtonInitial?.click()
await new Promise(r => setTimeout(r, 20))
const formCanOpen = formMode?.style.display !== 'none' && canvasMode?.style.display === 'none'
canvasModeButton?.click()
await new Promise(r => setTimeout(r, 20))
const modeSwitchOk = canvasStartsVisible && formCanOpen
  && formMode?.style.display === 'none' && canvasMode?.style.display !== 'none'
  && window.localStorage.getItem('notmyfault.ruleEditorMode') === 'canvas'
console.log((modeSwitchOk?'PASS':'FAIL')+' - canvas and step editor modes coexist')
if (!modeSwitchOk) process.exit(1)

const graphShapeOk = document.querySelectorAll('.graph-node-trigger').length === 3
  && document.querySelectorAll('.graph-node-condition').length === 2
  && document.querySelectorAll('.node-link-condition').length === 4
console.log((graphShapeOk?'PASS':'FAIL')+' - condition tree renders as editable graph branches')
if (!graphShapeOk) process.exit(1)
const defaultNodes = [...document.querySelectorAll('.graph-node')].map(node => ({
  left: Number.parseFloat(node.style.left),
  top: Number.parseFloat(node.style.top),
  height: Number.parseFloat(node.style.height),
}))
const columns = [...new Set(defaultNodes.map(node => node.left))].sort((a, b) => a - b)
const horizontalClearances = columns.slice(1).map((left, index) => left - columns[index] - 228)
const verticalClearances = []
for (const left of columns) {
  const columnNodes = defaultNodes.filter(node => node.left === left).sort((a, b) => a.top - b.top)
  columnNodes.slice(1).forEach((node, index) => {
    const previous = columnNodes[index]
    verticalClearances.push(node.top - previous.top - previous.height)
  })
}
const defaultSpacingOk = Math.min(...horizontalClearances) >= 96
  && Math.min(...verticalClearances) >= 56
console.log((defaultSpacingOk?'PASS':'FAIL')+' - default layout leaves readable space between nodes')
if (!defaultSpacingOk) process.exit(1)
const portsStartCollapsed = document.querySelectorAll('.graph-node-data-summary').length > 0
  && document.querySelectorAll('.graph-node-data .data-port-row').length === 0
document.querySelector('.graph-node-trigger .graph-node-data-summary button:last-child')?.click()
document.querySelector('.graph-node-action .graph-node-data-summary button:first-child')?.click()
await new Promise(r => setTimeout(r, 20))
const typedPortsOk = portsStartCollapsed
  && document.querySelectorAll('.graph-node-trigger .data-port-column-output .data-port-row').length > 0
  && document.querySelectorAll('.graph-node-action .data-port-column-input .data-port-row').length > 0
  && document.querySelector('.data-type-chip')?.textContent.length > 0
console.log((typedPortsOk?'PASS':'FAIL')+' - typed data ports expand on demand')
if (!typedPortsOk) process.exit(1)

// 展开端口后，同一列里的节点仍应被自动推开。
const positionedNodes = [...document.querySelectorAll('.graph-node')].map(node => ({
  left: Number.parseFloat(node.style.left),
  top: Number.parseFloat(node.style.top),
  height: Number.parseFloat(node.style.height),
}))
let layoutDoesNotOverlap = true
for (let i = 0; i < positionedNodes.length; i += 1) {
  for (let j = i + 1; j < positionedNodes.length; j += 1) {
    const a = positionedNodes[i]
    const b = positionedNodes[j]
    if (Math.abs(a.left - b.left) >= 228) continue
    const separated = a.top + a.height + 24 <= b.top || b.top + b.height + 24 <= a.top
    if (!separated) layoutDoesNotOverlap = false
  }
}
console.log((layoutDoesNotOverlap?'PASS':'FAIL')+' - expanding ports keeps nodes from overlapping')
if (!layoutDoesNotOverlap) process.exit(1)

document.querySelector('.graph-node-condition .graph-node-logic-actions button')?.click()
await new Promise(r => setTimeout(r, 20))
const logicNodeAddsDirectly = document.querySelector('.plugin-picker-dialog')?.textContent.includes('添加条件')
console.log((logicNodeAddsDirectly?'PASS':'FAIL')+' - logic node exposes direct condition creation')
if (!logicNodeAddsDirectly) process.exit(1)
document.querySelector('.plugin-picker-head .icon-btn')?.click()
await new Promise(r => setTimeout(r, 20))

// 悬停叶节点时，条件汇合后的后续动作也属于下游链路，不能被置灰。
const traceLeaf = document.querySelector('.graph-node-trigger')
const traceAction = document.querySelector('.graph-node-action')
traceLeaf?.dispatchEvent(new window.Event('pointerenter'))
await new Promise(r => setTimeout(r, 20))
const tracePathOk = traceAction && !traceAction.classList.contains('dimmed')
console.log((tracePathOk?'PASS':'FAIL')+' - hover traces the full downstream execution path')
if (!tracePathOk) process.exit(1)
traceLeaf?.dispatchEvent(new window.Event('pointerleave'))

const viewportEl = document.querySelector('.node-canvas-viewport')
const minimapEl = document.querySelector('.node-minimap')
const zoomControls = document.querySelector('.node-canvas-zoom')
const zoomLabel = document.querySelector('.zoom-label')
const zoomIn = [...document.querySelectorAll('.node-canvas-zoom .icon-btn')].find(b => b.querySelector('.material-symbols-outlined')?.textContent === 'add')
const zoomFit = [...document.querySelectorAll('.node-canvas-zoom .icon-btn')].find(b => b.querySelector('.material-symbols-outlined')?.textContent === 'fit_screen')
console.log((viewportEl?'PASS':'FAIL')+' - canvas viewport element renders')
if (!viewportEl) process.exit(1)
const zoomUiOk = !!zoomControls && !!zoomLabel && !!zoomIn && !!zoomFit
console.log((zoomUiOk?'PASS':'FAIL')+' - zoom controls render (+ / - / fit / %)')
if (!zoomUiOk) process.exit(1)
console.log((minimapEl?'PASS':'FAIL')+' - minimap renders')
if (!minimapEl) process.exit(1)

// 默认窗口下优先保证节点文字可读；长流程允许横向平移。
const zoomTextOk = zoomLabel.textContent.includes('72')
console.log((zoomTextOk?'PASS':'FAIL')+' - initial zoom keeps long flows readable')
if (!zoomTextOk) process.exit(1)

// 选中视口边缘的节点时，画布应主动把节点和设置框带回可编辑区域。
const revealAction = document.querySelector('.graph-node-action')
const revealHandle = revealAction?.querySelector('.graph-node-head')
const cameraBeforeEdgeInteraction = document.querySelector('.node-canvas')?.style.transform
revealAction?.click()
await new Promise(r => setTimeout(r, 20))
revealHandle?.dispatchEvent(new window.MouseEvent('pointerdown', { bubbles: true, button: 0, clientX: 0, clientY: 0 }))
revealHandle?.dispatchEvent(new window.MouseEvent('pointermove', { bubbles: true, button: 0, clientX: 900, clientY: 0 }))
revealHandle?.dispatchEvent(new window.MouseEvent('pointerup', { bubbles: true, button: 0, clientX: 900, clientY: 0 }))
// 浏览器会在 pointerup 后补发 click；这里显式模拟，确保拖动不会把抽屉重新打开。
revealAction?.click()
await new Promise(r => setTimeout(r, 20))
const dragClosesInspector = !document.querySelector('.node-inspector')
console.log((dragClosesInspector?'PASS':'FAIL')+' - dragging a node closes the inspector without reopening it')
if (!dragClosesInspector) process.exit(1)
const manualLeftBeforePorts = revealAction?.style.left
const revealInputToggle = revealAction?.querySelector('.graph-node-data-summary button:first-child')
revealInputToggle?.click()
await new Promise(r => setTimeout(r, 20))
const manualNodeStaysPut = revealAction?.style.left === manualLeftBeforePorts
console.log((manualNodeStaysPut?'PASS':'FAIL')+' - expanding ports preserves manually placed nodes')
if (!manualNodeStaysPut) process.exit(1)
revealInputToggle?.click()
await new Promise(r => setTimeout(r, 20))
window.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
await new Promise(r => setTimeout(r, 20))
const cameraBeforeReveal = document.querySelector('.node-canvas')?.style.transform
revealAction?.click()
await new Promise(r => setTimeout(r, 30))
const cameraAfterReveal = document.querySelector('.node-canvas')?.style.transform
const edgeInspector = document.querySelector('.node-inspector')
const edgeNodeRevealOk = (cameraBeforeEdgeInteraction !== cameraBeforeReveal || cameraBeforeReveal !== cameraAfterReveal)
  && edgeInspector?.getAttribute('aria-label') === '节点设置'
  && !edgeInspector.hasAttribute('style')
console.log((edgeNodeRevealOk?'PASS':'FAIL')+' - edge node selection pans the canvas beside the inspector drawer')
if (!edgeNodeRevealOk) process.exit(1)
document.querySelector('.node-canvas-status .icon-btn')?.click()
window.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
await new Promise(r => setTimeout(r, 20))

// 点击放大按钮，验证 zoom label 更新
zoomIn.click()
await new Promise(r => setTimeout(r, 50))
const zoomedLabel = document.querySelector('.zoom-label')
const zoomChanged = zoomedLabel && !zoomedLabel.textContent.includes('100')
console.log((zoomChanged?'PASS':'FAIL')+' - zoom button changes zoom level')
if (!zoomChanged) process.exit(1)

// “适应全部”仍能覆盖全图，不受首次可读缩放下限影响。
zoomFit.click()
await new Promise(r => setTimeout(r, 50))
const fittedZoom = Number.parseInt(document.querySelector('.zoom-label')?.textContent, 10)
const fitViewOk = Number.isFinite(fittedZoom) && fittedZoom < 72
console.log((fitViewOk?'PASS':'FAIL')+' - fit view can still show the entire graph')
if (!fitViewOk) process.exit(1)

document.querySelector('.graph-node-trigger')?.click()
await new Promise(r => setTimeout(r, 20))
const leafTriggerTypeButton = document.querySelector('.node-inspector .plugin-type-button')
const leafInspectorOk = leafTriggerTypeButton?.textContent.includes('窗口标题检测')
  && document.querySelector('.node-inspector')?.getAttribute('role') === 'complementary'
console.log((leafInspectorOk?'PASS':'FAIL')+' - graph leaf opens the trigger editor drawer')
if (!leafInspectorOk) process.exit(1)
leafTriggerTypeButton?.click()
await new Promise(r => setTimeout(r, 20))
const nodeTriggerUsesPicker = document.querySelector('.plugin-picker-dialog')?.textContent.includes('更换触发方式')
console.log((nodeTriggerUsesPicker?'PASS':'FAIL')+' - node trigger type reuses the plugin picker')
if (!nodeTriggerUsesPicker) process.exit(1)
document.querySelector('.plugin-picker-head .icon-btn')?.click()
await new Promise(r => setTimeout(r, 20))
viewportEl.dispatchEvent(new window.MouseEvent('pointerdown', { bubbles: true, button: 0, clientX: 10, clientY: 10 }))
viewportEl.dispatchEvent(new window.MouseEvent('pointerup', { bubbles: true, button: 0, clientX: 10, clientY: 10 }))
await new Promise(r => setTimeout(r, 20))
const blankCanvasClosesInspector = !document.querySelector('.node-inspector')
console.log((blankCanvasClosesInspector?'PASS':'FAIL')+' - blank canvas closes the node editor')
if (!blankCanvasClosesInspector) process.exit(1)
document.querySelector('.graph-node-trigger')?.click()
await new Promise(r => setTimeout(r, 20))
window.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
await new Promise(r => setTimeout(r, 20))
const escapeClosesInspector = !document.querySelector('.node-inspector')
console.log((escapeClosesInspector?'PASS':'FAIL')+' - Escape closes the node editor')
if (!escapeClosesInspector) process.exit(1)

// 收起目标输入后开始拖线：只在拖动期间展开兼容输入，并弱化不兼容节点。
document.querySelector('.graph-node-action .graph-node-data-summary button:first-child')?.click()
await new Promise(r => setTimeout(r, 20))
const outputPort = document.querySelector('.graph-node-trigger .data-port-column-output .data-port-dot')
outputPort?.dispatchEvent(new window.MouseEvent('pointerdown', { bubbles: true, button: 0, clientX: 80, clientY: 160 }))
await new Promise(r => setTimeout(r, 20))
const dragTemporarilyExpandsCompatibleInputs = document.querySelectorAll('.graph-node-action .data-port-column-input .data-port-row').length > 0
  && document.querySelectorAll('.graph-node.data-incompatible').length > 0
console.log((dragTemporarilyExpandsCompatibleInputs?'PASS':'FAIL')+' - data drag reveals only compatible inputs')
if (!dragTemporarilyExpandsCompatibleInputs) process.exit(1)
viewportEl?.dispatchEvent(new window.Event('pointercancel', { bubbles: true }))
await new Promise(r => setTimeout(r, 20))
const temporaryInputsCloseAfterCancel = document.querySelectorAll('.graph-node-action .data-port-column-input .data-port-row').length === 0
console.log((temporaryInputsCloseAfterCancel?'PASS':'FAIL')+' - temporary data ports close after cancel')
if (!temporaryInputsCloseAfterCancel) process.exit(1)

outputPort?.dispatchEvent(new window.MouseEvent('pointerdown', { bubbles: true, button: 0, clientX: 80, clientY: 160 }))
await new Promise(r => setTimeout(r, 20))
const inputPort = document.querySelector('.graph-node-action .data-port-column-input .data-port-row')
inputPort?.dispatchEvent(new window.MouseEvent('pointerup', { bubbles: true, button: 0, clientX: 330, clientY: 160 }))
await new Promise(r => setTimeout(r, 20))
const dragBindingOk = document.querySelectorAll('.node-link-data').length === 1
console.log((dragBindingOk?'PASS':'FAIL')+' - dragging typed ports creates a data binding')
if (!dragBindingOk) process.exit(1)

document.querySelector('.graph-node-action')?.click()
await new Promise(r => setTimeout(r, 20))
document.querySelector('.field-binding-button')?.click()
await new Promise(r => setTimeout(r, 20))
const bindingSelect = document.querySelector('.binding-picker select')
const bindingOption = [...(bindingSelect?.querySelectorAll('option') || [])].find(option => option.value)
if (bindingSelect && bindingOption) {
  bindingSelect.value = bindingOption.value
  bindingSelect.dispatchEvent(new window.Event('change', { bubbles: true }))
}
await new Promise(r => setTimeout(r, 20))
const bindingUse = [...document.querySelectorAll('.binding-picker button')].find(
  button => button.textContent.includes('使用'),
)
bindingUse?.click()
await new Promise(r => setTimeout(r, 20))
const bindingPickerOk = !!document.querySelector('.binding-value')
console.log((bindingPickerOk?'PASS':'FAIL')+' - action parameter accepts trigger runtime data')
if (!bindingPickerOk) {
  console.error(document.querySelector('.node-inspector')?.innerHTML?.slice(0, 1600))
  process.exit(1)
}
const dataEdgeOk = document.querySelectorAll('.node-link-data').length === 1
console.log((dataEdgeOk?'PASS':'FAIL')+' - structured binding renders as a data edge')
if (!dataEdgeOk) process.exit(1)

// 收起两侧以后，只保留已连线端口；数据线和端口名称都不能消失。
document.querySelector('.graph-node-trigger .graph-node-data-summary button:last-child')?.click()
document.querySelector('.graph-node-action .graph-node-data-summary button:first-child')?.click()
await new Promise(r => setTimeout(r, 20))
const connectedPortsStayVisible = document.querySelectorAll('.graph-node-trigger .data-port-column-output .data-port-row').length === 1
  && document.querySelectorAll('.graph-node-action .data-port-column-input .data-port-row').length === 1
  && document.querySelectorAll('.node-link-data').length === 1
console.log((connectedPortsStayVisible?'PASS':'FAIL')+' - connected ports stay visible when collapsed')
if (!connectedPortsStayVisible) process.exit(1)

const saveRun = [...document.querySelectorAll('button')].find(
  button => button.textContent.includes('测试规则'),
)
saveRun?.click()
await new Promise(r => setTimeout(r, 100))
const snapshotCall = bridgeCalls.find(call => call.path === '/api/rules/0/run')
const snapshotBinding = snapshotCall?.data?.rule?.actions?.[0]?.params?.message?.$ref
const snapshotOk = snapshotCall?.data?.rule?.name === '挂载测试规则'
  && snapshotBinding?.scope === 'trigger'
  && snapshotBinding?.path?.[0] === 'matched_title'
console.log((snapshotOk?'PASS':'FAIL')+' - test-rule sends exact saved rule snapshot')
if (!snapshotOk) process.exit(1)

// ================================================================
// 绑定生命周期回归（binding_id 保留 / 敏感标记 / 完整 payload）
// ================================================================

// 敏感输出标记：mock 的 matched_title 声明了 sensitive:true。
// 动作参数已绑定该输出，点"更换"重新打开选择器检查标记与警告。
const changeBindingBtn = [...document.querySelectorAll('.binding-value button')].find(
  button => button.textContent.includes('更换'),
)
changeBindingBtn?.click()
await new Promise(r => setTimeout(r, 20))
const sensitiveOption = [...document.querySelectorAll('.binding-picker option')].find(
  option => option.textContent.includes('【敏感】'),
)
console.log((sensitiveOption?'PASS':'FAIL')+' - binding picker marks sensitive outputs')
if (!sensitiveOption) process.exit(1)
const pickerSelect = document.querySelector('.binding-picker select')
pickerSelect.value = sensitiveOption.value
pickerSelect.dispatchEvent(new window.Event('change', { bubbles: true }))
await new Promise(r => setTimeout(r, 20))
const sensitiveWarn = document.querySelector('.binding-picker .text-warn')
console.log((sensitiveWarn?'PASS':'FAIL')+' - selecting sensitive source shows propagation warning')
if (!sensitiveWarn) process.exit(1)
const pickerCancel = [...document.querySelectorAll('.binding-picker button')].find(
  button => button.textContent.includes('取消'),
)
pickerCancel?.click()
await new Promise(r => setTimeout(r, 20))

function collectLeafIds(node, acc = []) {
  if (!node || typeof node !== 'object') return acc
  if (node.type && !Array.isArray(node.children) && !Array.isArray(node.events)) {
    acc.push(node.binding_id)
    return acc
  }
  ;(node.children || node.events || []).forEach(child => collectLeafIds(child, acc))
  return acc
}
async function clickSaveRule() {
  const saveButton = [...document.querySelectorAll('.rule-editor-actions button')].find(
    button => button.textContent.includes('保存规则'),
  )
  const enabled = saveButton && !saveButton.disabled
  saveButton?.click()
  await new Promise(r => setTimeout(r, 100))
  return enabled
}

const originalLeafIds = collectLeafIds(savedRulesPayload?.[0]?.condition)
const originalBindingNode = savedRulesPayload?.[0]?.actions?.[0]?.params?.message?.$ref?.node

// A. 组合条件 → 单个条件：保留选中叶节点的 binding_id，已有 $ref 继续有效
const formModeButton = [...document.querySelectorAll('button')].find(
  button => button.textContent.includes('普通模式'),
)
formModeButton?.click()
await new Promise(r => setTimeout(r, 50))
const firstClassicCard = document.querySelector('.classic-rule-editor .condition-flow-card')
const firstClassicSummary = firstClassicCard?.querySelector('summary')
const classicOpenBefore = !!firstClassicCard?.open
firstClassicSummary?.click()
await new Promise(r => setTimeout(r, 20))
const classicCardExpandsInPlace = !!firstClassicCard && firstClassicCard.open !== classicOpenBefore
console.log((classicCardExpandsInPlace?'PASS':'FAIL')+' - classic cards still expand in place')
if (!classicCardExpandsInPlace) process.exit(1)
const existingTriggerTypeButton = firstClassicCard?.querySelector('.plugin-type-button')
existingTriggerTypeButton?.click()
await new Promise(r => setTimeout(r, 20))
const existingTriggerUsesPicker = document.querySelector('.plugin-picker-dialog')?.textContent.includes('更换触发方式')
console.log((existingTriggerUsesPicker?'PASS':'FAIL')+' - existing trigger type reuses the plugin picker')
if (!existingTriggerUsesPicker) process.exit(1)
document.querySelector('.plugin-picker-head .icon-btn')?.click()
await new Promise(r => setTimeout(r, 20))
const existingActionTypeButton = document.querySelector('.classic-rule-editor .action-flow-card .plugin-type-button')
existingActionTypeButton?.click()
await new Promise(r => setTimeout(r, 20))
const existingActionUsesPicker = document.querySelector('.plugin-picker-dialog')?.textContent.includes('更换动作类型')
console.log((existingActionUsesPicker?'PASS':'FAIL')+' - existing action type reuses the plugin picker')
if (!existingActionUsesPicker) process.exit(1)
document.querySelector('.plugin-picker-head .icon-btn')?.click()
await new Promise(r => setTimeout(r, 20))
const toSingleBtn = [...document.querySelectorAll('.classic-rule-editor button')].find(
  button => button.textContent.includes('改为单个条件'),
)
toSingleBtn?.click()
await new Promise(r => setTimeout(r, 50))
const saveAEnabled = await clickSaveRule()
const singleEventId = savedRulesPayload?.[0]?.event?.binding_id
const singleOk = saveAEnabled
  && originalLeafIds.length === 3
  && originalBindingNode === originalLeafIds[0]
  && singleEventId === originalLeafIds[0]
  && savedRulesPayload?.[0]?.actions?.[0]?.params?.message?.$ref?.node === singleEventId
console.log((singleOk?'PASS':'FAIL')+' - condition to single event keeps trigger binding_id')
if (!singleOk) process.exit(1)

// 单触发器也通过插件选择器更换类型，并保留下游数据引用依赖的 binding_id。
const singleTriggerTypeButton = document.querySelector('.classic-rule-editor .condition-flow-card .plugin-type-button')
singleTriggerTypeButton?.click()
await new Promise(r => setTimeout(r, 20))
const singleTriggerPickerOk = document.querySelector('.plugin-picker-dialog')?.textContent.includes('更换触发方式')
const compatibleTriggerItem = [...document.querySelectorAll('.plugin-picker-item')].find(
  item => item.textContent.includes('窗口标题备用'),
)
compatibleTriggerItem?.click()
await new Promise(r => setTimeout(r, 50))
const saveSingleReplaceEnabled = await clickSaveRule()
const singleReplaceOk = singleTriggerPickerOk
  && saveSingleReplaceEnabled
  && savedRulesPayload?.[0]?.event?.type === 'window_title_alt'
  && savedRulesPayload?.[0]?.event?.binding_id === singleEventId
  && savedRulesPayload?.[0]?.actions?.[0]?.params?.message?.$ref?.node === singleEventId
console.log((singleReplaceOk?'PASS':'FAIL')+' - changing a single trigger uses picker and keeps binding_id')
if (!singleReplaceOk) process.exit(1)

// B. 单个条件 → 组合条件：保留 binding_id（any 组合只有一个分支时仍保证命中）
const toComboBtn = [...document.querySelectorAll('.classic-rule-editor button')].find(
  button => button.textContent.includes('并且满足'),
)
toComboBtn?.click()
await new Promise(r => setTimeout(r, 50))
document.querySelector('.plugin-picker-item')?.click()
await new Promise(r => setTimeout(r, 50))
const saveBEnabled = await clickSaveRule()
const upgradedChildId = savedRulesPayload?.[0]?.condition?.children?.[0]?.binding_id
const upgradeOk = saveBEnabled
  && upgradedChildId === singleEventId
  && savedRulesPayload?.[0]?.actions?.[0]?.params?.message?.$ref?.node === upgradedChildId
console.log((upgradeOk?'PASS':'FAIL')+' - single event to conditions keeps trigger binding_id')
if (!upgradeOk) process.exit(1)

// C. 分步编辑：新增条件必须生成 binding_id；更换触发器类型必须保留 binding_id。
// 先添加条件（any 组合下分支不保证命中，动作引用会判不可用），
// 再把根组合切到 all，保证全部分支命中后保存。
const addConditionBtn = [...document.querySelectorAll('.classic-rule-editor .flow-add-row button')].find(
  button => button.textContent.includes('添加条件') && !button.textContent.includes('组'),
)
addConditionBtn?.click()
await new Promise(r => setTimeout(r, 50))
document.querySelector('.plugin-picker-item')?.click()
await new Promise(r => setTimeout(r, 50))
const opSelect = document.querySelector('.classic-rule-editor .condition-op')
opSelect.value = 'all'
opSelect.dispatchEvent(new window.Event('change', { bubbles: true }))
await new Promise(r => setTimeout(r, 50))
const saveCEnabled = await clickSaveRule()
const addedChildId = savedRulesPayload?.[0]?.condition?.children?.at(-1)?.binding_id
const addedOk = saveCEnabled && typeof addedChildId === 'string' && addedChildId.startsWith('t_')
console.log((addedOk?'PASS':'FAIL')+' - step editor new condition gets a binding_id')
if (!addedOk) process.exit(1)

// 保存后编辑器会用服务端快照替换草稿，等待这次渲染完成再操作新节点。
await new Promise(r => setTimeout(r, 150))
const conditionCards = document.querySelectorAll('.classic-rule-editor .condition-flow-card')
const typeButton = conditionCards[conditionCards.length - 1]?.querySelector('.plugin-type-button')
typeButton?.click()
await new Promise(r => setTimeout(r, 20))
const nextTriggerType = 'window_title_alt'
const replacementTriggerItem = [...document.querySelectorAll('.plugin-picker-item')].find(
  item => item.textContent.includes('窗口标题备用'),
)
replacementTriggerItem?.click()
await new Promise(r => setTimeout(r, 50))
const saveDEnabled = await clickSaveRule()
const changedChild = savedRulesPayload?.[0]?.condition?.children?.at(-1)
const changeTypeOk = saveDEnabled
  && changedChild?.type === nextTriggerType
  && changedChild?.binding_id === addedChildId
console.log((changeTypeOk?'PASS':'FAIL')+' - changing trigger type keeps condition binding_id')
if (!changeTypeOk) {
  console.error({ saveDEnabled, addedChildId, changedChild, conditionCards: conditionCards.length, typeButton: typeButton?.textContent })
  process.exit(1)
}

document.querySelector('.classic-rule-editor .stage-then .flow-add-control button')?.click()
await new Promise(r => setTimeout(r, 20))
const classicActionUsesPicker = document.querySelector('.plugin-picker-dialog')?.textContent.includes('添加动作')
console.log((classicActionUsesPicker?'PASS':'FAIL')+' - classic action creation reuses the plugin picker')
if (!classicActionUsesPicker) process.exit(1)
document.querySelector('.plugin-picker-head .icon-btn')?.click()
await new Promise(r => setTimeout(r, 20))

// 两种编辑方式只改变界面，不应凭空把规则标记为已修改。
await new Promise(r => setTimeout(r, 100))
const cleanBeforeModeSwitch = !document.querySelector('.draft-state')
canvasModeButton?.click()
await new Promise(r => setTimeout(r, 30))
formModeButton?.click()
await new Promise(r => setTimeout(r, 30))
const modeSwitchPreservesDraft = cleanBeforeModeSwitch && !document.querySelector('.draft-state')
console.log((modeSwitchPreservesDraft?'PASS':'FAIL')+' - switching editor modes does not change rule data')
if (!modeSwitchPreservesDraft) process.exit(1)

// 控制流连线上的 + 可以把动作插进中间，并立即打开新节点设置。
canvasModeButton?.click()
await new Promise(r => setTimeout(r, 30))
const actionCountBeforeInsert = document.querySelectorAll('.graph-node-action').length
document.querySelector('.graph-node-add')?.click()
await new Promise(r => setTimeout(r, 30))
const flowEndAddOpensPicker = document.querySelector('.plugin-picker-dialog')?.textContent.includes('添加下一步')
console.log((flowEndAddOpensPicker?'PASS':'FAIL')+' - flow-end plus opens the action picker')
if (!flowEndAddOpensPicker) process.exit(1)
document.querySelector('.plugin-picker-head .icon-btn')?.click()
await new Promise(r => setTimeout(r, 20))
document.querySelector('.canvas-edge-add')?.click()
await new Promise(r => setTimeout(r, 30))
const pluginSearch = document.querySelector('.plugin-picker-search input')
pluginSearch.value = '不存在的插件'
pluginSearch.dispatchEvent(new window.Event('input', { bubbles: true }))
await new Promise(r => setTimeout(r, 20))
const emptySearchWorks = document.querySelectorAll('.plugin-picker-item').length === 0
pluginSearch.value = 'notify'
pluginSearch.dispatchEvent(new window.Event('input', { bubbles: true }))
await new Promise(r => setTimeout(r, 20))
const pluginPickerSearchesAndGroups = emptySearchWorks
  && document.querySelectorAll('.plugin-picker-item').length === 1
  && document.querySelectorAll('.plugin-picker-categories button').length >= 2
console.log((pluginPickerSearchesAndGroups?'PASS':'FAIL')+' - plugin picker supports search and categories')
if (!pluginPickerSearchesAndGroups) process.exit(1)
document.querySelector('.plugin-picker-item')?.click()
await new Promise(r => setTimeout(r, 30))
const insertsActionInFlow = document.querySelectorAll('.graph-node-action').length === actionCountBeforeInsert + 1
  && document.querySelector('.node-inspector')?.textContent.includes('动作 1')
console.log((insertsActionInFlow?'PASS':'FAIL')+' - control-flow plus inserts and opens an action')
if (!insertsActionInFlow) process.exit(1)
const insertedLayout = [...document.querySelectorAll('.graph-node')].map(node => ({
  left: Number.parseFloat(node.style.left), top: Number.parseFloat(node.style.top),
  height: Number.parseFloat(node.style.height),
}))
let insertedLayoutOk = true
for (let i = 0; i < insertedLayout.length; i += 1) {
  for (let j = i + 1; j < insertedLayout.length; j += 1) {
    const a = insertedLayout[i]
    const b = insertedLayout[j]
    if (Math.abs(a.left - b.left) >= 228) continue
    if (!(a.top + a.height + 24 <= b.top || b.top + b.height + 24 <= a.top)) insertedLayoutOk = false
  }
}
console.log((insertedLayoutOk?'PASS':'FAIL')+' - inserting an action keeps the graph separated')
if (!insertedLayoutOk) process.exit(1)
const insertedDelete = [...document.querySelectorAll('.node-inspector button')].find(
  button => button.textContent.includes('删除'),
)
insertedDelete?.click()
await new Promise(r => setTimeout(r, 30))
const deleteRelayoutOk = document.querySelectorAll('.graph-node-action').length === actionCountBeforeInsert
console.log((deleteRelayoutOk?'PASS':'FAIL')+' - deleting an inserted action relayouts the flow')
if (!deleteRelayoutOk) process.exit(1)

// D. requestTestContext：空 path（完整 payload）必须写入 trigger_payloads / event_payload
const { requestTestContext } = await import('../src/lib/bindings.js')
const ctxSchema = {
  triggers: { window_title: { name:'窗口标题检测', outputs:[{ name:'matched_title', label:'匹配标题', type:'string' }] } },
  actions: {},
}
const ctxRule = {
  condition: { op:'all', children:[{ binding_id:'t_full01', type:'window_title', params:{} }] },
  actions: [
    { binding_id:'a_1', type:'notify', params:{ message:{ $ref:{ scope:'trigger', node:'t_full01', path:[] } } } },
    { binding_id:'a_2', type:'notify', params:{ message:{ $ref:{ scope:'event', path:[] } } } },
  ],
}
const promptInputs = ['{"matched_title":"记事本 - 无标题"}', '{"type":"window_title","payload":{}}']
const testCtx = requestTestContext(ctxRule, ctxSchema, () => promptInputs.shift())
const fullPayloadOk = testCtx?.trigger_payloads?.t_full01?.matched_title === '记事本 - 无标题'
  && testCtx?.event_payload?.type === 'window_title'
console.log((fullPayloadOk?'PASS':'FAIL')+' - requestTestContext writes full payload for empty path')
if (!fullPayloadOk) process.exit(1)

const navHasEngineControl = !!document.querySelector('.nav-engine-ctl')
console.log((!navHasEngineControl?'PASS':'FAIL')+' - navigation has no duplicate engine controls')
if (navHasEngineControl) process.exit(1)

const securityNav = [...document.querySelectorAll('.nav-item')].find(
  button => button.textContent.includes('安全'),
)
securityNav?.click()
await new Promise(r => setTimeout(r, 50))
const permissionChipTexts = [...document.querySelectorAll('.perm-chips .chip')]
  .map(chip => chip.textContent.trim())
const permissionLabelsOk = permissionChipTexts.length > 0
  && permissionChipTexts.every(Boolean)
  && permissionChipTexts.includes('content_paste剪贴板')
const securitySpacingOk = !!document.querySelector('.config-security-status')
console.log((permissionLabelsOk?'PASS':'FAIL')+' - every registered permission renders a non-empty label')
if (!permissionLabelsOk) process.exit(1)
console.log((securitySpacingOk?'PASS':'FAIL')+' - config status keeps dedicated spacing from the security banner')
if (!securitySpacingOk) process.exit(1)

console.log('\nDashboard mount test: PASS')
process.exit(0)
