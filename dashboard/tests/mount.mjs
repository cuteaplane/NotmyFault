// Dashboard 挂载冒烟测试：在 jsdom 中加载构建产物，验证应用挂载与渲染。
// 运行：npm run build && npm test
import { JSDOM } from 'jsdom'
import fs from 'fs'
import path from 'path'
import { pathToFileURL } from 'url'
import { buildRunExport } from '../src/lib/runExport.js'
import { aiProviderIdFor } from '../src/lib/providers.js'
import { templateAvailability } from '../src/lib/automationTemplates.js'
import { streamRuleDraftWithAI } from '../src/lib/api.js'
import { computeChangeSet, summarizeRuleChanges } from '../src/lib/ruleDiff.js'
import { ensureParams } from '../src/lib/utils.js'
import { actionOutputDefs, bindingSourceFor, buildNodeDataPorts, collectReferences, parameterAllowsBinding, regenerateBindingIds } from '../src/lib/bindings.js'
import { compatibleTypes, formatTypedInput, isOpaqueValue, parseTypedInput, typeAtPath } from '../src/lib/valueTypes.js'

const bindingPolicyMeta = {
  params:[
    { name:'operation', type:'select' },
    { name:'source', type:'string' },
    { name:'destination', type:'string' },
  ],
  security:{ literal_only_params:['operation', 'destination'] },
}
const bindingPolicyNode = buildNodeDataPorts(
  { kind:'action', source:{ type:'file_operation' } },
  { triggers:{}, actions:{ file_operation:bindingPolicyMeta } },
)
const bindingPolicyOk = parameterAllowsBinding(bindingPolicyMeta, 'source')
  && !parameterAllowsBinding(bindingPolicyMeta, 'destination')
  && bindingPolicyNode.dataInputs.map(port => port.name).join('|') === 'source'
console.log((bindingPolicyOk?'PASS':'FAIL')+' - literal-only parameters stay out of graph binding ports')
if (!bindingPolicyOk) process.exit(1)

const copiedBranch = {
  type:'if', binding_id:'a_branch01', condition:{ op:'is_true', left:true },
  then:[
    { type:'query', binding_id:'a_query001', params:{} },
    { type:'notify', binding_id:'a_notify01', params:{ message:{ $ref:{ scope:'step', node:'a_query001', path:['text'] } } } },
  ], else:[],
}
regenerateBindingIds(copiedBranch, 'action')
const copiedBranchOk = copiedBranch.then[0].binding_id !== 'a_query001'
  && copiedBranch.then[1].params.message.$ref.node === copiedBranch.then[0].binding_id
console.log((copiedBranchOk?'PASS':'FAIL')+' - copying an IF branch keeps its internal data references')
if (!copiedBranchOk) process.exit(1)

const distDir = process.env.DASHBOARD_DIST_DIR || 'dist'
const dom = new JSDOM('<!DOCTYPE html><html><head></head><body><div id="app"></div></body></html>', {
  url: 'http://127.0.0.1:19199/', pretendToBeVisual: true,
})
const { window } = dom

// mock API + SSE（不依赖真实引擎）
const bridgeCalls = []
const windowStateCalls = []
let savedRulesPayload = null
let requireAdminRulePassword = false
const adminRulePasswordAttempts = []
const saveConfigCalls = []
let requireEarlyApproval = false
const earlyApprovalAttempts = []
const aiDraftCalls = []
const pluginInstallForms = []
let mockAiDraftResultType = 'assistant_message'
let mockAiDraftDelay = 0
let mockAiDraftError = ''
let mockAiStreamMode = 'normal'
let mockEngineRunning = true
let mockEngineState = 'running'
let engineStatusReads = 0
let configReads = 0
let slowStopPolls = 0
let simulateSlowStop = false
let launchCalls = 0
let mockAdminAuthorization = 'per_execution'
let mockAdminRuleVerification = true
let mockAiDrafting = { enabled: false, endpoint_url: '', model: '', api_format: 'chat_completions' }
let mockConfigSecurity = { status:'ok', reason:'', summary:null }
let mockAiApiKeyStatus = 'none'
let mockBluetoothInstalled = false
let exportedRunBlob = null
let exportedRunFileName = ''
const exportedFileNames = []
const textEncoder = new TextEncoder()
let engineEventController = null
let engineConnections = 0
let recoveredRun = null

function pause(ms) {
  return new Promise(resolve => setTimeout(resolve, ms))
}

async function waitFor(selector, timeout = 1000) {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    const element = typeof selector === 'function' ? selector() : window.document.querySelector(selector)
    if (element) return element
    await pause(10)
  }
  return null
}

function sseFrame(type, data) {
  return `event: ${type}\ndata: ${JSON.stringify(data)}\n\n`
}

function sseResponse(chunks) {
  let index = 0
  return {
    ok: true,
    status: 200,
    body: new ReadableStream({
      pull(controller) {
        if (index >= chunks.length) {
          controller.close()
          return
        }
        const chunk = chunks[index++]
        controller.enqueue(chunk instanceof Uint8Array ? chunk : textEncoder.encode(chunk))
      },
    }),
  }
}

function mockAiDraftResult() {
  if (mockAiDraftResultType === 'assistant_message') {
    return {
      ok:true, source:'ai', result_type:'assistant_message',
      message:'已检查的最终文案不应覆盖流式内容。',
    }
  }
  return {
    ok:true, source:'ai', result_type:'rule_draft',
    draft:{ name:'AI 候选草稿', folder:'未分类', event:{ type:'time_schedule', params:{ time:'09:00' } }, actions:[{ type:'notify', params:{ title:'NotmyFault', message:'AI 生成的提醒', secret:'ai-preview-secret' } }] },
    validation:{ valid:true, issues:[] },
  }
}

function mockAiStreamResponse() {
  if (mockAiDraftError) {
    return sseResponse([
      sseFrame('status', { status:'started' }),
      sseFrame('error', { code:'ai_provider_failed', error:mockAiDraftError }),
      sseFrame('done', { status:'done' }),
    ])
  }
  const result = mockAiDraftResult()
  const text = result.result_type === 'assistant_message'
    ? '**请补充**提醒的具体内容。'
    : result.result_type === 'rule_draft'
      ? '正在整理规则草稿。'
      : '正在整理结果。'
  const events = [
    ': keepalive\n\n',
    sseFrame('status', { status:'started' }),
    sseFrame('reasoning', { delta:'正在检查可用条件。' }),
    sseFrame('text', { delta:text }),
  ]
  if (result.result_type !== 'assistant_message') {
    events.push(
      sseFrame('progress', { phase:'drafting', received:64 }),
      sseFrame('progress', { phase:'validating', received:0 }),
    )
  }
  events.push(sseFrame('result', result), sseFrame('done', { status:'done' }))
  return sseResponse(events)
}
window.matchMedia = () => ({ matches: false, addEventListener(){}, removeEventListener(){} })
window.ResizeObserver = class {
  constructor(callback) { this.callback = callback }
  observe() { this.callback([{ contentRect: { width: 818, height: 640 } }]) }
  disconnect() {}
}
window.EventSource = class { constructor(){} addEventListener(){} close(){} }
window.URL.createObjectURL = blob => { exportedRunBlob = blob; return 'blob:notmyfault-run-export' }
window.URL.revokeObjectURL = () => {}
window.HTMLAnchorElement.prototype.click = function() {
  exportedRunFileName = this.download
  exportedFileNames.push(this.download)
}
// Dashboard 只支持 pywebview；提供完整的最小 bridge 契约。
window.pywebview = { api: {
  get_config: async () => {
    configReads++
    if (configReads === 1) throw new Error('bridge not ready')
    return { rules: [{
    rule_id: 'r_mount001',
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
    actions: [{ binding_id: 'a_mount001', type: 'notify', params: { message:'公开内容', secret:'draft-recovery-secret' } }],
    }] }
  },
  save_config: async (rules, adminKeyPassword = '') => {
    saveConfigCalls.push({ password: adminKeyPassword, rules: JSON.parse(JSON.stringify(rules)) })
    if (requireAdminRulePassword) {
      adminRulePasswordAttempts.push(adminKeyPassword)
      if (!adminKeyPassword) return {
        ok: false,
        code: 'admin_key_required',
        error: '请输入签名私钥密码',
        plugins: ['admin_action'],
      }
      if (adminKeyPassword !== 'dashboard-secret') return {
        ok: false,
        code: 'admin_key_invalid',
        error: '私钥密码错误',
        plugins: ['admin_action'],
      }
      requireAdminRulePassword = false
    }
    savedRulesPayload = JSON.parse(JSON.stringify(rules))
    return { ok: true }
  },
  get_api_token: async () => 'test-token',
  get_engine_status: async () => {
    engineStatusReads++
    if (engineStatusReads === 1) {
      return { api_alive:false, engine_running:false, engine_state:'offline' }
    }
    if (mockEngineState === 'stopping') {
      if (slowStopPolls > 0) slowStopPolls--
      else mockEngineState = 'stopped'
    }
    mockEngineRunning = mockEngineState === 'running'
    return { api_alive:true, engine_running:mockEngineRunning, engine_state:mockEngineState, security_mode:'permissive', last_error:'核心文件完整性校验失败', rules_count:1, triggers_count:1, actions_count:1, pid:1234 }
  },
  stop_engine: async () => {
    mockEngineRunning = false
    if (simulateSlowStop) {
      mockEngineState = 'stopping'
      slowStopPolls = 2
      return { ok:true, stopped:false, stopping:true }
    }
    mockEngineState = 'stopped'
    return { ok:true, stopped:true, stopping:false }
  },
  launch_engine: async () => {
    launchCalls++
    if (mockEngineState !== 'stopped') return { ok:false, error:'engine_stopping' }
    mockEngineState = 'running'
    mockEngineRunning = true
    return { ok:true, api_alive:true, engine_running:true, engine_state:'running' }
  },
  shutdown_engine: async () => ({ ok:true }),
  set_window_state: async (action) => {
    windowStateCalls.push(action)
    return { ok:true, action }
  },
  request_api: async (path, method, data) => {
    bridgeCalls.push({ path, method, data })
    if (path === '/api/config/security-status') return mockConfigSecurity
    if (path === '/api/platform') return {
      platform:'windows', session_type:'desktop', capabilities:{
        'audio.control':{ available:true, degraded:false, backend:'pycaw', reason:'' },
        'bluetooth.control':{ available:false, degraded:false, backend:'', reason:'未安装后端' },
      },
    }
    if (path === '/api/settings/ai-drafting' && method === 'GET') {
      return { ...mockAiDrafting, api_key_status: mockAiApiKeyStatus }
    }
    if (path === '/api/settings/ai-drafting' && method === 'PUT') {
      mockAiDrafting = {
        enabled: data.enabled === true,
        endpoint_url: typeof data.endpoint_url === 'string' ? data.endpoint_url : '',
        model: typeof data.model === 'string' ? data.model : '',
        api_format: data.api_format === 'responses' ? 'responses' : 'chat_completions',
      }
      return { ok:true, settings:mockAiDrafting }
    }
    if (path === '/api/settings/ai-drafting/api-key' && method === 'PUT') {
      if (typeof data?.api_key !== 'string' || !data.api_key) {
        return { ok:false, error:'API key 不能为空' }
      }
      mockAiApiKeyStatus = 'saved'
      return { ok:true, api_key_status:mockAiApiKeyStatus }
    }
    if (path === '/api/settings/ai-drafting/api-key' && method === 'DELETE') {
      mockAiApiKeyStatus = 'none'
      return { ok:true, api_key_status:mockAiApiKeyStatus }
    }
    if (path === '/api/rules/draft/ai' && method === 'POST') {
      aiDraftCalls.push(data)
      if (mockAiDraftDelay) await new Promise(resolve => setTimeout(resolve, mockAiDraftDelay))
      if (mockAiDraftError) return { ok:false, error:mockAiDraftError }
      return mockAiDraftResult()
    }
    if (path === '/api/rules/approve' && method === 'POST') {
      earlyApprovalAttempts.push(data.admin_key_password || '')
      if (requireEarlyApproval) {
        if (!data.admin_key_password) return { ok:false, code:'admin_key_required', error:'请输入签名私钥密码', plugins:['shutdown_system'] }
        if (data.admin_key_password !== 'dashboard-secret') return { ok:false, code:'admin_key_invalid', error:'私钥密码错误', plugins:['shutdown_system'] }
        requireEarlyApproval = false
      }
      return { ok:true }
    }
    if (path === '/api/rules/validate') return { ok:true, valid:true, issues:[], summary:{ errors:0, warnings:0 } }
    if (/^\/api\/rules\/\d+\/run$/.test(path) && method === 'POST') return { ok:true, action_count:1, run_id:'run_test001' }
    if (path === '/api/runs/run_test001/cancel' && method === 'POST') return { ok:true, message:'已请求停止这次运行' }
    if (path.startsWith('/api/runs?')) return { runs:[{
      run_id:'run_mount001', rule_id:'r_mount001', rule_name:'挂载测试规则', event_type:'manual',
      status:'failed', started_at:1723000000, finished_at:1723000000.2,
      duration_ms:200, action_count:1, replayable:false,
      error:{ code:'test_failure', message:'测试动作失败' },
      steps:[{
        step_id:'a_mount001', action_type:'notify', status:'failed', duration_ms:180, attempt:1, error:'测试动作失败',
        input_summary:[{ name:'message', label:'消息', type:'string', display:'文本 · 12 字符', redacted:false }],
        output_summary:[{ name:'delivered', label:'已送达', type:'bool', display:'布尔值', redacted:false }],
      }],
    }] }
    if (path === '/api/runs/run_test001') return recoveredRun || { run_id:'run_test001', status:'running' }
    if (path === '/api/plugins/toggle') return { ok:true, restart_required:true }
    if (path === '/api/plugins/key-status') return { exists:true, encrypted:true }
    if (path === '/api/plugins/extensions') return {
      commands: [],
      parameter_editors: [
        {
          plugin_id:'hotkey', id:'hotkey_recorder', parameter:'hotkey', value_type:'string',
          command:'capture_hotkey',
          ui:{ control:'button', label:'录制', busy_label:'请按快捷键…', icon:'keyboard' },
        },
        {
          plugin_id:'macro_run', id:'macro_editor', parameter:'macro', data_type:'mouse_macro',
          command:'open_macro', view:'macro_workbench',
          ui:{ control:'button', empty_label:'录制操作宏', icon:'movie', description:'由插件管理' },
        },
      ],
      views: [{ plugin_id:'macro_run', id:'macro_workbench', title:'操作宏编辑器', window_controls:['minimize','restore'] }],
      data_types: [{ plugin_id:'macro_run', id:'mouse_macro', version:1, binding:'private' }],
    }
    if (path === '/api/plugins/macro_run/extensions/views/macro_workbench/page') {
      return { ok:true, html:'<h1>操作宏插件页面</h1>' }
    }
    if (path === '/api/plugins/macro_run/extensions/commands/open_macro/invoke' && method === 'POST') {
      if (data.current_value != null && data.current_value?.$type !== 'com.test.macro/mouse_macro@1') {
        return { ok:false, error:'当前值不是该插件声明的数据' }
      }
      const current = data.current_value?.data || data.current_value || {}
      return { ok:true, session_id:'s_macro', view:'macro_workbench', state:{ steps:current.steps || [] }, close:false }
    }
    if (path === '/api/plugins/macro_run/extensions/commands/start_recording/invoke' && method === 'POST') {
      return { ok:true, session_id:'s_macro', close:false, data:{
        recording:true, event_count:0, elapsed_seconds:0, stop_hotkey:'Ctrl+Shift+F10',
        ...(data.payload?.minimize_window ? { window_action:'minimize' } : {}),
      } }
    }
    if (path === '/api/plugins/macro_run/extensions/commands/commit_macro/invoke' && method === 'POST') {
      const steps = Array.isArray(data.payload?.steps) ? data.payload.steps : []
      return {
        ok:true, session_id:'s_macro', close:true,
        value:{
          '$type':'com.test.macro/mouse_macro@1',
          summary:`1 个操作宏 · ${steps.length} 步`,
          data:{ version:1, steps },
        },
      }
    }
    if (path === '/api/plugins/macro_run/extensions/sessions/s_macro' && method === 'DELETE') {
      return { ok:true }
    }
    if (path === '/api/plugins/hotkey/extensions/commands/capture_hotkey/invoke' && method === 'POST') {
      return { ok:true, session_id:'s_hot', value:'Ctrl+Shift+M', close:true }
    }
    if (path.includes('/api/plugins/list')) return { triggers: T, actions: A }
    if (path.includes('/api/plugins')) return { triggers: T, actions: A }
    return { ok:true }
  },
} }
const T = {
  window_title: { id:'window_title', name:'窗口标题检测', description:'d', origin:'builtin', enabled:true, version_code:1, permissions:['native_api','admin'], params:[], outputs:[{ name:'matched_title', label:'匹配标题', type:'string', sensitive:true }, { name:'state', label:'窗口状态', type:'string' }] },
  window_title_alt: { id:'window_title_alt', name:'窗口标题备用', description:'d', origin:'builtin', enabled:true, version_code:1, permissions:['native_api'], params:[], outputs:[{ name:'matched_title', label:'匹配标题', type:'string', sensitive:true }] },
  time_schedule: { id:'time_schedule', name:'定时', description:'d', origin:'builtin', enabled:true, version_code:1, permissions:[], params:[], outputs:[] },
  clipboard: { id:'clipboard', name:'剪贴板监控', description:'d', origin:'builtin', enabled:true, version_code:1, permissions:['clipboard','native_api'], params:[], outputs:[] },
}
const A = {
  notify: { id:'notify', name:'显示通知', description:'d', origin:'builtin', enabled:true, version_code:1, permissions:['admin'], params:[{ name:'message', label:'消息', type:'string', default:'' }, { name:'secret', label:'密钥', type:'string', default:'', sensitive:true }], outputs:[{ name:'delivered', label:'已送达', type:'bool' }] },
  shutdown_system: { id:'shutdown_system', name:'关闭电脑', description:'d', origin:'builtin', enabled:true, version_code:1, permissions:['admin'], params:[], outputs:[] },
  macro_run: { id:'macro_run', name:'宏录制动作', description:'d', origin:'builtin', enabled:true, version_code:1, permissions:[], params:[
    { name:'macro', label:'操作宏', type:'plugin_data', data_type:'mouse_macro', value_type:'object', summary:'hidden' },
  ], outputs:[] },
}
window.fetch = async (url, options = {}) => {
  const u = String(url); const j = (o) => ({ json: async () => o, ok: true, status: 200 })
  if (u.endsWith('/api/events')) {
    engineConnections++
    return {
      ok: true,
      status: 200,
      body: new ReadableStream({
        start(controller) {
          engineEventController = controller
          options.signal?.addEventListener('abort', () => controller.error(new DOMException('已停止', 'AbortError')), { once:true })
        },
        cancel() { engineEventController = null },
      }),
    }
  }
  if (u.includes('/api/rules/draft/ai/stream')) {
    aiDraftCalls.push(JSON.parse(options.body || '{}'))
    if (mockAiDraftDelay) await pause(mockAiDraftDelay)
    if (mockAiStreamMode === 'stalled') {
      return await new Promise((_resolve, reject) => {
        options.signal?.addEventListener('abort', () => {
          reject(new DOMException('已停止', 'AbortError'))
        }, { once:true })
      })
    }
    return mockAiStreamResponse()
  }
  if (u.endsWith('/api/plugins/preview')) return j({
    ok:true,
    preview_token:'preview-author-signed',
    plugin:{
      id:'author_signed', name:'作者自签插件', description:'测试副签密码字段',
      version:'1.0', version_code:1, package_name:'com.test.author_signed',
      type:'actions', platform_compatible:true,
    },
    permissions:[], permission_conform:true, permission_errors:[], risks:[],
    schema_valid:true, schema_errors:[], update_diff:{ update:{ kind:'new' } },
  })
  if (u.endsWith('/api/plugins/install')) {
    pluginInstallForms.push(Object.fromEntries(options.body.entries()))
    return j({ ok:true, id:'author_signed', restart_required:true })
  }
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
const appendToHead = window.document.head.appendChild.bind(window.document.head)
window.document.head.appendChild = element => {
  const result = appendToHead(element)
  if (element.tagName === 'LINK' && element.rel === 'stylesheet') {
    queueMicrotask(() => element.dispatchEvent(new window.Event('load')))
  }
  return result
}

async function checkDraftStreamParser() {
  const originalFetch = window.fetch
  const originalToken = window.pywebview.api.get_api_token
  const bridgeCount = bridgeCalls.length
  const calls = []
  let tokenReads = 0
  const received = []
  const streamText = ': keepalive\r\n\r\nevent: status\r\ndata: {"status":"started"}\r\n\r\nevent: progress\r\ndata: {"phase":"drafting","received":128}\r\n\r\nevent: text\r\ndata: {"delta":"跨块中文"}\r\n\r\nevent: done\r\ndata: {"status":"done"}\r\n\r\n'
  const bytes = textEncoder.encode(streamText)
  const splitAt = textEncoder.encode(': keepalive\r\n\r\nevent: status\r\ndata: {"status":"started"}\r\n\r\nevent: progress\r\ndata: {"phase":"drafting","received":128}\r\n\r\nevent: text\r\ndata: {"delta":"').length + 1
  window.pywebview.api.get_api_token = async () => `stream-token-${++tokenReads}`
  window.fetch = async (_url, options = {}) => {
    calls.push(options)
    if (calls.length === 1) return { ok:false, status:403, json:async () => ({ error:'token stale' }) }
    return sseResponse([
      bytes.slice(0, splitAt),
      bytes.slice(splitAt, splitAt + 7),
      bytes.slice(splitAt + 7),
    ])
  }
  global.fetch = window.fetch
  try {
    await streamRuleDraftWithAI([{ role:'user', content:'测试流' }], {
      signal: new AbortController().signal,
      onEvent: event => received.push(event),
    })
  } finally {
    window.fetch = originalFetch
    window.pywebview.api.get_api_token = originalToken
    global.fetch = originalFetch
  }
  return calls.length === 2
    && tokenReads === 2
    && calls[1]?.headers?.Authorization === 'Bearer stream-token-2'
    && received.map(event => event.type).join('|') === 'status|progress|text|done'
    && received[1]?.data?.phase === 'drafting'
    && received[1]?.data?.received === 128
    && received[2]?.data?.delta === '跨块中文'
    && bridgeCalls.length === bridgeCount
}

const draftStreamParserOk = await checkDraftStreamParser()
console.log((draftStreamParserOk ? 'PASS' : 'FAIL') + ' - AI stream retries preflight auth and parses partial UTF-8 SSE frames')
if (!draftStreamParserOk) process.exit(1)

const safeExportProbe = buildRunExport([{
  run_id:'run_export001', rule_name:'导出测试', event_type:'manual', status:'succeeded',
  event_payload:{ secret:'不能导出' },
  steps:[{
    step_id:'a_export001', action_type:'notify', status:'succeeded', params:{ secret:'不能导出' },
    input_summary:[{ name:'message', label:'消息', type:'string', display:'文本 · 4 字符', redacted:false, raw:'不能导出' }],
  }],
}], { status:'all', timeRange:'7d', actionType:'notify', search:'' }, new Date('2026-08-10T00:00:00Z'))
const safeExportText = JSON.stringify(safeExportProbe)
const safeRunExportOk = safeExportProbe.format === 'NotmyFault run diagnostics'
  && safeExportProbe.filters.time_range === '7d'
  && safeExportProbe.runs[0].duration_ms === null
  && safeExportProbe.runs[0].steps[0].input_summary[0].display === '文本 · 4 字符'
  && !safeExportText.includes('不能导出')
  && !safeExportText.includes('event_payload')
  && !safeExportText.includes('params')
console.log((safeRunExportOk?'PASS':'FAIL')+' - run export keeps only redacted diagnostic fields')
if (!safeRunExportOk) process.exit(1)

const missingParamsNode = { type:'notify' }
const ensuredParamsOk = ensureParams(missingParamsNode) === missingParamsNode.params
  && Object.keys(missingParamsNode.params).length === 0
const beforeRecovery = {
  actions: [{
    binding_id: 'a_main001',
    type: 'notify',
    params: {},
    failure_actions: [{
      binding_id: 'a_recover1',
      type: 'notify',
      params: { message:'旧值' },
    }],
  }],
}
const afterRecovery = {
  actions: [{
    binding_id: 'a_main001',
    type: 'notify',
    params: {},
    failure_actions: [{
      binding_id: 'a_recover1',
      type: 'notify',
      params: { message:'新值' },
    }],
  }],
}
const recoveryDiff = computeChangeSet(beforeRecovery, afterRecovery, { actions:{ notify:{ params:[{ name:'message', label:'消息' }] } } })
const recoveryDiffOk = recoveryDiff.length === 1
  && recoveryDiff[0].target === 'failure-action'
  && recoveryDiff[0].detail.includes('旧值 → 新值')
  && summarizeRuleChanges(beforeRecovery, afterRecovery).some(item => item.includes('补救动作'))
console.log((ensuredParamsOk && recoveryDiffOk?'PASS':'FAIL')+' - missing params and recovery changes stay editable and visible')
if (!ensuredParamsOk || !recoveryDiffOk) process.exit(1)

const builtHtml = fs.readFileSync(path.join(distDir, 'index.html'), 'utf8')
const entryPath = builtHtml.match(/<script[^>]*type="module"[^>]*src="([^"]+)"/)?.[1]
if (!entryPath) throw new Error('Dashboard 构建结果缺少入口脚本')
await import(new URL(entryPath, pathToFileURL(path.resolve(distDir, 'index.html'))).href)
await new Promise(r => setTimeout(r, 100))

const offlineHtml = document.getElementById('app').innerHTML
const offlineStateOk = offlineHtml.includes('引擎未运行')
  && offlineHtml.includes('启动引擎')
  && !offlineHtml.includes('正在连接后台服务')
console.log((offlineStateOk?'PASS':'FAIL')+' - offline home shows the final state without a connection spinner')
if (!offlineStateOk) process.exit(1)

window.dispatchEvent(new window.Event('pywebviewready'))
await new Promise(r => setTimeout(r, 100))
const html = document.getElementById('app').innerHTML
const checks = [
  ['engine running', html.includes('运行中')],
]
const startupRetryOk = engineStatusReads >= 2 && configReads >= 2
  && document.querySelector('.home-engine-metrics')?.textContent.includes('1')
  && !document.querySelector('.dashboard-first-run')
const homeKeepsCapabilitiesOutOfDiagnostics = !document.querySelector('.dashboard-capability-report')
checks.push(['bridge readiness restores engine state without waiting for polling', startupRetryOk])
checks.push(['home diagnostics omit the full platform capability list', homeKeepsCapabilitiesOutOfDiagnostics])
let ok = true
for (const [name, pass] of checks) { console.log((pass?'PASS':'FAIL')+' - '+name); if(!pass) ok=false }
if (!ok) { console.error(html.substring(0, 600)); process.exit(1) }

const homeNav = [...document.querySelectorAll('.nav-item')].find(
  button => button.textContent.includes('首页'),
)
const pauseAutomation = [...document.querySelectorAll('button')].find(
  button => button.textContent.trim() === 'pause暂停',
)
pauseAutomation?.click()
await new Promise(r => setTimeout(r, 50))
const pausedControls = [...document.querySelectorAll('.home-engine-actions .btn')]
const pausedControlsOk = pausedControls.length === 2
  && pausedControls.some(button => button.textContent.includes('启动自动化'))
  && pausedControls.some(button => button.textContent.includes('彻底停止'))
const startupAlertOk = document.querySelector('.engine-startup-alert')?.textContent.includes('安装文件异常，请重新安装 NotmyFault')
  && document.querySelector('.engine-startup-alert .btn')?.textContent.includes('查看重新安装说明')
console.log((pausedControlsOk && startupAlertOk?'PASS':'FAIL')+' - paused automation keeps controls and startup failure in separate layouts')
if (!pausedControlsOk || !startupAlertOk) process.exit(1)

const rulesNav = [...document.querySelectorAll('.nav-item')].find(
  button => button.textContent.includes('自动化'),
)
rulesNav?.click()
await waitFor('.rules-library')
const automationPageOk = document.querySelector('.rules-library')?.textContent.includes('创建、测试和管理这台电脑上的自动化')
  && !document.querySelector('.automation-create-panel')
;[...document.querySelectorAll('.rules-library button')].find(button => button.textContent.includes('创建自动化'))?.click()
await new Promise(r => setTimeout(r, 40))
const disabledAiCreateOk = document.querySelector('.rule-title-capsule')?.textContent.includes('新规则')
  && !document.querySelector('.automation-create-panel')
  && !document.querySelector('#natural-draft-description')
  && !document.querySelector('.automation-template')
const compactValidation = document.querySelector('.flow-validation')
const compactValidationOk = compactValidation
  && !compactValidation.classList.contains('expanded')
  && !compactValidation.querySelector('.flow-validation-list')
  && !!compactValidation.querySelector('.flow-validation-preview')
compactValidation?.querySelector('.flow-validation-toggle')?.click()
await new Promise(r => setTimeout(r, 20))
const validationExpandsOnDemand = document.querySelector('.flow-validation.expanded .flow-validation-list')
console.log((automationPageOk && disabledAiCreateOk?'PASS':'FAIL')+' - AI-off creation opens a blank editor without draft surfaces')
console.log((compactValidationOk && validationExpandsOnDemand?'PASS':'FAIL')+' - rule validation stays compact until expanded')
if (!automationPageOk || !disabledAiCreateOk || !compactValidationOk || !validationExpandsOnDemand) process.exit(1)
document.querySelector('.rule-back-btn')?.click()
await new Promise(r => setTimeout(r, 50))
const earlySettingsNav = [...document.querySelectorAll('.nav-item')].find(
  button => button.textContent.includes('设置'),
)
earlySettingsNav?.click()
await waitFor('.settings-root-list')
;[...document.querySelectorAll('.settings-root-list button')].find(button => button.textContent.includes('AI 功能'))?.click()
await new Promise(r => setTimeout(r, 20))
const earlyAiEnabled = document.querySelector('.ai-drafting-settings input[type="checkbox"]')
earlyAiEnabled?.click()
await new Promise(r => setTimeout(r, 50))
rulesNav?.click()
await new Promise(r => setTimeout(r, 50))
;[...document.querySelectorAll('.rules-library button')].find(button => button.textContent.includes('创建自动化'))?.click()
await new Promise(r => setTimeout(r, 40))
const automationTemplatesOk = [...document.querySelectorAll('.automation-template')].some(
  button => button.textContent.includes('U盘插入后备份文件'),
) && [...document.querySelectorAll('.automation-template')].some(
  button => button.textContent.includes('每天固定时间提醒我') && button.textContent.includes('可以直接使用'),
) && !document.querySelector('.natural-draft-panel')
  && !document.querySelector('#natural-draft-description')
  && !templateAvailability({ triggerType:'blocked', actionTypes:[] }, {
    schema:{ triggers:{ blocked:{ availability:'unavailable', unavailable_reasons:['缺少设备'] } } },
  }).available
console.log((automationTemplatesOk?'PASS':'FAIL')+' - AI-on creation keeps templates without the legacy draft card')
if (!automationTemplatesOk) process.exit(1)

const openQuickCreate = () => [...document.querySelectorAll('.automation-create-panel button')].find(
  button => button.textContent.includes('自己搭一个'),
)
const openBlankRule = () => [...document.querySelectorAll('.automation-create-panel button')].find(
  button => button.textContent.includes('空白规则'),
)
openQuickCreate()?.click()
await new Promise(r => setTimeout(r, 100))
;[...document.querySelectorAll('.quick-create-option')].find(button => button.textContent.includes('定时'))?.click()
await new Promise(r => setTimeout(r, 30))
const quickCreateSecondStepOk = document.querySelector('.quick-create-dialog')?.textContent.includes('接着要做什么')
  && document.querySelector('.quick-create-sentence')?.textContent.includes('定时')
;[...document.querySelectorAll('.quick-create-option')].find(button => button.textContent.includes('显示通知'))?.click()
await new Promise(r => setTimeout(r, 30))
;[...document.querySelectorAll('.quick-create-foot button')].find(button => button.textContent.includes('在编辑器中继续'))?.click()
await new Promise(r => setTimeout(r, 80))
const quickCreateDraftOk = document.querySelector('.rule-title-capsule')?.textContent.includes('定时后显示通知')
  && document.querySelector('.graph-node-trigger')?.textContent.includes('定时')
  && document.querySelector('.graph-node-action')?.textContent.includes('显示通知')
console.log((quickCreateSecondStepOk && quickCreateDraftOk?'PASS':'FAIL')+' - automation page opens a real prefilled rule draft')
if (!quickCreateSecondStepOk || !quickCreateDraftOk) process.exit(1)
document.querySelector('.rule-back-btn')?.click()
await new Promise(r => setTimeout(r, 50))
document.querySelector('.rule-library-row')?.click()
await new Promise(r => setTimeout(r, 50))
document.querySelector('.rule-title-edit')?.click()
await new Promise(r => setTimeout(r, 20))
const ruleNameInput = document.querySelector('.rule-title-capsule input')
ruleNameInput.value = '临时规则名称'
ruleNameInput.dispatchEvent(new window.Event('input', { bubbles: true }))
await new Promise(r => setTimeout(r, 350))
const draftRecovery = JSON.parse(window.localStorage.getItem('notmyfault.ruleDraft.v1') || 'null')
const draftRecoveryStored = /^r_[a-z0-9_]{6,64}$/.test(draftRecovery?.ruleId || '')
  && !Object.prototype.hasOwnProperty.call(draftRecovery || {}, 'ruleIndex')
  && !JSON.stringify(draftRecovery).includes('draft-recovery-secret')
  && !Object.prototype.hasOwnProperty.call(draftRecovery?.draft?.actions?.[0]?.params || {}, 'secret')
const undoDraftButton = document.querySelector('.rule-history-actions button:first-child')
undoDraftButton?.click()
await new Promise(r => setTimeout(r, 80))
const undoDraftOk = document.querySelector('.rule-title-capsule input')?.value === '挂载测试规则'
const draftRecoveryCleared = !window.localStorage.getItem('notmyfault.ruleDraft.v1')
console.log((undoDraftOk?'PASS':'FAIL')+' - rule draft supports undo history')
if (!undoDraftOk) process.exit(1)
console.log((draftRecoveryStored && draftRecoveryCleared?'PASS':'FAIL')+' - unsaved draft recovery follows the history state')
if (!draftRecoveryStored || !draftRecoveryCleared) process.exit(1)
window.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
await new Promise(r => setTimeout(r, 20))

document.querySelector('.graph-node-trigger .graph-node-data-summary button:last-child')?.click()
await new Promise(r => setTimeout(r, 20))
const outputPort = document.querySelector('.graph-node-trigger .data-port-column-output .data-port-dot')
outputPort?.dispatchEvent(new window.MouseEvent('pointerdown', {
  bubbles: true, button: 0, clientX: 80, clientY: 160,
}))
await new Promise(r => setTimeout(r, 20))
document.querySelector('.graph-node-action .data-port-column-input .data-port-row')?.dispatchEvent(
  new window.MouseEvent('pointerup', { bubbles: true, button: 0, clientX: 330, clientY: 160 }),
)
await new Promise(r => setTimeout(r, 20))

const saveRun = [...document.querySelectorAll('button')].find(
  button => button.textContent.includes('测试规则'),
)
requireAdminRulePassword = true
saveRun?.click()
await new Promise(r => setTimeout(r, 30))
let adminPasswordInput = document.querySelector('.app-dialog-password')
if (adminPasswordInput) {
  adminPasswordInput.value = 'wrong-secret'
  adminPasswordInput.dispatchEvent(new window.Event('input', { bubbles: true }))
  await new Promise(r => setTimeout(r, 10))
  document.querySelector('.app-dialog .btn-filled')?.click()
}
await new Promise(r => setTimeout(r, 30))
const rejectedPasswordVisible = document.querySelector('.app-dialog .test-field-error')
  ?.textContent.includes('私钥密码错误')
adminPasswordInput = document.querySelector('.app-dialog-password')
if (adminPasswordInput) {
  adminPasswordInput.value = 'dashboard-secret'
  adminPasswordInput.dispatchEvent(new window.Event('input', { bubbles: true }))
  await new Promise(r => setTimeout(r, 10))
  document.querySelector('.app-dialog .btn-filled')?.click()
}
await new Promise(r => setTimeout(r, 100))
const adminPasswordGateOk = rejectedPasswordVisible
  && adminRulePasswordAttempts.join('|') === '|wrong-secret|dashboard-secret'
console.log((adminPasswordGateOk?'PASS':'FAIL')+' - strict admin rule save asks Dashboard for the private-key password')
if (!adminPasswordGateOk) {
  console.error(JSON.stringify({ rejectedPasswordVisible, adminRulePasswordAttempts }))
  console.error(document.querySelector('.app-dialog')?.textContent)
  process.exit(1)
}
const testPrepDialog = document.querySelector('.test-prep-dialog')
const testPrepShowsRoute = testPrepDialog?.textContent.includes('模拟触发数据')
  && testPrepDialog?.textContent.includes('运行当前规则')
  && testPrepDialog?.textContent.includes('查看每步结果')
  && testPrepDialog?.textContent.includes('选择执行范围')
  && testPrepDialog?.textContent.includes('结果检查')
const sensitiveTestInput = testPrepDialog?.querySelector('input[type="password"]')
const reusableTestInput = testPrepDialog?.querySelector('input[type="text"]')
if (sensitiveTestInput) {
  sensitiveTestInput.value = '测试敏感标题'
  sensitiveTestInput.dispatchEvent(new window.Event('input', { bubbles: true }))
}
if (reusableTestInput) {
  reusableTestInput.value = 'opened'
  reusableTestInput.dispatchEvent(new window.Event('input', { bubbles: true }))
}
const rangeModeSelect = testPrepDialog?.querySelector('.test-range-controls select')
if (rangeModeSelect) {
  rangeModeSelect.value = 'from'
  rangeModeSelect.dispatchEvent(new window.Event('change', { bubbles: true }))
}
await new Promise(r => setTimeout(r, 20))
;[...(testPrepDialog?.querySelectorAll('button') || [])].find(
  button => button.textContent.includes('添加检查项'),
)?.click()
await new Promise(r => setTimeout(r, 20))
const assertionExpected = testPrepDialog?.querySelector('.test-assertion-row input')
if (assertionExpected) {
  assertionExpected.value = 'true'
  assertionExpected.dispatchEvent(new window.Event('input', { bubbles: true }))
}
;[...(testPrepDialog?.querySelectorAll('button') || [])].find(
  button => button.textContent.includes('用这些数据运行'),
)?.click()
await new Promise(r => setTimeout(r, 100))
const savedTestData = window.localStorage.getItem('notmyfault.ruleTestData.v1') || ''
const testPrepSafe = testPrepShowsRoute && !!sensitiveTestInput
  && !savedTestData.includes('测试敏感标题')
  && savedTestData.includes('opened')
console.log((testPrepSafe?'PASS':'FAIL')+' - test preparation groups data and never persists sensitive values')
if (!testPrepSafe) process.exit(1)
const stopTestButton = [...document.querySelectorAll('.test-result-dialog button')].find(
  button => button.textContent.includes('停止测试'),
)
stopTestButton?.click()
await new Promise(r => setTimeout(r, 30))
const testRunCanStop = !!stopTestButton && bridgeCalls.some(call =>
  call.path === '/api/runs/run_test001/cancel' && call.method === 'POST'
)
console.log((testRunCanStop?'PASS':'FAIL')+' - a running manual test can be stopped')
if (!testRunCanStop) process.exit(1)
const manualTestStayedLocked = [...document.querySelectorAll('button')].find(
  button => button.textContent.includes('测试中'),
)?.disabled === true
recoveredRun = { run_id:'run_test001', status:'cancelled' }
engineEventController?.close()
await waitFor(() => [...document.querySelectorAll('button')].find(
  button => button.textContent.includes('测试规则') && !button.disabled,
), 2000)
const manualTestUnlockedAtTerminal = [...document.querySelectorAll('button')].find(
  button => button.textContent.includes('测试规则'),
)?.disabled === false
console.log((manualTestStayedLocked && manualTestUnlockedAtTerminal?'PASS':'FAIL')+' - manual test recovers its terminal status after an SSE disconnect')
if (!manualTestStayedLocked || !manualTestUnlockedAtTerminal) {
  console.error(JSON.stringify({ manualTestStayedLocked, manualTestUnlockedAtTerminal, engineConnections, runReads:bridgeCalls.filter(call => call.path === '/api/runs/run_test001') }))
  process.exit(1)
}
;[...document.querySelectorAll('.test-result-dialog button')].find(
  button => button.textContent.includes('关闭'),
)?.click()
await new Promise(r => setTimeout(r, 20))
saveRun?.click()
await new Promise(r => setTimeout(r, 100))
const restoredTestDialog = document.querySelector('.test-prep-dialog')
const testDataRestored = restoredTestDialog?.querySelector('input[type="text"]')?.value === 'opened'
  && restoredTestDialog?.querySelector('input[type="password"]')?.value === ''
console.log((testDataRestored?'PASS':'FAIL')+' - test preparation restores only non-sensitive values')
if (!testDataRestored) process.exit(1)
;[...restoredTestDialog.querySelectorAll('button')].find(
  button => button.textContent.includes('取消'),
)?.click()
await new Promise(r => setTimeout(r, 20))
const snapshotCall = bridgeCalls.find(call => call.path === '/api/rules/0/run')
const snapshotBinding = snapshotCall?.data?.rule?.actions?.[0]?.params?.message?.$ref
const snapshotOk = snapshotCall?.data?.rule?.name === '挂载测试规则'
  && /^r_[a-z0-9_]{6,64}$/.test(snapshotCall?.data?.rule?.rule_id || '')
  && snapshotBinding?.scope === 'trigger'
  && snapshotBinding?.path?.[0] === 'matched_title'
  && Object.values(snapshotCall?.data?.trigger_payloads || {}).some(payload => payload.matched_title === '测试敏感标题')
  && snapshotCall?.data?.start_step_id === 'a_mount001'
  && snapshotCall?.data?.test_assertions?.[0]?.step_id === 'a_mount001'
  && snapshotCall?.data?.test_assertions?.[0]?.expected === true
console.log((snapshotOk?'PASS':'FAIL')+' - test-rule sends exact saved rule snapshot')
if (!snapshotOk) process.exit(1)

// D. requestTestContext：空 path（完整 payload）必须写入 trigger_payloads / event_payload
const { buildAllTestInputFields, buildPreparedTestContext, buildTestInputFields, requestTestContext } = await import('../src/lib/bindings.js')
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

const preparedContext = buildPreparedTestContext([
  { key:'count', scope:'event', node:'', path:['count'], type:'number', required:true },
  { key:'ready', scope:'event', node:'', path:['ready'], type:'bool', required:true },
  { key:'meta', scope:'trigger', node:'t_full01', path:['meta'], type:'object', required:true },
  { key:'precise', scope:'variable', node:'v_precise01', path:[], valueType:'int', required:true },
  { key:'nested', scope:'step', node:'a_nested01', path:['rows', 0, '名称'], valueType:'text', required:true },
], { count:'12.5', ready:true, meta:'{"source":"test"}', precise:'9007199254740993', nested:'条目' })
let unsafeContextRejected = false
try {
  buildPreparedTestContext([{ key:'unsafe', scope:'event', path:['__proto__', 'polluted'], type:'string' }], { unsafe:'yes' })
} catch { unsafeContextRejected = true }
const encodedInputCases = [
  ['int', 'int', '9007199254740993'],
  ['decimal', 'decimal', '9007199254740993.12345'],
  ['bytes', 'bytes', 'AAEC'],
  ['date', 'date', '2026-09-06'],
  ['time', 'time', '12:34:56+08:00'],
  ['datetime', 'datetime', '2026-09-06T12:34:56+08:00'],
  ['uuid', 'uuid', 'e7cc3f52-319b-4cbb-8c6b-cd8ec2e18c13'],
  ['path', 'windows_path', 'C:\\data\\example.txt'],
  ['path', 'posix_path', '/data/example.txt'],
]
const encodedInputsOk = encodedInputCases.every(([declaration, kind, data]) => {
  const value = { $nmf_value:{ type:kind, data } }
  const specialized = parseTypedInput(formatTypedInput(value, declaration), declaration)
  const expected = ['int', 'decimal', 'bytes'].includes(declaration) ? value : data
  return JSON.stringify(specialized) === JSON.stringify(expected)
    && ['any', { type:'union', variants:[declaration, 'null'] }].every(type => (
      JSON.stringify(parseTypedInput(formatTypedInput(value, type), type)) === JSON.stringify(value)
    ))
})
const ordinaryReference = { $ref:{ scope:'trigger', node:'t_full01', path:['matched_title'] } }
const ordinaryObjects = [
  { $literal:'business label', content:ordinaryReference },
  { $nmf_value:'business label', content:ordinaryReference },
  { $type:'business/name', data:ordinaryReference },
  { $type:'io.example.plugin/record@1', content:ordinaryReference },
]
const ordinaryObjectsOk = ordinaryObjects.every(value => (
  !isOpaqueValue(value) && collectReferences(value).length === 1
  && JSON.stringify(parseTypedInput(formatTypedInput(value, 'any'), 'any')) === JSON.stringify(value)
  && collectReferences({ $literal:value }).length === 0
)) && collectReferences({ $type:'io.example.plugin/record@1', data:ordinaryReference }).length === 0
const preparedTypesOk = preparedContext.event_payload.count === 12.5
  && preparedContext.event_payload.ready === true
  && preparedContext.trigger_payloads.t_full01.meta.source === 'test'
  && preparedContext.variable_values.v_precise01.$nmf_value.data === '9007199254740993'
  && preparedContext.step_outputs.a_nested01.rows[0]['名称'] === '条目'
  && unsafeContextRejected && !({}).polluted && encodedInputsOk && ordinaryObjectsOk
console.log((preparedTypesOk?'PASS':'FAIL')+' - prepared test data follows output types and expands full payload references')
if (!preparedTypesOk) process.exit(1)

const objectCompatibilityCases = [
  [{ type:'object', additional_properties:'text' }, { type:'object', properties:{ count:'int' } }, false],
  [{ type:'object', additional_properties:'int' }, { type:'object', properties:{ count:'int' } }, true],
  [{ type:'object', additional_properties:'int' }, { type:'object', additional_properties:'text' }, false],
  [{ type:'object', properties:{ name:'text' }, additional_properties:false }, { type:'object', additional_properties:'int' }, false],
  [{ type:'object', additional_properties:false }, { type:'object', additional_properties:'int' }, true],
  [{ type:'object' }, { type:'object', additional_properties:false }, false],
]
const sharedIdentity = 'io.example.plugin/record@1'
const sharedCatalog = [{ id:sharedIdentity, binding:'shared', schema:{ type:'object', properties:{ count:'int' }, required:['count'] } }]
const sharedSources = [{ type:sharedIdentity, value:{ $ref:{ scope:'constant', node:'c_record01', path:[] } } }]
const sharedPathCases = [[['$type'], 'text', false], [['summary'], 'text', true], [['data', 'count'], 'int', false]]
const bindingTypesOk = objectCompatibilityCases.every(([source, target, expected]) => compatibleTypes(source, target) === expected)
  && typeAtPath({ type:'object', additional_properties:'text' }, ['extra']).type.type === 'text'
  && sharedPathCases.every(([path, type, optional]) => {
    const selected = typeAtPath(sharedIdentity, path, sharedCatalog)
    const source = bindingSourceFor({ scope:'constant', node:'c_record01', path }, sharedSources, sharedCatalog)
    return selected.type.type === type && selected.optional === optional && source?.type.type === type
  })
console.log((bindingTypesOk?'PASS':'FAIL')+' - object and shared binding types follow their declared fields')
if (!bindingTypesOk) process.exit(1)

const partialSchema = {
  triggers: {},
  actions: {
    produce: { name:'生成数据', outputs:[{ name:'url', label:'链接', type:'string' }] },
    consume: { name:'打开链接', outputs:[{ name:'opened', label:'打开数量', type:'number' }] },
  },
}
const partialRule = {
  variables: [
    { id:'v_secret01', name:'凭据', value_type:'text', sensitive:true },
    { id:'v_count001', name:'计数', value_type:'int' },
  ],
  actions: [
    { binding_id:'a_source001', type:'produce', params:{} },
    { binding_id:'a_secret001', type:'set_variable', variable:'v_secret01', value:'测试凭据' },
    { binding_id:'a_count001', type:'set_variable', variable:'v_count001', value:42 },
    { binding_id:'a_target001', type:'consume', params:{
      url:{ $ref:{ scope:'step', node:'a_source001', path:['url'] } },
      secret:{ $ref:{ scope:'step', node:'a_secret001', path:['value'] } },
      count:{ $ref:{ scope:'step', node:'a_count001', path:['value'] } },
    } },
  ],
}
const partialFields = buildTestInputFields(partialRule, partialSchema, { startStepId:'a_target001' })
const secretOutputField = partialFields.find(field => field.node === 'a_secret001')
const countOutputField = partialFields.find(field => field.node === 'a_count001')
const partialContext = buildPreparedTestContext(partialFields, {
  [partialFields[0].key]:'https://example.com',
  [secretOutputField?.key]:'测试凭据',
  [countOutputField?.key]:'42',
})
const allPartialFields = buildAllTestInputFields(partialRule, partialSchema)
const partialInputsOk = partialFields.filter(field => field.scope === 'step').length === 3
  && partialFields[0].scope === 'step'
  && partialFields[0].node === 'a_source001'
  && partialContext.step_outputs.a_source001.url === 'https://example.com'
  && partialContext.step_outputs.a_count001.value === 42
  && secretOutputField?.sensitive === true && secretOutputField?.type === 'text'
  && allPartialFields.find(field => field.node === 'a_secret001')?.sensitive === true
  && actionOutputDefs(partialRule.actions[1], partialSchema, partialRule)[0].sensitive === true
  && allPartialFields.some(field => field.scope === 'step')
console.log((partialInputsOk?'PASS':'FAIL')+' - partial test run collects skipped upstream action outputs')
if (!partialInputsOk) process.exit(1)

const settingsNav = [...document.querySelectorAll('.nav-item')].find(
  button => button.textContent.includes('设置'),
)
settingsNav?.click()
await new Promise(r => setTimeout(r, 50))
mockConfigSecurity = {
  status:'tampered', reason:'配置签名无效', summary:{ rule_count:1, rules:[{
    name:'分支审批摘要', actions:[{
      type:'if', high_risk:true, params:{},
      then:[{ type:'run_powershell', high_risk:true, params:{ command:'Write-Output branch-safe', secret:'***' } }],
      else:[{ type:'if', high_risk:true, params:{}, then:[{
        type:'launch_program', high_risk:true, params:{ path:'nested-example.exe' },
        failure_actions:[{ type:'notify', high_risk:false, params:{ message:'分支补救摘要' } }],
      }], else:[] }],
    }],
  }] },
}
;[...document.querySelectorAll('.settings-root-list button')].find(
  button => button.textContent.includes('安全与权限'),
)?.click()
await new Promise(r => setTimeout(r, 50))
const securitySummaryDetails = document.querySelector('.config-security-rule-details')
const securitySummaryOk = securitySummaryDetails?.textContent.includes('动作 1 · 成立时 1')
  && securitySummaryDetails?.textContent.includes('Write-Output branch-safe')
  && securitySummaryDetails?.textContent.includes('动作 1 · 否则 1 · 成立时 1')
  && securitySummaryDetails?.textContent.includes('nested-example.exe')
  && securitySummaryDetails?.textContent.includes('失败补救 1')
  && securitySummaryDetails?.textContent.includes('分支补救摘要')
  && securitySummaryDetails?.textContent.includes('"secret":"***"')
  && securitySummaryDetails?.querySelectorAll('.chip-admin').length === 4
console.log((securitySummaryOk?'PASS':'FAIL')+' - security approval shows nested branches, fallback actions and masked parameters')
if (!securitySummaryOk) process.exit(1)
mockConfigSecurity = { status:'ok', reason:'', summary:null }
;[...document.querySelectorAll('.settings-root-list button')].find(
  button => button.textContent.includes('关于 NotmyFault'),
)?.click()
await new Promise(r => setTimeout(r, 20))
const capabilitiesMovedToSettings = document.querySelector('.settings-capability-report')?.textContent.includes('audio.control')
  && document.querySelector('.settings-capability-report')?.textContent.includes('1 / 2 可用')
console.log((capabilitiesMovedToSettings?'PASS':'FAIL')+' - platform capability details live under settings about')
if (!capabilitiesMovedToSettings) process.exit(1)
document.querySelector('.settings-back')?.click()
await new Promise(r => setTimeout(r, 20))
;[...document.querySelectorAll('.settings-root-list button')].find(
  button => button.textContent.includes('AI 功能'),
)?.click()
await new Promise(r => setTimeout(r, 20))
;[...document.querySelectorAll('.ai-drafting-settings button')].find(
  button => button.textContent.includes('服务配置'),
)?.click()
await new Promise(r => setTimeout(r, 20))
const aiProvider = document.querySelector('.ai-drafting-settings select[name="ai-provider"]')
const aiProviderLabels = [...(aiProvider?.options || [])].map(option => option.textContent)
const aiProvidersOk = ['OpenAI', 'DeepSeek', 'Gemini', '通义千问', '智谱 GLM', 'Moonshot / Kimi']
  .every(label => aiProviderLabels.some(value => value.includes(label)))
const providerFormatMismatchIsCustom = aiProviderIdFor({
  endpoint_url: 'https://api.openai.com/v1',
  model: 'gpt-5.4',
  api_format: 'chat_completions',
}) === 'custom'
if (aiProvider) {
  aiProvider.value = 'openai'
  aiProvider.dispatchEvent(new window.Event('change', { bubbles:true }))
}
await new Promise(r => setTimeout(r, 20))
const providerPresetApplied = document.querySelector('.ai-drafting-settings input[name="ai-endpoint"]')?.value === 'https://api.openai.com/v1'
  && document.querySelector('.ai-drafting-settings input[name="ai-model"]')?.value === 'gpt-5.4'
  && document.querySelector('.ai-drafting-settings select[name="ai-api-format"]')?.value === 'responses'
  && document.querySelector('.ai-drafting-settings')?.textContent.includes('服务商 API 兼容地址')
console.log((aiProvidersOk && providerPresetApplied && providerFormatMismatchIsCustom?'PASS':'FAIL')+' - common AI provider preset fills and matches endpoint model and format')
if (!aiProvidersOk || !providerPresetApplied || !providerFormatMismatchIsCustom) process.exit(1)
const aiEndpoint = document.querySelector('.ai-drafting-settings input[name="ai-endpoint"]')
const aiModel = document.querySelector('.ai-drafting-settings input[name="ai-model"]')
if (aiEndpoint) { aiEndpoint.value = 'https://api.example.com/v1'; aiEndpoint.dispatchEvent(new window.Event('input', { bubbles:true })) }
if (aiModel) { aiModel.value = 'test-model'; aiModel.dispatchEvent(new window.Event('input', { bubbles:true })) }
;[...document.querySelectorAll('.ai-drafting-settings button')].find(button => button.textContent.trim() === '保存')?.click()
await new Promise(r => setTimeout(r, 50))
const aiSettingsSavedWithoutKey = bridgeCalls.some(call => call.path === '/api/settings/ai-drafting'
  && call.method === 'PUT' && call.data?.enabled === true && !('api_key' in call.data))
document.querySelector('.settings-back')?.click()
await new Promise(r => setTimeout(r, 20))
;[...document.querySelectorAll('.ai-drafting-settings button')].find(button => button.textContent.includes('API 密钥'))?.click()
await new Promise(r => setTimeout(r, 20))
;[...document.querySelectorAll('.ai-drafting-settings button')].find(button => button.textContent.includes('添加 API 密钥'))?.click()
await new Promise(r => setTimeout(r, 20))
const aiKeyInput = document.querySelector('.ai-drafting-settings input[type="password"]')
const testApiKey = 'saved-key-for-mount-test'
const aiKeyPersistenceStart = bridgeCalls.length
if (aiKeyInput) {
  aiKeyInput.value = testApiKey
  aiKeyInput.dispatchEvent(new window.Event('input', { bubbles:true }))
}
await new Promise(r => setTimeout(r, 20))
;[...document.querySelectorAll('.ai-drafting-settings button')].find(button => button.textContent.includes('保存 API 密钥'))?.click()
await new Promise(r => setTimeout(r, 50))
const aiKeyPersistenceCalls = bridgeCalls.slice(aiKeyPersistenceStart)
const secretSentOnlyToKeySaveEndpoint = aiKeyPersistenceCalls.every(call => (
  !JSON.stringify(call.data || {}).includes(testApiKey)
  || (call.path === '/api/settings/ai-drafting/api-key' && call.method === 'PUT')
))
const savedKeyStatusRenders = document.querySelector('.settings-key-status.is-saved')?.textContent.includes('已保存')
const savedKeyInputIsHidden = !document.querySelector('.ai-drafting-settings input[type="password"]')
  && [...document.querySelectorAll('.ai-drafting-settings button')].some(button => button.textContent.includes('更改 API 密钥'))
const keyPersistenceHasNoRuleOrPluginEffects = !aiKeyPersistenceCalls.some(call => (
  /^\/api\/(?:rules|plugins)(?:\/|$)/.test(call.path)
))
const aiKeySavedSecurely = aiSettingsSavedWithoutKey
  && secretSentOnlyToKeySaveEndpoint
  && savedKeyStatusRenders
  && savedKeyInputIsHidden
  && keyPersistenceHasNoRuleOrPluginEffects
console.log((aiKeySavedSecurely?'PASS':'FAIL')+' - saved AI keys show a green state and hide the editor until changed')
if (!aiKeySavedSecurely) process.exit(1)
;[...document.querySelectorAll('.ai-drafting-settings button')].find(button => button.textContent.includes('更改 API 密钥'))?.click()
await new Promise(r => setTimeout(r, 20))
const unsavedApiKey = 'leave-settings-without-saving'
const unsavedKeyInput = document.querySelector('.ai-drafting-settings input[type="password"]')
if (unsavedKeyInput) {
  unsavedKeyInput.value = unsavedApiKey
  unsavedKeyInput.dispatchEvent(new window.Event('input', { bubbles:true }))
}
document.querySelector('.settings-back')?.click()
await new Promise(r => setTimeout(r, 20))
document.querySelector('.settings-back')?.click()
await new Promise(r => setTimeout(r, 20))

rulesNav?.click()
await new Promise(r => setTimeout(r, 50))
;[...document.querySelectorAll('.rules-library button')].find(
  button => button.textContent.includes('创建自动化'),
)?.click()
await new Promise(r => setTimeout(r, 50))
;[...document.querySelectorAll('.automation-create-panel button')].find(
  button => button.textContent.includes('AI 起草'),
)?.click()
await new Promise(r => setTimeout(r, 80))
const aiChatStart = aiDraftCalls.length
const longTriggerName = '定时：在连续多个条件满足后仍需完整显示的中文触发标签'
const longActionName = '显示通知：包含较长说明且不应省略或截断的中文动作标签'
const originalTriggerName = T.time_schedule.name
const originalActionName = A.notify.name
T.time_schedule.name = longTriggerName
A.notify.name = longActionName
mockAiDraftDelay = 120
async function sendNaturalDraft(message) {
  const composer = document.querySelector('#natural-draft-description')
  if (!composer) return
  composer.value = message
  composer.dispatchEvent(new window.Event('input', { bubbles:true }))
  await new Promise(r => setTimeout(r, 20))
  ;[...document.querySelectorAll('.ai-composer button')].find(
    button => (button.title || '').includes('发送'),
  )?.click()
}

await sendNaturalDraft('每天九点提醒我检查日报')
await new Promise(r => setTimeout(r, 155))
mockAiDraftDelay = 0
const firstAiTurn = aiDraftCalls[aiChatStart]
const firstTurnPreservesHistory = firstAiTurn?.messages?.length === 1
  && firstAiTurn.messages[0]?.role === 'user'
  && firstAiTurn.messages[0]?.content === '每天九点提醒我检查日报'
  && !('consent' in firstAiTurn)
  && !('api_key' in firstAiTurn)
  && !JSON.stringify(firstAiTurn).includes(unsavedApiKey)
  && document.querySelector('.natural-draft-conversation')?.textContent.includes('请补充提醒的具体内容。')

mockAiDraftResultType = 'rule_draft'
await sendNaturalDraft('通知内容写成今天的日报')
await new Promise(r => setTimeout(r, 50))
const secondAiTurn = aiDraftCalls[aiChatStart + 1]
const secondTurnPreservesHistory = secondAiTurn?.messages?.length === 3
  && secondAiTurn.messages.map(message => message.role + ':' + message.content).join('|') === [
    'user:每天九点提醒我检查日报',
    'assistant:**请补充**提醒的具体内容。',
    'user:通知内容写成今天的日报',
  ].join('|')
  && !('consent' in secondAiTurn)
  && !('api_key' in secondAiTurn)
  && !secondAiTurn.messages.some(message => message.content.includes('正在检查可用条件。'))
const ruleCard = document.querySelector('.ai-rule-card')
;[...(ruleCard?.querySelectorAll('button') || [])].find(button => button.textContent.includes('查看参数'))?.click()
await new Promise(r => setTimeout(r, 20))
const rulePreviewCardOk = !!ruleCard
  && ruleCard.textContent.includes('候选规则')
  && ruleCard.textContent.includes('2 个节点')
  && ruleCard.textContent.includes(longTriggerName)
  && ruleCard.textContent.includes(longActionName)
  && ruleCard.querySelectorAll('.ai-mini-node').length === 2
  && ruleCard.textContent.includes('***')
  && !ruleCard.textContent.includes('ai-preview-secret')
  && !![...(ruleCard?.querySelectorAll('button') || [])].find(b => b.textContent.includes('应用到编辑器'))
const aiDraftPreviewOk = rulePreviewCardOk
  && firstTurnPreservesHistory
  && secondTurnPreservesHistory
console.log((aiDraftPreviewOk?'PASS':'FAIL')+' - AI drafting preserves history and masks sensitive rule parameters')
if (!aiDraftPreviewOk) {
  console.error(JSON.stringify({ firstAiTurn, secondAiTurn, rulePreviewCardOk }))
  process.exit(1)
}
T.time_schedule.name = originalTriggerName
A.notify.name = originalActionName
requireEarlyApproval = true
earlyApprovalAttempts.length = 0
;[...(ruleCard?.querySelectorAll('button') || [])].find(
  button => button.textContent.includes('应用到编辑器'),
)?.click()
await new Promise(r => setTimeout(r, 60))
const aiDraftNeedsApproval = document.querySelector('.app-dialog')?.textContent.includes('确认后才会打开规则编辑器')
const aiApprovalInput = document.querySelector('.app-dialog-password')
if (aiApprovalInput) {
  aiApprovalInput.value = 'dashboard-secret'
  aiApprovalInput.dispatchEvent(new window.Event('input', { bubbles:true }))
  await new Promise(r => setTimeout(r, 10))
  document.querySelector('.app-dialog .btn-filled')?.click()
}
await new Promise(r => setTimeout(r, 60))
requireEarlyApproval = false
const aiDraftReviewOnlyOk = document.querySelector('.rule-title-capsule')?.textContent.includes('AI 候选草稿')
  && savedRulesPayload?.[0]?.name !== 'AI 候选草稿'
  && aiDraftNeedsApproval
  && earlyApprovalAttempts.join('|') === '|dashboard-secret'
console.log((aiDraftReviewOnlyOk?'PASS':'FAIL')+' - rule drafts open the editor only after admin approval')
if (!aiDraftReviewOnlyOk) {
  console.error(JSON.stringify({ earlyApprovalAttempts, aiDraftNeedsApproval, savedRulesPayload }))
  process.exit(1)
}

mockAiDraftError = 'AI 模拟错误：请稍后重试'
await sendNaturalDraft('继续说明这个草稿')
const retryButton = await waitFor('.ai-error-retry:not(:disabled)')
if (!retryButton) throw new Error('AI 错误提示未就绪')
const failedTurn = aiDraftCalls.at(-1)
const requestHasNoRemovedPluginFields = !('consent' in failedTurn)
const visibleErrorMessages = [...document.querySelectorAll('.natural-draft-conversation .ai-message')]
  .filter(message => message.textContent.includes(mockAiDraftError))
const errorCallCount = aiDraftCalls.length
retryButton.click()
const retryFinished = await waitFor(() => (
  !retryButton.isConnected && document.querySelector('.ai-error-retry:not(:disabled)')
))
if (!retryFinished) throw new Error('AI 重试后的错误提示未就绪')
const retryTurn = aiDraftCalls.at(-1)
const errorRetryKeepsOneRequest = aiDraftCalls.length === errorCallCount + 1
  && retryTurn?.messages?.at(-1)?.content === '继续说明这个草稿'
  && retryTurn.messages.filter(message => message.content === '继续说明这个草稿').length === 1
const errorAnnouncedOnce = visibleErrorMessages.length === 1
  && document.querySelectorAll('.ai-error[role="alert"]').length === 1
  && errorRetryKeepsOneRequest
await new Promise(r => setTimeout(r, 550))
const savedConversation = JSON.parse(window.sessionStorage.getItem('nmf_ai_conversation') || '[]')
const conversationPersistsAfterRetry = savedConversation.length > 0
  && savedConversation.at(-1)?.error === true
  && savedConversation.at(-1)?.retryPrompt === '继续说明这个草稿'
  && savedConversation.every(message => !('result' in message) && !('reasoning' in message))
  && !window.localStorage.getItem('nmf_ai_conversation')
console.log((requestHasNoRemovedPluginFields && errorAnnouncedOnce && conversationPersistsAfterRetry?'PASS':'FAIL')+' - AI errors can be retried without removed plugin fields')
if (!requestHasNoRemovedPluginFields || !errorAnnouncedOnce || !conversationPersistsAfterRetry) {
  console.error(JSON.stringify({ failedTurn, visibleErrorMessages:visibleErrorMessages.length, retryTurn, savedConversation }))
  process.exit(1)
}
mockAiDraftError = ''
mockAiStreamMode = 'stalled'
await sendNaturalDraft('这条流我想停止')
await new Promise(r => setTimeout(r, 30))
const stopDraftButton = [...document.querySelectorAll('.ai-composer button')].find(
  button => (button.title || '').includes('停止'),
)
stopDraftButton?.click()
await new Promise(r => setTimeout(r, 30))
const stoppedLabels = [...document.querySelectorAll('.ai-message')]
  .filter(element => element.textContent.includes('已停止'))
const abortStaysQuiet = stoppedLabels.length === 1
  && document.activeElement === document.querySelector('#natural-draft-description')
  && ![...document.querySelectorAll('.ai-message')].some(message => (
    message.textContent.includes('连接已中断') || message.textContent.includes('草稿服务暂时不可用')
  ))
console.log((abortStaysQuiet ? 'PASS' : 'FAIL') + ' - stopping an AI stream returns to idle without an error turn')
if (!abortStaysQuiet) process.exit(1)
mockAiStreamMode = 'normal'
mockAiDraftResultType = 'rule_draft'


await sendNaturalDraft('再补一个每天下班提醒')
await new Promise(r => setTimeout(r, 50))
const resumedAiTurn = aiDraftCalls.at(-1)
const failedAndStoppedTurnsStayOutOfHistory = !resumedAiTurn?.messages?.some(message => (
  message.content === '继续说明这个草稿' || message.content === '这条流我想停止'
))
document.querySelector('.ai-editor-panel-head button[title="新对话"]')?.click()
await new Promise(r => setTimeout(r, 30))
const resetUsesAppDialog = !!document.querySelector('.app-dialog')?.textContent.includes('清空当前对话')
;[...document.querySelectorAll('.app-dialog .btn-filled')].find(
  button => button.textContent.includes('清空'),
)?.click()
await new Promise(r => setTimeout(r, 40))
const newConversationResetsToWelcome = !document.querySelector('.natural-draft-conversation')
  && [...document.querySelectorAll('.ai-empty-chip')].length === 2
  && !document.querySelector('.ai-rule-card')
  && !window.sessionStorage.getItem('nmf_ai_conversation')
const historyAndResetOk = failedAndStoppedTurnsStayOutOfHistory
  && resetUsesAppDialog
  && newConversationResetsToWelcome
console.log((historyAndResetOk?'PASS':'FAIL')+' - failed turns stay out of history and clearing removes session data')
if (!historyAndResetOk) {
  console.error(JSON.stringify({ failedAndStoppedTurnsStayOutOfHistory, resetUsesAppDialog, newConversationResetsToWelcome }))
  process.exit(1)
}

const aiKeyDeleteStart = bridgeCalls.length
settingsNav?.click()
await new Promise(r => setTimeout(r, 50))
;[...document.querySelectorAll('.settings-root-list button')].find(button => button.textContent.includes('AI 功能'))?.click()
await new Promise(r => setTimeout(r, 20))
;[...document.querySelectorAll('.ai-drafting-settings button')].find(button => button.textContent.includes('API 密钥'))?.click()
await new Promise(r => setTimeout(r, 20))
;[...document.querySelectorAll('.ai-drafting-settings button')].find(button => button.textContent.includes('删除 API 密钥'))?.click()
await new Promise(r => setTimeout(r, 50))
const aiKeyDeleteCalls = bridgeCalls.slice(aiKeyDeleteStart)
const savedKeyDeleted = aiKeyDeleteCalls.some(call => (
  call.path === '/api/settings/ai-drafting/api-key' && call.method === 'DELETE'
)) && document.querySelector('.settings-key-status')?.textContent.includes('未设置')
const keyDeleteHasNoRuleOrPluginEffects = !aiKeyDeleteCalls.some(call => (
  /^\/api\/(?:rules|plugins)(?:\/|$)/.test(call.path)
))
console.log((savedKeyDeleted && keyDeleteHasNoRuleOrPluginEffects?'PASS':'FAIL')+' - deleting a saved AI key updates status without rule or plugin side effects')
if (!savedKeyDeleted || !keyDeleteHasNoRuleOrPluginEffects) process.exit(1)
rulesNav?.click()
await new Promise(r => setTimeout(r, 50))
;[...document.querySelectorAll('.rules-library button')].find(button => button.textContent.includes('创建自动化'))?.click()
await new Promise(r => setTimeout(r, 50))
requireEarlyApproval = true
earlyApprovalAttempts.length = 0
openQuickCreate()?.click()
await new Promise(r => setTimeout(r, 50))
;[...document.querySelectorAll('.quick-create-option')].find(button => button.textContent.includes('定时'))?.click()
await new Promise(r => setTimeout(r, 30))
;[...document.querySelectorAll('.quick-create-option')].find(button => button.textContent.includes('关闭电脑'))?.click()
await new Promise(r => setTimeout(r, 30))
;[...document.querySelectorAll('.quick-create-foot button')].find(button => button.textContent.includes('在编辑器中继续'))?.click()
await new Promise(r => setTimeout(r, 50))
const earlyApprovalBlocksEditor = document.querySelector('.app-dialog')?.textContent.includes('确认后才会打开规则编辑器')
  && !document.querySelector('.rule-title-capsule')
let earlyPasswordInput = document.querySelector('.app-dialog-password')
if (earlyPasswordInput) {
  earlyPasswordInput.value = 'wrong-secret'
  earlyPasswordInput.dispatchEvent(new window.Event('input', { bubbles:true }))
  await new Promise(r => setTimeout(r, 10))
  document.querySelector('.app-dialog .btn-filled')?.click()
}
await new Promise(r => setTimeout(r, 40))
const earlyApprovalRejectsWrongKey = document.querySelector('.app-dialog .test-field-error')?.textContent.includes('私钥密码错误')
earlyPasswordInput = document.querySelector('.app-dialog-password')
if (earlyPasswordInput) {
  earlyPasswordInput.value = 'dashboard-secret'
  earlyPasswordInput.dispatchEvent(new window.Event('input', { bubbles:true }))
  await new Promise(r => setTimeout(r, 10))
  document.querySelector('.app-dialog .btn-filled')?.click()
}
await new Promise(r => setTimeout(r, 60))
const earlyApprovalOpensEditor = earlyApprovalAttempts.join('|') === '|wrong-secret|dashboard-secret'
  && document.querySelector('.rule-title-capsule')?.textContent.includes('定时后关闭电脑')
console.log((earlyApprovalBlocksEditor && earlyApprovalRejectsWrongKey && earlyApprovalOpensEditor?'PASS':'FAIL')+' - high-risk quick drafts require a real key approval before the editor')
if (!earlyApprovalBlocksEditor || !earlyApprovalRejectsWrongKey || !earlyApprovalOpensEditor) {
  console.error(JSON.stringify({ earlyApprovalAttempts, earlyApprovalBlocksEditor, earlyApprovalRejectsWrongKey, earlyApprovalOpensEditor, quickDialog:document.querySelector('.quick-create-dialog')?.textContent, dialog:document.querySelector('.app-dialog')?.textContent }))
  process.exit(1)
}
// 创建时验证通过的密码要在保存时复用，不再弹第二遍。
const saveCallsBefore = saveConfigCalls.length
document.querySelector('.rule-title-edit')?.click()
await new Promise(r => setTimeout(r, 20))
const earlyDraftNameInput = document.querySelector('.rule-title-capsule input')
earlyDraftNameInput.value = '定时后关闭电脑（测试）'
earlyDraftNameInput.dispatchEvent(new window.Event('input', { bubbles: true }))
await new Promise(r => setTimeout(r, 350))
;[...document.querySelectorAll('.rule-editor-actions button')].find(
  button => button.textContent.includes('保存规则'),
)?.click()
await new Promise(r => setTimeout(r, 100))
const approvedSaveOk = saveConfigCalls.length === saveCallsBefore + 1
  && saveConfigCalls[saveConfigCalls.length - 1]?.password === 'dashboard-secret'
  && !document.querySelector('.app-dialog-password')
console.log((approvedSaveOk?'PASS':'FAIL')+' - early-approved admin draft saves with the verified password and no second prompt')
if (!approvedSaveOk) {
  console.error(JSON.stringify({ saveConfigCalls: saveConfigCalls.map(call => call.password), dialog:document.querySelector('.app-dialog')?.textContent }))
  process.exit(1)
}
// 删掉这条为验证保存而建的规则，让后面的删除末条测试仍从一条规则开始。
document.querySelector('.rule-back-btn')?.click()
await new Promise(r => setTimeout(r, 50))
document.querySelector('.rule-library-row .icon-btn-danger')?.click()
await new Promise(r => setTimeout(r, 30))
;[...document.querySelectorAll('.app-dialog button')].find(button => button.textContent.trim() === '删除')?.click()
await new Promise(r => setTimeout(r, 80))

rulesNav?.click()
await waitFor('.rules-library')
;[...document.querySelectorAll('.automation-section-tabs button')].find(
  button => button.textContent.includes('运行记录'),
)?.click()
await waitFor('.run-center-page')
document.querySelector('.run-export-btn')?.click()
await new Promise(r => setTimeout(r, 20))
const runExportConfirmOk = document.querySelector('.app-dialog')?.textContent.includes('不包含触发输入、动作参数、动作返回值或测试期望值')
document.querySelector('.app-dialog .btn-filled')?.click()
await new Promise(r => setTimeout(r, 20))
const runExportDownloadOk = exportedRunBlob?.type === 'application/json;charset=utf-8'
  && exportedRunBlob.size > 0
  && /^NotmyFault-run-diagnostics-\d{8}-\d{6}\.json$/.test(exportedRunFileName)
console.log((runExportConfirmOk && runExportDownloadOk?'PASS':'FAIL')+' - run center confirms and downloads a redacted export')
if (!runExportConfirmOk || !runExportDownloadOk) process.exit(1)
document.querySelector('.run-card-main')?.click()
await new Promise(r => setTimeout(r, 20))
document.querySelector('.run-step-open')?.click()
await new Promise(r => setTimeout(r, 80))
const failureSettings = document.querySelector('.node-inspector .action-failure-settings')
const failurePolicySelect = failureSettings?.querySelector('.action-failure-policy select')
failurePolicySelect.value = 'continue'
failurePolicySelect.dispatchEvent(new window.Event('change', { bubbles:true }))
const retrySelect = failureSettings?.querySelector('.action-retry-settings select')
retrySelect.value = '2'
retrySelect.dispatchEvent(new window.Event('change', { bubbles:true }))
await new Promise(r => setTimeout(r, 30))
const retryDelayInput = failureSettings?.querySelector('.action-delay-field input')
retryDelayInput.value = '3'
retryDelayInput.dispatchEvent(new window.Event('input', { bubbles:true }))
const retryBackoffSelect = [...failureSettings.querySelectorAll('.action-retry-body select')].at(-1)
retryBackoffSelect.value = 'exponential'
retryBackoffSelect.dispatchEvent(new window.Event('change', { bubbles:true }))
await new Promise(r => setTimeout(r, 30))
const actionFailureSettingsOk = failureSettings?.textContent.includes('停止，不再执行后面的动作')
  && failureSettings.textContent.includes('继续执行后面的动作')
  && failureSettings.textContent.includes('重试可能重复发通知、写文件或启动程序')
  && failureSettings.textContent.includes('插件没有提供安全停止能力')
  && document.querySelector('.classic-rule-editor .action-flow-card .flow-card-copy small')?.textContent.includes('失败后继续 · 最多重试 2 次')
console.log((actionFailureSettingsOk?'PASS':'FAIL')+' - action editor exposes stop, continue and retry settings')
if (!actionFailureSettingsOk) process.exit(1)

const addFailureAction = [...failureSettings.querySelectorAll('button')].find(
  button => button.textContent.includes('添加补救动作'),
)
addFailureAction?.click()
await new Promise(r => setTimeout(r, 20))
const failurePickerOk = document.querySelector('.plugin-picker-dialog')?.textContent.includes('添加补救动作')
document.querySelector('.plugin-picker-item')?.click()
const failureActionNote = await waitFor('.node-inspector .failure-action-note')
const controlLabels = [...document.querySelectorAll('.node-link-label')].map(label => label.textContent)
const failureBranchOk = failurePickerOk
  && document.querySelectorAll('.graph-node-failure-action').length === 1
  && controlLabels.includes('失败时')
  && controlLabels.includes('处理后继续')
  && failureActionNote?.closest('.node-inspector').textContent.includes('这个动作只会在上方主动作最终失败时执行')
  && document.querySelector('.classic-rule-editor .action-flow-card .flow-card-copy small')?.textContent.includes('1 个补救动作')
console.log((failureBranchOk?'PASS':'FAIL')+' - failed actions can run an editable recovery branch')
if (!failureBranchOk) process.exit(1)

document.querySelector('.graph-node-trigger')?.click()
await new Promise(r => setTimeout(r, 30))
;[...document.querySelectorAll('.node-inspector button')].find(button => button.textContent.includes('未发生时（NOT）'))?.click()
await new Promise(r => setTimeout(r, 30))
const absenceEditorOk = [...document.querySelectorAll('.graph-node-condition')].some(node => node.textContent.includes('未发生 · NOT'))
  && document.querySelector('.node-inspector')?.textContent.includes('等待时长（秒）')
  && !document.querySelector('.stage-check')
console.log((absenceEditorOk?'PASS':'FAIL')+' - NOT exposes an absence timer and replaces pre-run checks')
if (!absenceEditorOk) process.exit(1)
;[...document.querySelectorAll('.node-inspector button')].find(button => button.textContent.includes('改为事件发生时'))?.click()
await new Promise(r => setTimeout(r, 30))

// 私有数据参数只显示插件编辑入口。
;[...document.querySelectorAll('button')].find(
  button => button.textContent.includes('普通模式'),
)?.click()
await new Promise(r => setTimeout(r, 30))
;[...document.querySelectorAll('.flow-add-control button')].find(
  button => button.textContent.includes('添加动作'),
)?.click()
await new Promise(r => setTimeout(r, 30))
;[...document.querySelectorAll('.plugin-picker-item')].find(
  button => button.textContent.includes('宏录制动作'),
)?.click()
await new Promise(r => setTimeout(r, 40))
;[...document.querySelectorAll('.action-flow-card')].at(-1)?.querySelector('summary')?.click()
await new Promise(r => setTimeout(r, 30))
const macroCaptureButton = [...document.querySelectorAll('.plugin-data-field button')].find(
  button => button.textContent.includes('录制操作宏'),
)
macroCaptureButton?.click()
await new Promise(r => setTimeout(r, 50))
const macroFrame = document.querySelector('.extension-page-frame')
const macroFullPageOpen = !!document.querySelector('body > .extension-page-layer')
  && document.querySelector('.extension-page-head button')?.title.includes('Esc')
if (macroFrame?.contentWindow) {
  window.dispatchEvent(new window.MessageEvent('message', {
    source: macroFrame.contentWindow,
    data: {
      source:'notmyfault:extension-view', type:'invoke', request_id:'record-1',
      command:'start_recording', payload:{ append:true, minimize_window:true },
    },
  }))
  await new Promise(r => setTimeout(r, 30))
  window.dispatchEvent(new window.MessageEvent('message', {
    source: macroFrame.contentWindow,
    data: {
      source:'notmyfault:extension-view', type:'invoke', request_id:'commit-1',
      command:'commit_macro', payload:{ steps:[{ selector:{ display:{ control:'保存' } }, operation:'invoke' }] },
    },
  }))
}
await new Promise(r => setTimeout(r, 50))
const macroCaptured = bridgeCalls.some(call => (
  call.path === '/api/plugins/macro_run/extensions/commands/start_recording/invoke'
  && call.data?.session_id === 's_macro'
)) && bridgeCalls.some(call => (
  call.path === '/api/plugins/macro_run/extensions/commands/commit_macro/invoke'
  && call.data?.session_id === 's_macro'
)) && windowStateCalls.includes('minimize')
  && document.querySelector('.plugin-data-field')?.textContent.includes('1 个操作宏 · 1 步')
const macroPageSource = macroFrame?.getAttribute('srcdoc') || ''
const macroPageOk = macroPageSource.includes('操作宏插件页面')
  && macroPageSource.includes('Content-Security-Policy')
  && macroPageSource.includes("connect-src 'none'")
console.log((macroCaptured && macroPageOk && macroFullPageOpen?'PASS':'FAIL')+' - private macro data is edited through the full-page plugin extension view')
if (!macroCaptured || !macroPageOk || !macroFullPageOpen) process.exit(1)

;[...document.querySelectorAll('.classic-rule-editor .flow-add-control button')].find(button => button.textContent.includes('添加 IF'))?.click()
await new Promise(r => setTimeout(r, 30))
const ifCard = [...document.querySelectorAll('.classic-rule-editor .action-flow-card')].at(-1)
if (ifCard && !ifCard.open) ifCard.querySelector('summary')?.click()
await new Promise(r => setTimeout(r, 30))
const ifBranches = [...ifCard.querySelectorAll('.if-action-editor > .if-branch')]
for (const branch of ifBranches) {
  branch.querySelector('.flow-add-row button')?.click()
  await new Promise(r => setTimeout(r, 20))
  document.querySelector('.plugin-picker-item')?.click()
  await new Promise(r => setTimeout(r, 30))
}
const ifEditorOk = ifBranches.length === 2
  && ifBranches.every(branch => branch.querySelector('.if-branch-action'))
console.log((ifEditorOk?'PASS':'FAIL')+' - IF edits separate THEN and ELSE action lists')
if (!ifEditorOk) process.exit(1)

const saveMacroBaseline = [...document.querySelectorAll('.rule-editor-actions button')].find(
  button => button.textContent.includes('保存规则'),
)
saveMacroBaseline?.click()
await new Promise(r => setTimeout(r, 50))
const savedIf = savedRulesPayload?.find(rule => rule.rule_id === 'r_mount001')?.actions?.find(action => action.type === 'if')
const ifSavedOk = savedIf?.then?.length === 1 && savedIf?.else?.length === 1
  && savedIf.condition?.op === 'is_true' && !Object.hasOwn(savedIf, 'params')
console.log((ifSavedOk?'PASS':'FAIL')+' - saving preserves IF branches and predicate data')
if (!ifSavedOk) process.exit(1)

const definitionsPanel = document.querySelector('.variables-editor')
definitionsPanel.open = true
let invalidTypeKeptDraft = true
for (const [buttonText, name] of [['添加常量', '批次号'], ['添加变量', '当前批次']]) {
  ;[...definitionsPanel.querySelectorAll('button')].find(button => button.textContent.includes(buttonText)).click()
  await pause(20)
  const definition = [...definitionsPanel.querySelectorAll('.variable-definition')].at(-1)
  const nameField = definition.querySelector('.variable-heading input')
  nameField.value = name
  nameField.dispatchEvent(new window.Event('input', { bubbles:true }))
  const typeField = definition.querySelector('select[aria-label="数据类型"]')
  const schemaField = definition.querySelector('textarea[aria-label="类型结构 JSON"]')
  for (const invalidType of [123, {}]) {
    schemaField.value = JSON.stringify({ type:invalidType })
    schemaField.dispatchEvent(new window.Event('input', { bubbles:true }))
    await pause(20)
    invalidTypeKeptDraft &&= typeField.value === 'text' && !!definition.querySelector('.type-picker .danger-text')
  }
  typeField.value = 'int'
  typeField.dispatchEvent(new window.Event('change', { bubbles:true }))
  await pause(20)
  const valueField = definition.querySelector('.typed-value-input input')
  valueField.value = buttonText === '添加常量' ? '9007199254740993' : '0'
  valueField.dispatchEvent(new window.Event('input', { bubbles:true }))
}
;[...definitionsPanel.querySelectorAll('button')].find(button => button.textContent.includes('添加常量')).click()
await pause(20)
const objectDefinition = [...definitionsPanel.querySelector('section').querySelectorAll('.variable-definition')].at(-1)
const objectNameField = objectDefinition.querySelector('.variable-heading input')
objectNameField.value = '业务对象'
objectNameField.dispatchEvent(new window.Event('input', { bubbles:true }))
const objectTypeField = objectDefinition.querySelector('select[aria-label="数据类型"]')
objectTypeField.value = 'any'
objectTypeField.dispatchEvent(new window.Event('change', { bubbles:true }))
objectDefinition.querySelector('.expression-editor').open = true
await pause(20)
const objectExpression = objectDefinition.querySelector('textarea[aria-label="参数表达式 JSON"]')
const businessObject = { $literal:'business label', content:'保留字段' }
objectExpression.value = JSON.stringify(businessObject)
objectExpression.dispatchEvent(new window.Event('input', { bubbles:true }))
await pause(20)
const objectValueField = objectDefinition.querySelector('.typed-value-input textarea')
const mixedObjectDisplayed = JSON.parse(objectValueField.value).content === '保留字段'
objectValueField.value = JSON.stringify({ ...businessObject, content:'已编辑字段' })
objectValueField.dispatchEvent(new window.Event('input', { bubbles:true }))
await pause(20)
;[...document.querySelectorAll('.node-canvas-tools button')].find(button => button.textContent.includes('变量赋值')).click()
await pause(30)
const assignmentEditor = [...document.querySelectorAll('.variable-assignment-editor')].at(-1)
assignmentEditor.querySelector('.field-binding-button')?.click()
await pause(20)
const sourcePicker = assignmentEditor.querySelector('.binding-picker > select')
const constantOption = [...sourcePicker.options].find(option => option.textContent.includes('批次号'))
sourcePicker.value = constantOption.value
sourcePicker.dispatchEvent(new window.Event('change', { bubbles:true }))
await pause(20)
assignmentEditor.querySelector('.binding-picker-actions button').click()
await pause(20)
;[...document.querySelectorAll('.rule-editor-actions button')].find(button => button.textContent.includes('保存规则')).click()
await pause(60)
const typedRule = savedRulesPayload?.find(rule => rule.rule_id === 'r_mount001')
const typedAssignment = typedRule?.actions?.find(action => action.type === 'set_variable')
const variableEditorOk = typedRule?.constants?.[0]?.value?.$nmf_value?.data === '9007199254740993'
  && typedRule?.variables?.[0]?.value_type === 'int'
  && typedAssignment?.variable === typedRule.variables[0].id
  && typedAssignment?.value?.$ref?.node === typedRule.constants[0].id
  && invalidTypeKeptDraft && mixedObjectDisplayed
  && typedRule.constants.find(item => item.name === '业务对象')?.value?.content === '已编辑字段'
  && typedRule.constants.find(item => item.name === '业务对象')?.value?.$literal === 'business label'
console.log((variableEditorOk?'PASS':'FAIL')+' - variable editor saves precise constants and a bound assignment')
if (!variableEditorOk) process.exit(1)

const reopenMacroButton = [...document.querySelectorAll('.plugin-data-field button')].find(
  button => button.textContent.includes('1 个操作宏 · 1 步'),
)
reopenMacroButton?.click()
await new Promise(r => setTimeout(r, 50))
const reopenedMacroFrame = document.querySelector('.extension-page-frame')
const initMessages = []
if (reopenedMacroFrame?.contentWindow) {
  reopenedMacroFrame.contentWindow.postMessage = message => initMessages.push(message)
  window.dispatchEvent(new window.MessageEvent('message', {
    source: reopenedMacroFrame.contentWindow,
    data: { source:'notmyfault:extension-view', type:'ready' },
  }))
}
await new Promise(r => setTimeout(r, 20))
const reopenCall = [...bridgeCalls].reverse().find(call => (
  call.path === '/api/plugins/macro_run/extensions/commands/open_macro/invoke'
))
const macroReopensWithSavedSteps = reopenCall?.data?.current_value?.data?.steps?.length === 1
  && initMessages.some(message => message.type === 'init' && message.state?.steps?.length === 1)
  && reopenedMacroFrame?.getAttribute('srcdoc')?.includes('"steps":[{')
console.log((macroReopensWithSavedSteps?'PASS':'FAIL')+' - saved private macro reopens with its recorded steps')
if (!macroReopensWithSavedSteps) process.exit(1)
if (reopenedMacroFrame?.contentWindow) {
  window.dispatchEvent(new window.MessageEvent('message', {
    source: reopenedMacroFrame.contentWindow,
    data: {
      source:'notmyfault:extension-view', type:'invoke', request_id:'commit-2',
      command:'commit_macro', payload:{ steps:[
        { selector:{ display:{ control:'打开' } }, operation:'invoke' },
        { selector:{ display:{ control:'保存' } }, operation:'invoke' },
      ] },
    },
  }))
}
await new Promise(r => setTimeout(r, 50))
const saveAfterMacroReplacement = [...document.querySelectorAll('.rule-editor-actions button')].find(
  button => button.textContent.includes('保存规则'),
)
const replacedMacroMarksDraftDirty = document.querySelector('.plugin-data-field')?.textContent.includes('1 个操作宏 · 2 步')
  && saveAfterMacroReplacement?.disabled === false
console.log((replacedMacroMarksDraftDirty?'PASS':'FAIL')+' - replacing a saved macro marks the rule draft as changed')
if (!replacedMacroMarksDraftDirty) process.exit(1)

const testReplacedMacro = [...document.querySelectorAll('.rule-editor-actions button')].find(
  button => button.textContent.includes('测试规则'),
)
testReplacedMacro?.click()
await new Promise(r => setTimeout(r, 60))
const savedMacroSteps = savedRulesPayload?.find(rule => rule.rule_id === 'r_mount001')
  ?.actions?.find(action => action.type === 'macro_run')?.params?.macro?.data?.steps
const replacedMacroSavedBeforeTest = savedMacroSteps?.length === 2
console.log((replacedMacroSavedBeforeTest?'PASS':'FAIL')+' - testing a replaced macro persists the current draft first')
if (!replacedMacroSavedBeforeTest) process.exit(1)
;[...document.querySelectorAll('.test-prep-dialog button')].find(
  button => button.textContent.includes('用这些数据运行'),
)?.click()
await new Promise(r => setTimeout(r, 80))
const replacedMacroRunCall = [...bridgeCalls].reverse().find(call => call.path === '/api/rules/0/run')
const testedMacroSteps = replacedMacroRunCall?.data?.rule?.actions
  ?.find(action => action.type === 'macro_run')?.params?.macro?.data?.steps
const replacedMacroRunsCurrentDraft = testedMacroSteps?.length === 2
console.log((replacedMacroRunsCurrentDraft?'PASS':'FAIL')+' - testing a replaced macro executes the current steps')
if (!replacedMacroRunsCurrentDraft) process.exit(1)
;[...document.querySelectorAll('.test-result-dialog button')].find(
  button => button.textContent.includes('查看日志'),
)?.click()
await waitFor(() => document.querySelector('.run-center-page') && !document.querySelector('.rule-back-btn') && !document.querySelector('.test-result-dialog'), 3000)
const testLogsOpened = !!document.querySelector('.run-center-page')
  && !document.querySelector('.rule-back-btn')
  && !document.querySelector('.test-result-dialog')
console.log((testLogsOpened?'PASS':'FAIL')+' - test results open the run center from the rule editor')
if (!testLogsOpened) process.exit(1)

homeNav?.click()
await waitFor('.home-run-row')
const homeReloadedRuns = document.querySelector('.home-run-row')?.textContent.includes('挂载测试规则')
console.log((homeReloadedRuns?'PASS':'FAIL')+' - returning home loads recent runs without waiting for polling')
if (!homeReloadedRuns) process.exit(1)

// 创建入口和用途示例只在自动化页出现，缺少插件时定向到插件页。
rulesNav?.click()
await new Promise(r => setTimeout(r, 50))
;[...document.querySelectorAll('button')].find(
  button => button.textContent.includes('创建自动化'),
)?.click()
await new Promise(r => setTimeout(r, 50))
const unavailableTemplate = [...document.querySelectorAll('.automation-template')].find(
  button => button.textContent.includes('U盘插入后备份文件'),
)
const unavailableTemplateShown = unavailableTemplate?.textContent.includes('缺少')
unavailableTemplate?.click()
await new Promise(r => setTimeout(r, 100))
const unavailableTemplateRouted = document.querySelector('.plugin-search input')?.value === 'usb_insert'
  && document.querySelector('.plugin-missing-callout')?.textContent.includes('未找到插件 usb_insert')
console.log((unavailableTemplateShown && unavailableTemplateRouted?'PASS':'FAIL')+' - unavailable templates route to the required plugin')
if (!unavailableTemplateShown || !unavailableTemplateRouted) process.exit(1)

mockEngineState = 'running'
mockEngineRunning = true
simulateSlowStop = true
const targetedPluginSearch = document.querySelector('.plugin-search input')
targetedPluginSearch.value = ''
targetedPluginSearch.dispatchEvent(new window.Event('input', { bubbles:true }))
;[...document.querySelectorAll('.plugin-origin-switch button')].find(
  button => button.textContent.includes('内置'),
)?.click()
await new Promise(r => setTimeout(r, 40))
document.querySelector('.plugin-list-row .switch input')?.dispatchEvent(new window.Event('change', { bubbles: true }))
await new Promise(r => setTimeout(r, 50))
const restartConfirm = [...document.querySelectorAll('.app-dialog button')].find(
  button => button.textContent.includes('立即重启'),
)
restartConfirm?.click()
await new Promise(r => setTimeout(r, 1800))
const slowRestartOk = launchCalls === 1 && mockEngineState === 'running'
console.log((slowRestartOk?'PASS':'FAIL')+' - plugin restart waits until stopping reaches stopped')
if (!slowRestartOk) process.exit(1)

// 删除最后一条自动化后，首页只显示一次去往自动化页的首次引导。
rulesNav?.click()
await new Promise(r => setTimeout(r, 60))
document.querySelector('.rule-library-row .icon-btn-danger')?.click()
await new Promise(r => setTimeout(r, 30))
;[...document.querySelectorAll('.app-dialog button')].find(button => button.textContent.trim() === '删除')?.click()
await new Promise(r => setTimeout(r, 80))
const emptyAutomationPageOk = !!document.querySelector('.automation-create-panel')
  && !document.querySelector('.rule-library-row')
homeNav?.click()
await new Promise(r => setTimeout(r, 60))
const firstRunHomeOk = document.querySelector('.dashboard-first-run')?.textContent.includes('创建第一条自动化')
  && !document.querySelector('.dashboard-home .automation-template')
document.querySelector('.dashboard-first-run button')?.click()
await new Promise(r => setTimeout(r, 60))
const firstRunRoutedOk = !!document.querySelector('.automation-create-panel')
  && document.querySelector('.rules-library')?.textContent.includes('创建、测试和管理这台电脑上的自动化')
console.log((emptyAutomationPageOk && firstRunHomeOk && firstRunRoutedOk?'PASS':'FAIL')+' - empty home points once to the automation page')
if (!emptyAutomationPageOk || !firstRunHomeOk || !firstRunRoutedOk) process.exit(1)

const pluginsNav = [...document.querySelectorAll('.nav-item')].find(
  item => item.textContent.includes('插件'),
)
pluginsNav?.click()
await waitFor('.plugin-management-page')
const registryButton = [...document.querySelectorAll('.page-head button')].find(button => button.textContent.trim() === '插件索引')
registryButton?.click()
if (!await waitFor('.plugin-registry-panel')) throw new Error('插件索引入口没有打开面板')
registryButton.click()
;[...document.querySelectorAll('.page-head button')].find(
  button => button.textContent.includes('安装插件'),
)?.click()
await pause(20)
const packageInput = document.querySelector('input[type="file"][accept=".nmfp"]')
Object.defineProperty(packageInput, 'files', {
  configurable:true,
  value:[new window.File(['package'], 'author-signed.nmfp')],
})
packageInput.dispatchEvent(new window.Event('change', { bubbles:true }))
await pause(80)
document.querySelector('.preview-foot-actions .btn-filled')?.click()
await pause(40)
const signingPasswordInput = document.querySelector('.install-dialog input[type="password"]')
signingPasswordInput.value = 'local-signing-password'
signingPasswordInput.dispatchEvent(new window.Event('input', { bubbles:true }))
;[...document.querySelectorAll('.install-dialog button')].find(
  button => button.textContent.trim() === '确认',
)?.click()
await pause(100)
const signingPasswordUsesDedicatedField = pluginInstallForms.length === 1
  && pluginInstallForms[0].preview_token === 'preview-author-signed'
  && pluginInstallForms[0].signing_password === 'local-signing-password'
  && !('password' in pluginInstallForms[0])
console.log((signingPasswordUsesDedicatedField?'PASS':'FAIL')+' - author counter-signing uses the signing_password form field')
if (!signingPasswordUsesDedicatedField) process.exit(1)

const connectionsBeforeUnmount = engineConnections
document.getElementById('app').__vue_app__.unmount()
await pause(1100)
if (engineConnections !== connectionsBeforeUnmount) throw new Error('Dashboard 关闭后仍在重连 SSE')
window.close()
console.log('\nDashboard mount test: PASS')
