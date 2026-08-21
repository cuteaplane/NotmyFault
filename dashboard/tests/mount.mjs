// Dashboard 挂载冒烟测试：在 jsdom 中加载构建产物，验证应用挂载与渲染。
// 运行：npm run build && npm test
import { JSDOM } from 'jsdom'
import fs from 'fs'
import path from 'path'
import { pathToFileURL } from 'url'
import { buildRunExport } from '../src/lib/runExport.js'
import { aiProviderIdFor } from '../src/lib/providers.js'
import { streamRuleDraftWithAI } from '../src/lib/api.js'

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
const mockPluginId = `weather_report_${'long_ascii_plugin_identifier_'.repeat(4)}`
let mockPluginKind = 'action'
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
let mockAiApiKeyStatus = 'none'
let mockBluetoothInstalled = false
let exportedRunBlob = null
let exportedRunFileName = ''
const exportedFileNames = []
const textEncoder = new TextEncoder()

function pause(ms) {
  return new Promise(resolve => setTimeout(resolve, ms))
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
  if (mockAiDraftResultType === 'plugin_proposal') {
    return {
      ok:true, source:'ai', result_type:'plugin_proposal',
      proposal:{
        kind:mockPluginKind, id:mockPluginId, name:'天气状态报告',
        description:'汇总当前天气和本地环境状态，供后续自动化引用。',
        permissions:['network'],
        parameters:[{ name:'location', label:'地点', type:'string' }],
        outputs:[{ name:'report', label:'天气报告', type:'object' }],
        rationale:'当前插件目录没有可获取天气报告的动作。',
        acceptance_criteria:['可返回指定地点的天气报告', '网络不可用时返回明确错误'],
      },
    }
  }
  if (mockAiDraftResultType === 'plugin_source') {
    return {
      ok:true, source:'ai', result_type:'plugin_source',
      plugin_id:mockPluginId,
      manifest:{ id:mockPluginId, kind:mockPluginKind, name:'天气状态报告' },
      source_code:"def run():\n  return '<script>review only</script>'",
    }
  }
  return {
    ok:true, source:'ai', result_type:'rule_draft',
    draft:{ name:'AI 候选草稿', folder:'未分类', event:{ type:'time_schedule', params:{ time:'09:00' } }, actions:[{ type:'notify', params:{ title:'NotmyFault', message:'AI 生成的提醒' } }] },
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
    actions: [{ binding_id: 'a_mount001', type: 'notify', params: {} }],
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
    return { api_alive:true, engine_running:mockEngineRunning, engine_state:mockEngineState, security_mode:'permissive', rules_count:1, triggers_count:1, actions_count:1, pid:1234 }
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
    if (path === '/api/config/security-status') return { status:'ok', reason:'', summary:null }
    if (path === '/api/settings/admin-authorization' && method === 'GET') {
      return { mode:mockAdminAuthorization, effective_mode:'per_execution', supported_modes:['per_execution','engine_start'], restart_required:false }
    }
    if (path === '/api/settings/admin-authorization' && method === 'PUT') {
      mockAdminAuthorization = data.mode
      return { ok:true, mode:data.mode, effective_mode:'per_execution', restart_required:data.mode !== 'per_execution' }
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
    if (path === '/api/settings/bluetooth' && method === 'GET') return {
      available:true,
      installed:mockBluetoothInstalled,
      meta:{ id:'bluetooth_toggle', name:'开关蓝牙', description:'打开或关闭蓝牙适配器' },
    }
    if (path === '/api/settings/bluetooth/install' && method === 'POST') {
      mockBluetoothInstalled = true
      return { ok:true, restart_required:true }
    }
    if (path === '/api/settings/bluetooth/uninstall' && method === 'POST') {
      mockBluetoothInstalled = false
      return { ok:true, restart_required:true }
    }
    if (path === '/api/settings/admin-rule-verification' && method === 'GET') {
      return { key_verification: mockAdminRuleVerification }
    }
    if (path === '/api/settings/admin-rule-verification' && method === 'PUT') {
      mockAdminRuleVerification = data.key_verification === true
      return { ok:true, key_verification: mockAdminRuleVerification }
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
    if (path === '/api/desktop-elements/capture' && method === 'POST') return { ok:true, selector:{
      version:1,
      window:{ process:'notepad.exe', name:'无标题 - 记事本', control_type:50032, class_name:'Notepad' },
      target:{ automation_id:'FileSave', name:'保存', control_type:50000, class_name:'Button' },
      ancestors:[], captured_at:'2026-08-10T00:00:00Z', bounds:{ left:10, top:10, width:80, height:30 },
      capabilities:{ invoke:true, focus:true, set_text:true, read_text:true },
      display:{ control:'保存', control_type:'按钮', window:'无标题 - 记事本', app:'notepad.exe' },
    } }
    if (path === '/api/desktop-elements/check' && method === 'POST') return { ok:true, display:data.selector.display, capabilities:{ invoke:true, focus:true } }
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
    if (path === '/api/plugins/toggle') return { ok:true, restart_required:true }
    if (path === '/api/plugins/components') return {
      components: [
        { plugin_id:'hotkey', kind:'triggers', id:'record', name:'录制热键', api:'component-v1', param_types:['hotkey'], ui:{ button_label:'录制', icon:'keyboard' }, vue:'', available:true },
        { plugin_id:'uia_control', kind:'actions', id:'record', name:'录制屏幕控件', api:'component-v1', param_types:['uia_selector'], ui:{ button_label:'录制桌面步骤', icon:'screen_record' }, vue:'', available:true },
      ],
    }
    if (path === '/api/plugins/extensions') return {
      commands: [],
      parameter_editors: [{
        plugin_id:'macro_run', id:'macro_editor', parameter:'macro', data_type:'mouse_macro',
        command:'open_macro', view:'macro_workbench',
        ui:{ control:'button', empty_label:'录制操作宏', icon:'movie', description:'由插件管理' },
      }],
      views: [{ plugin_id:'macro_run', id:'macro_workbench', title:'操作宏编辑器', window_controls:['minimize','restore'] }],
      data_types: [{ plugin_id:'macro_run', id:'mouse_macro', version:1, binding:'private' }],
    }
    if (path === '/api/plugins/macro_run/extensions/views/macro_workbench/page') {
      return { ok:true, html:'<h1>操作宏插件页面</h1>' }
    }
    if (path === '/api/plugins/macro_run/extensions/commands/open_macro/invoke' && method === 'POST') {
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
    if (path === '/api/plugins/uia_control/components/record/invoke' && method === 'POST') {
      if (data?.method === 'check') return {
        ok:true, session_id:'s_uia',
        data:{ ok:true, data:{ ok:true, display:data.payload?.selector?.display || {}, capabilities:{ invoke:true, focus:true } } },
      }
      if (data?.method === 'to_actions') {
        const steps = Array.isArray(data.payload?.steps) ? data.payload.steps : []
        const actions = steps.map(step => {
          const selector = step.selector || {}
          if (step.operation === 'wait_present') return {
            type:'uia_wait',
            params:{ target:selector, wait_seconds:Number(step.waitSeconds) || 30 },
          }
          if (step.operation === 'focus_window') return {
            type:'uia_focus_window', params:{ target:selector },
          }
          if (step.operation === 'read_text') return {
            type:'uia_read_text', params:{ target:selector },
          }
          return {
            type:'uia_control',
            params:{
              target:selector,
              operation:['focus','set_text'].includes(step.operation) ? step.operation : 'invoke',
              text:step.operation === 'set_text' ? String(step.text || '') : '',
            },
          }
        })
        return { ok:true, session_id:'s_uia', data:{ ok:true, data:{ actions } } }
      }
      return { ok:true, session_id:'s_uia', data:{ ok:true, data:{ selector:{
        version:1,
        window:{ process:'notepad.exe', name:'无标题 - 记事本', control_type:50032, class_name:'Notepad' },
        target:{ automation_id:'FileSave', name:'保存', control_type:50000, class_name:'Button' },
        ancestors:[], captured_at:'2026-08-10T00:00:00Z', bounds:{ left:10, top:10, width:80, height:30 },
        capabilities:{ invoke:true, focus:true, set_text:true, read_text:true },
        display:{ control:'保存', control_type:'按钮', window:'无标题 - 记事本', app:'notepad.exe' },
      } } } }
    }
    if (path === '/api/plugins/hotkey/components/record/invoke' && method === 'POST') {
      return { ok:true, session_id:'s_hot', data:{ ok:true, data:{ hotkey:'Ctrl+Shift+M' } } }
    }
    if (path === '/api/plugins/install-source' && method === 'POST') {
      return { ok:true, id:data?.plugin_id, type:'actions', signed:false, restart_required:true }
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
  notify: { id:'notify', name:'显示通知', description:'d', origin:'builtin', enabled:true, version_code:1, permissions:['admin'], params:[{ name:'message', label:'消息', type:'string', default:'' }], outputs:[{ name:'delivered', label:'已送达', type:'bool' }] },
  shutdown_system: { id:'shutdown_system', name:'关闭电脑', description:'d', origin:'builtin', enabled:true, version_code:1, permissions:['admin'], params:[], outputs:[] },
  uia_control: { id:'uia_control', name:'操作屏幕控件', description:'d', origin:'builtin', enabled:true, version_code:1, permissions:['native_api','screen_reader'], params:[
    { name:'target', label:'屏幕控件', type:'uia_selector', value_type:'object' },
    { name:'operation', label:'怎么操作', type:'select', default:'invoke', options:[{ value:'invoke', label:'按下控件' }, { value:'focus', label:'让控件获得焦点' }, { value:'set_text', label:'写入文本' }] },
    { name:'text', label:'要写入的文本', type:'textarea', default:'', sensitive:true, summary:'hidden', visible_when:{ operation:['set_text'] } },
  ], outputs:[] },
  uia_wait: { id:'uia_wait', name:'等待屏幕控件出现', description:'d', origin:'builtin', enabled:true, version_code:1, permissions:['native_api','screen_reader'], cancellation_api:'runtime-v1', params:[
    { name:'target', label:'屏幕控件', type:'uia_selector', value_type:'object' },
    { name:'wait_seconds', label:'最多等待（秒）', type:'number', default:30 },
  ], outputs:[{ name:'found', label:'已经出现', type:'bool' }] },
  uia_focus_window: { id:'uia_focus_window', name:'切换到录制窗口', description:'d', origin:'builtin', enabled:true, version_code:1, permissions:['native_api','screen_reader'], params:[
    { name:'target', label:'窗口里的控件', type:'uia_selector', value_type:'object' },
  ], outputs:[{ name:'focused', label:'已经切换', type:'bool' }] },
  uia_read_text: { id:'uia_read_text', name:'读取屏幕控件文本', description:'d', origin:'builtin', enabled:true, version_code:1, permissions:['native_api','screen_reader'], params:[
    { name:'target', label:'屏幕控件', type:'uia_selector', value_type:'object' },
  ], outputs:[{ name:'text', label:'读取的文本', type:'string', sensitive:true, summary:'hidden' }] },
  macro_run: { id:'macro_run', name:'宏录制动作', description:'d', origin:'builtin', enabled:true, version_code:1, permissions:[], params:[
    { name:'macro', label:'操作宏', type:'plugin_data', data_type:'mouse_macro', value_type:'object', summary:'hidden' },
  ], outputs:[] },
}
window.fetch = async (url, options = {}) => {
  const u = String(url); const j = (o) => ({ json: async () => o, ok: true, status: 200 })
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

const jsFile = fs.readdirSync(path.join(distDir, 'assets')).find(f => f.endsWith('.js'))
await import(pathToFileURL(path.resolve(distDir, 'assets', jsFile)).href)
await new Promise(r => setTimeout(r, 100))

const offlineHtml = document.getElementById('app').innerHTML
const offlineStateOk = offlineHtml.includes('引擎未运行')
  && offlineHtml.includes('启动引擎')
  && !offlineHtml.includes('正在连接后台服务')
  && !offlineHtml.includes('Dashboard 正在确认')
  && !offlineHtml.includes('后台服务未启动')
  && !document.querySelector('.apatch-hero.offline .spinner')
console.log((offlineStateOk?'PASS':'FAIL')+' - offline home shows the final state without a connection spinner')
if (!offlineStateOk) process.exit(1)

await new Promise(r => setTimeout(r, 2100))
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
  ['dashboard version follows Python package', !!expectedVersion && html.includes(expectedVersion)],
  ['full engine stop stays hidden while automation runs', !html.includes('彻底停止引擎')],
]
const startupRetryOk = engineStatusReads >= 2 && configReads >= 2
  && document.querySelector('.dashboard-metrics')?.textContent.includes('1')
  && !document.querySelector('.dashboard-first-run')
checks.push(['status polling restores engine state and startup config retries', startupRetryOk])
let ok = true
for (const [name, pass] of checks) { console.log((pass?'PASS':'FAIL')+' - '+name); if(!pass) ok=false }
if (!ok) { console.error(html.substring(0, 600)); process.exit(1) }

const homeNav = [...document.querySelectorAll('.nav-item')].find(
  button => button.textContent.includes('首页'),
)
const homeRoleOk = !document.querySelector('.dashboard-home .automation-create-panel')
  && !document.querySelector('.dashboard-home .automation-template')
  && !document.querySelector('.dashboard-first-run')
console.log((homeRoleOk?'PASS':'FAIL')+' - home stays focused on status after automations exist')
if (!homeRoleOk) process.exit(1)

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

const rulesNav = [...document.querySelectorAll('.nav-item')].find(
  button => button.textContent.includes('自动化'),
)
rulesNav?.click()
await new Promise(r => setTimeout(r, 50))
const automationPageOk = document.querySelector('.rules-library')?.textContent.includes('创建、测试和管理这台电脑上的自动化')
  && !document.querySelector('.automation-create-panel')
;[...document.querySelectorAll('.rules-library button')].find(button => button.textContent.includes('创建自动化'))?.click()
await new Promise(r => setTimeout(r, 40))
const disabledAiCreateOk = document.querySelector('.rule-title-capsule')?.textContent.includes('新规则')
  && !document.querySelector('.automation-create-panel')
  && !document.querySelector('#natural-draft-description')
  && !document.querySelector('.automation-template')
console.log((automationPageOk && disabledAiCreateOk?'PASS':'FAIL')+' - AI-off creation opens a blank editor without draft surfaces')
if (!automationPageOk || !disabledAiCreateOk) process.exit(1)
document.querySelector('.rule-back-btn')?.click()
await new Promise(r => setTimeout(r, 50))
const earlySettingsNav = [...document.querySelectorAll('.nav-item')].find(
  button => button.textContent.includes('设置'),
)
earlySettingsNav?.click()
await new Promise(r => setTimeout(r, 50))
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
const quickCreateFocusOk = document.querySelector('.quick-create-dialog')?.textContent.includes('什么时候开始')
  && document.activeElement === document.querySelector('.quick-create-search input')
document.querySelector('.nmf-dialog-backdrop')?.dispatchEvent(new window.KeyboardEvent('keydown', { key:'Escape', bubbles:true }))
await new Promise(r => setTimeout(r, 300))
const quickCreateEscapeOk = !document.querySelector('.quick-create-dialog')
  && document.activeElement?.textContent.includes('自己搭一个')
console.log((quickCreateFocusOk && quickCreateEscapeOk?'PASS':'FAIL')+' - quick create focuses search and closes with Escape')
if (!quickCreateFocusOk || !quickCreateEscapeOk) process.exit(1)

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
const ruleCheckButton = [...document.querySelectorAll('button')].find(
  button => button.textContent.includes('规则检查'),
)
ruleCheckButton?.click()
await new Promise(r => setTimeout(r, 50))
const ruleCheckerOk = !!ruleCheckButton
  && bridgeCalls.some(call => call.path === '/api/rules/validate' && call.method === 'POST')
  && document.querySelector('.flow-validation')?.textContent.includes('规则可以保存')
console.log((ruleCheckerOk?'PASS':'FAIL')+' - rule checker validates the current draft before save')
if (!ruleCheckerOk) process.exit(1)
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
ruleNameInput.value = '临时规则名称'
ruleNameInput.dispatchEvent(new window.Event('input', { bubbles: true }))
await new Promise(r => setTimeout(r, 350))
const draftRecovery = JSON.parse(window.localStorage.getItem('notmyfault.ruleDraft.v1') || 'null')
const draftRecoveryStored = /^r_[a-z0-9_]{6,64}$/.test(draftRecovery?.ruleId || '')
  && !Object.prototype.hasOwnProperty.call(draftRecovery || {}, 'ruleIndex')
const draftDifferenceShown = document.querySelector('.draft-change-strip')?.textContent.includes('名称已改')
const undoDraftButton = document.querySelector('.rule-history-actions button:first-child')
const redoDraftButton = document.querySelector('.rule-history-actions button:last-child')
undoDraftButton?.click()
await new Promise(r => setTimeout(r, 80))
const undoDraftOk = document.querySelector('.rule-title-capsule input')?.value === '挂载测试规则'
redoDraftButton?.click()
await new Promise(r => setTimeout(r, 80))
const redoDraftOk = document.querySelector('.rule-title-capsule input')?.value === '临时规则名称'
document.querySelector('.rule-history-actions button:first-child')?.click()
await new Promise(r => setTimeout(r, 100))
const draftRecoveryCleared = !window.localStorage.getItem('notmyfault.ruleDraft.v1')
console.log((undoDraftOk && redoDraftOk?'PASS':'FAIL')+' - rule draft supports undo and redo history')
if (!undoDraftOk || !redoDraftOk) process.exit(1)
console.log((draftRecoveryStored && draftRecoveryCleared?'PASS':'FAIL')+' - unsaved draft recovery follows the history state')
if (!draftRecoveryStored || !draftRecoveryCleared) process.exit(1)
console.log((draftDifferenceShown && !document.querySelector('.draft-change-strip')?'PASS':'FAIL')+' - draft summary explains changes and clears after undo')
if (!draftDifferenceShown || document.querySelector('.draft-change-strip')) process.exit(1)
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
  && Number.parseFloat(document.querySelector('.graph-node-trigger')?.style.height) === 208
  && Number.parseFloat(document.querySelector('.graph-node-action')?.style.height) === 218
  && document.querySelectorAll('.graph-node-body.has-admin').length >= 2
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
  && !document.querySelector('.node-inspector')
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
  && !document.querySelector('.node-inspector')
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

const preparedFields = buildTestInputFields(ctxRule, ctxSchema)
const preparedContext = buildPreparedTestContext([
  { key:'count', scope:'event', node:'', path:['count'], type:'number', required:true },
  { key:'ready', scope:'event', node:'', path:['ready'], type:'bool', required:true },
  { key:'meta', scope:'trigger', node:'t_full01', path:['meta'], type:'object', required:true },
], { count:'12.5', ready:true, meta:'{"source":"test"}' })
const preparedTypesOk = preparedFields.length === 2
  && preparedFields.every(field => field.path[0] === 'matched_title')
  && preparedContext.event_payload.count === 12.5
  && preparedContext.event_payload.ready === true
  && preparedContext.trigger_payloads.t_full01.meta.source === 'test'
console.log((preparedTypesOk?'PASS':'FAIL')+' - prepared test data follows output types and expands full payload references')
if (!preparedTypesOk) process.exit(1)

const partialSchema = {
  triggers: {},
  actions: {
    produce: { name:'生成数据', outputs:[{ name:'url', label:'链接', type:'string' }] },
    consume: { name:'打开链接', outputs:[{ name:'opened', label:'打开数量', type:'number' }] },
  },
}
const partialRule = {
  actions: [
    { binding_id:'a_source001', type:'produce', params:{} },
    { binding_id:'a_target001', type:'consume', params:{ url:{ $ref:{ scope:'step', node:'a_source001', path:['url'] } } } },
  ],
}
const partialFields = buildTestInputFields(partialRule, partialSchema, { startStepId:'a_target001' })
const partialContext = buildPreparedTestContext(partialFields, {
  [partialFields[0].key]:'https://example.com',
})
const allPartialFields = buildAllTestInputFields(partialRule, partialSchema)
const partialInputsOk = partialFields.length === 1
  && partialFields[0].scope === 'step'
  && partialFields[0].node === 'a_source001'
  && partialContext.step_outputs.a_source001.url === 'https://example.com'
  && allPartialFields.some(field => field.scope === 'step')
console.log((partialInputsOk?'PASS':'FAIL')+' - partial test run collects skipped upstream action outputs')
if (!partialInputsOk) process.exit(1)

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
const securityDoesNotDuplicateSettings = !document.querySelector('.admin-auth-option')
console.log((permissionLabelsOk?'PASS':'FAIL')+' - every registered permission renders a non-empty label')
if (!permissionLabelsOk) process.exit(1)
console.log((securitySpacingOk?'PASS':'FAIL')+' - config status keeps dedicated spacing from the security banner')
if (!securitySpacingOk) process.exit(1)
console.log((securityDoesNotDuplicateSettings?'PASS':'FAIL')+' - security page does not duplicate settings controls')
if (!securityDoesNotDuplicateSettings) process.exit(1)
const settingsNav = [...document.querySelectorAll('.nav-item')].find(
  button => button.textContent.includes('设置'),
)
settingsNav?.click()
await new Promise(r => setTimeout(r, 50))
const rootSettingsText = document.querySelector('.settings-root-list')?.textContent || ''
const rootSettingsOk = ['安全与授权', 'AI 功能', '可选插件', '关于 NotmyFault'].every(label => rootSettingsText.includes(label))
  && !document.querySelector('.settings-nav')
  && !document.querySelector('.a16-profile')
console.log((rootSettingsOk?'PASS':'FAIL')+' - settings uses Android-style subpages without a side rail or profile capsule')
if (!rootSettingsOk) process.exit(1)

;[...document.querySelectorAll('.settings-root-list button')].find(button => button.textContent.includes('安全与授权'))?.click()
await new Promise(r => setTimeout(r, 20))
const adminAuthorizationOptions = [...document.querySelectorAll('.admin-auth-option')]
const settingsControlsOk = adminAuthorizationOptions.length === 2
  && adminAuthorizationOptions.some(button => button.textContent.includes('引擎启动时授权一次'))
  && adminAuthorizationOptions.some(button => button.textContent.includes('每次执行时确认'))
  && document.querySelector('.admin-rule-verification-settings')?.textContent.includes('创建管理员规则时验证签名私钥')
  && !document.querySelector('.ai-drafting-settings')
  && !document.querySelector('.bluetooth-settings')
console.log((settingsControlsOk?'PASS':'FAIL')+' - each settings subpage renders only its own controls')
if (!settingsControlsOk) process.exit(1)
const adminRuleVerificationCheckbox = document.querySelector('.admin-rule-verification-settings input[type="checkbox"]')
const verificationDefaultOn = adminRuleVerificationCheckbox?.checked === true
adminRuleVerificationCheckbox?.click()
await new Promise(r => setTimeout(r, 50))
const verificationSaved = bridgeCalls.some(call =>
  call.path === '/api/settings/admin-rule-verification'
  && call.method === 'PUT'
  && call.data?.key_verification === false
)
console.log((verificationDefaultOn && verificationSaved?'PASS':'FAIL')+' - admin rule verification toggle defaults on and saves')
if (!verificationDefaultOn || !verificationSaved) process.exit(1)
adminAuthorizationOptions.find(button => button.textContent.includes('引擎启动时授权一次'))?.click()
await new Promise(r => setTimeout(r, 50))
const adminAuthorizationSaved = bridgeCalls.some(call =>
  call.path === '/api/settings/admin-authorization'
  && call.method === 'PUT'
  && call.data?.mode === 'engine_start'
)
console.log((adminAuthorizationSaved?'PASS':'FAIL')+' - admin authorization selection is saved')
if (!adminAuthorizationSaved) process.exit(1)
document.querySelector('.settings-back')?.click()
await new Promise(r => setTimeout(r, 20))

;[...document.querySelectorAll('.settings-root-list button')].find(button => button.textContent.includes('AI 功能'))?.click()
await new Promise(r => setTimeout(r, 20))
const aiMenuOk = document.querySelector('.ai-drafting-settings')?.textContent.includes('AI 规则草稿')
  && document.querySelector('.ai-drafting-settings')?.textContent.includes('服务配置')
  && document.querySelector('.ai-drafting-settings')?.textContent.includes('API 密钥')
console.log((aiMenuOk?'PASS':'FAIL')+' - AI settings are split into feature, service, and API key pages')
if (!aiMenuOk) process.exit(1)
;[...document.querySelectorAll('.ai-drafting-settings button')].find(button => button.textContent.includes('服务配置'))?.click()
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
document.querySelector('.settings-back')?.click()
await new Promise(r => setTimeout(r, 20))
document.querySelector('.settings-back')?.click()
await new Promise(r => setTimeout(r, 20))

;[...document.querySelectorAll('.settings-root-list button')].find(button => button.textContent.includes('可选插件'))?.click()
await new Promise(r => setTimeout(r, 20))
const bluetoothInstallButton = [...document.querySelectorAll('.bluetooth-settings button')]
  .find(button => button.textContent.trim() === '安装')
bluetoothInstallButton?.click()
await new Promise(r => setTimeout(r, 50))
const bluetoothInstalled = bridgeCalls.some(call => (
  call.path === '/api/settings/bluetooth/install' && call.method === 'POST'
)) && document.querySelector('.bluetooth-settings')?.textContent.includes('已安装')
console.log((bluetoothInstalled?'PASS':'FAIL')+' - optional plugins live on their own settings page')
if (!bluetoothInstalled) process.exit(1)
document.querySelector('.settings-back')?.click()
await new Promise(r => setTimeout(r, 20))

;[...document.querySelectorAll('.settings-root-list button')].find(button => button.textContent.includes('关于 NotmyFault'))?.click()
await new Promise(r => setTimeout(r, 20))
const settingsAboutOk = document.querySelector('.settings-page .settings-about-card')?.textContent.includes('NotmyFault')
  && document.querySelector('.settings-page')?.textContent.includes('平台支持')
  && document.querySelector('.settings-page')?.textContent.includes('技术栈')
const noAboutNav = ![...document.querySelectorAll('.nav-item')].some(button => button.textContent.includes('关于'))
console.log((settingsAboutOk && noAboutNav?'PASS':'FAIL')+' - about stays inside settings as a subpage')
if (!settingsAboutOk || !noAboutNav) process.exit(1)
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
const aiPanelOpensInEditor = !!document.querySelector('.ai-editor-panel .ai-draft-panel')
  && !!document.querySelector('#natural-draft-description')
const aiEmptyStateOk = document.querySelector('.ai-editor-panel')?.textContent.includes('AI 自动化助手')
  && [...document.querySelectorAll('.ai-empty-chip')].length === 2
console.log((aiEmptyStateOk && aiPanelOpensInEditor?'PASS':'FAIL')+' - AI assistant opens as an editor side panel with a restrained empty state')
if (!aiEmptyStateOk || !aiPanelOpensInEditor) process.exit(1)

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
await new Promise(r => setTimeout(r, 25))
const conversation = document.querySelector('.natural-draft-conversation')
const chatScrollIsKeyboardReachable = conversation?.getAttribute('tabindex') === '0'
const chatBusyWhileRequesting = conversation?.getAttribute('aria-busy') === 'true'
await new Promise(r => setTimeout(r, 130))
mockAiDraftDelay = 0
const composerRestoresFocus = document.activeElement === document.querySelector('#natural-draft-description')
Object.defineProperty(conversation, 'scrollHeight', { configurable:true, value:320 })
conversation.scrollTop = 0
const firstAiTurn = aiDraftCalls[aiChatStart]
const firstTurnPreservesHistory = firstAiTurn?.messages?.length === 1
  && firstAiTurn.messages[0]?.role === 'user'
  && firstAiTurn.messages[0]?.content === '每天九点提醒我检查日报'
  && !('consent' in firstAiTurn)
  && !('api_key' in firstAiTurn)
  && conversation?.textContent.includes('请补充提醒的具体内容。')
const userBubbleRenders = [...document.querySelectorAll('.ai-user-bubble')].some(
  bubble => bubble.textContent === '每天九点提醒我检查日报'
)
const streamedMarkdownRendersSafely = document.querySelector('.natural-draft-markdown strong')?.textContent === '请补充'
  && !document.querySelector('.natural-draft-markdown script')
  && !document.querySelector('.natural-draft-markdown img')
const activityPanel = document.querySelector('.ai-activity')
const activityShowsStepsNotReasoning = !!activityPanel
  && activityPanel.classList.contains('finished')
  && activityPanel.textContent.includes('已完成')
  && !!activityPanel.querySelector('.ai-activity-chevron')
  && activityPanel.querySelectorAll('.ai-activity-step').length >= 2
  && !activityPanel.textContent.includes('正在检查可用条件。')
const reasoningPanel = document.querySelector('.ai-reasoning')
const reasoningStreamVisible = !!reasoningPanel
  && !!reasoningPanel.querySelector('.ai-reasoning-chevron')
  && reasoningPanel.textContent.includes('思考过程')
  && reasoningPanel.textContent.includes('正在检查可用条件。')

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
const rulePreviewCardOk = !!ruleCard
  && ruleCard.textContent.includes('候选规则')
  && ruleCard.textContent.includes('2 个节点')
  && ruleCard.textContent.includes(longTriggerName)
  && ruleCard.textContent.includes(longActionName)
  && ruleCard.querySelectorAll('.ai-mini-node').length === 2
  && !![...(ruleCard?.querySelectorAll('button') || [])].find(b => b.textContent.includes('应用到编辑器'))
const composerStaysUsable = !!document.querySelector('#natural-draft-description:not([disabled])')
const conversationFollowsLatest = conversation.scrollTop === conversation.scrollHeight
const noStreamingLeftoverText = !conversation?.textContent.includes('正在整理规则草稿。')
  && document.querySelectorAll('.ai-rule-card').length === 1
const aiDraftPreviewOk = rulePreviewCardOk
  && firstTurnPreservesHistory
  && secondTurnPreservesHistory
  && aiPanelOpensInEditor
  && userBubbleRenders
  && streamedMarkdownRendersSafely
  && activityShowsStepsNotReasoning
  && reasoningStreamVisible
  && composerStaysUsable
  && chatScrollIsKeyboardReachable
  && chatBusyWhileRequesting
  && composerRestoresFocus
  && conversationFollowsLatest
  && noStreamingLeftoverText
console.log((aiDraftPreviewOk?'PASS':'FAIL')+' - AI drafting flows flat with agent activity and a rule preview card')
if (!aiDraftPreviewOk) {
  console.error(JSON.stringify({ firstAiTurn, secondAiTurn, userBubbleRenders, streamedMarkdownRendersSafely, activityShowsStepsNotReasoning, reasoningStreamVisible, rulePreviewCardOk, conversationFollowsLatest, noStreamingLeftoverText }))
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

mockAiDraftResultType = 'plugin_proposal'
const proposalCallStart = bridgeCalls.length
const proposalSaveStart = saveConfigCalls.length
const proposalAiStart = aiDraftCalls.length
await sendNaturalDraft('根据天气情况生成状态报告')
await new Promise(r => setTimeout(r, 50))
const proposalAiTurn = aiDraftCalls[proposalAiStart]
const proposalResult = [...document.querySelectorAll('.ai-card')].at(-1)
const proposalText = proposalResult?.textContent || ''
const proposalDisplaysSafeMetadata = [
  '缺少能力提案',
  '动作',
  '天气状态报告',
  mockPluginId,
  '汇总当前天气和本地环境状态，供后续自动化引用。',
  'network',
  '地点',
  'string',
  '天气报告',
  'object',
  '当前插件目录没有可获取天气报告的动作。',
  '可返回指定地点的天气报告',
  '网络不可用时返回明确错误',
].every(value => proposalText.includes(value))
const consentButton = [...(proposalResult?.querySelectorAll('button') || [])].find(
  button => button.textContent.includes('同意生成插件草稿'),
)
const pluginIdRowRenders = [...(proposalResult?.querySelectorAll('code') || [])].some(
  code => code.textContent === mockPluginId
)
const proposalMutationCalls = bridgeCalls.slice(proposalCallStart).filter(call => (
  /^\/api\/(?:plugins|rules)(?:\/|$)/.test(call.path)
  && call.path !== '/api/rules/draft/ai'
))
const proposalAiTurnOk = proposalAiTurn?.messages?.at(-1)?.role === 'user'
  && proposalAiTurn.messages.at(-1)?.content === '根据天气情况生成状态报告'
  && proposalAiTurn.messages.length === 5
  && !('api_key' in proposalAiTurn)
  && !('consent' in proposalAiTurn)
const proposalReviewOnlyOk = proposalDisplaysSafeMetadata
  && pluginIdRowRenders
  && !!consentButton
  && proposalResult?.querySelectorAll('button').length === 1
  && proposalMutationCalls.length === 0
  && saveConfigCalls.length === proposalSaveStart
  && proposalAiTurnOk
if (!proposalReviewOnlyOk) {
  console.error(JSON.stringify({ proposalText, proposalMutationCalls, proposalAiTurn, consentButton }))
  process.exit(1)
}
console.log((proposalReviewOnlyOk?'PASS':'FAIL')+' - capability proposals render read-only metadata with a single consent action')

mockAiDraftResultType = 'plugin_source'
const consentAiStart = aiDraftCalls.length
const consentSideEffectStart = bridgeCalls.length
consentButton?.click()
await new Promise(r => setTimeout(r, 50))
const consentCalls = aiDraftCalls.slice(consentAiStart)
const consentTurn = consentCalls[0]
const sourceResult = document.querySelector('.natural-draft-source')
const sourceText = sourceResult?.textContent || ''
const consentSendsExactPluginId = consentCalls.length === 1
  && JSON.stringify(consentTurn?.consent) === JSON.stringify({ plugin_id:mockPluginId })
  && consentTurn?.messages?.at(-1)?.role === 'user'
  && consentTurn?.messages?.at(-1)?.content === ('我同意生成插件草稿：' + mockPluginId)
const sourceRendersInstallable = sourceText.includes('插件已生成')
  && sourceText.includes(mockPluginId)
  && sourceText.includes('<script>review only</script>')
  && sourceResult?.querySelectorAll('pre').length === 2
  && [...(sourceResult?.querySelectorAll('pre') || [])].every(pre => pre.getAttribute('tabindex') === '0')
  && !sourceResult?.querySelector('script')
  && !sourceResult?.textContent.includes('同意生成插件草稿')
const consentSideEffects = bridgeCalls.slice(consentSideEffectStart).filter(call => (
  /^\/api\/(?:plugins|rules)(?:\/|$)/.test(call.path)
  && call.path !== '/api/rules/draft/ai'
))
console.log((consentSendsExactPluginId && sourceRendersInstallable && consentSideEffects.length === 0?'PASS':'FAIL')+' - plugin consent renders an installable source card without side effects')
if (!consentSendsExactPluginId || !sourceRendersInstallable || consentSideEffects.length) {
  console.error(JSON.stringify({ consentCalls, sourceText, consentSideEffects }))
  process.exit(1)
}

const actionDownloadStart = exportedFileNames.length
;[...(sourceResult?.querySelectorAll('button') || [])].find(
  button => button.textContent.includes('下载文件'),
)?.click()
const actionDownloadNames = exportedFileNames.slice(actionDownloadStart)
const actionDownloadsUsePluginId = actionDownloadNames.includes(`${mockPluginId}.action.json`)
  && actionDownloadNames.includes(`${mockPluginId}.action.py`)

const installStart = bridgeCalls.length
;[...(sourceResult?.querySelectorAll('button') || [])].find(
  button => button.textContent.includes('安装插件'),
)?.click()
await new Promise(r => setTimeout(r, 80))
const installCalls = bridgeCalls.slice(installStart).filter(call => call.path === '/api/plugins/install-source')
const installPayload = installCalls[0]?.data
const pluginInstalledDirectly = installCalls.length === 1
  && installCalls[0]?.method === 'POST'
  && installPayload?.plugin_id === mockPluginId
  && installPayload?.kind === 'action'
  && installPayload?.manifest?.id === mockPluginId
  && typeof installPayload?.source === 'string'
  && !('password' in installPayload)
const installNoticeUsesPluginId = document.querySelector('.snackbar')?.textContent.includes(`插件 ${mockPluginId} 已安装`)
console.log((pluginInstalledDirectly && actionDownloadsUsePluginId && installNoticeUsesPluginId?'PASS':'FAIL')+' - AI plugin files keep their id and install through the source endpoint')
if (!pluginInstalledDirectly || !actionDownloadsUsePluginId || !installNoticeUsesPluginId) {
  console.error(JSON.stringify({ installCalls, actionDownloadNames, installNoticeUsesPluginId }))
  process.exit(1)
}

mockPluginKind = 'trigger'
const triggerDownloadStart = exportedFileNames.length
await sendNaturalDraft('生成前台窗口切换触发器源码')
await new Promise(r => setTimeout(r, 50))
const triggerSourceResult = [...document.querySelectorAll('.natural-draft-source')].at(-1)
;[...(triggerSourceResult?.querySelectorAll('button') || [])].find(
  button => button.textContent.includes('下载文件'),
)?.click()
const triggerDownloadNames = exportedFileNames.slice(triggerDownloadStart)
const triggerDownloadsUsePluginId = triggerDownloadNames.includes(`${mockPluginId}.trigger.json`)
  && triggerDownloadNames.includes(`${mockPluginId}.trigger.py`)
console.log((triggerDownloadsUsePluginId?'PASS':'FAIL')+' - trigger plugin downloads use trigger filenames')
if (!triggerDownloadsUsePluginId) {
  console.error(JSON.stringify({ triggerDownloadNames }))
  process.exit(1)
}
mockPluginKind = 'action'

mockAiDraftError = 'AI 模拟错误：请稍后重试'
await sendNaturalDraft('继续说明这个草稿')
await new Promise(r => setTimeout(r, 50))
const afterSourceTurn = aiDraftCalls.at(-1)
const sourceStaysOutOfHistory = !afterSourceTurn?.messages?.some(message => (
  message.content.includes('<script>review only</script>')
)) && !('consent' in afterSourceTurn)
const visibleErrorMessages = [...document.querySelectorAll('.natural-draft-conversation .ai-message')]
  .filter(message => message.textContent.includes(mockAiDraftError))
const retryButton = document.querySelector('.ai-error-retry')
const errorCallCount = aiDraftCalls.length
retryButton?.click()
await new Promise(r => setTimeout(r, 50))
const retryTurn = aiDraftCalls.at(-1)
const errorRetryKeepsOneRequest = aiDraftCalls.length === errorCallCount + 1
  && retryTurn?.messages?.at(-1)?.content === '继续说明这个草稿'
  && retryTurn.messages.filter(message => message.content === '继续说明这个草稿').length === 1
const errorAnnouncedOnce = visibleErrorMessages.length === 1
  && document.querySelectorAll('.ai-error[role="alert"]').length === 1
  && errorRetryKeepsOneRequest
await new Promise(r => setTimeout(r, 550))
const savedConversation = JSON.parse(window.localStorage.getItem('nmf_ai_conversation') || '[]')
const conversationPersistsAfterRetry = savedConversation.length > 0
  && savedConversation.at(-1)?.error === true
  && savedConversation.at(-1)?.retryPrompt === '继续说明这个草稿'
console.log((sourceStaysOutOfHistory && errorAnnouncedOnce && conversationPersistsAfterRetry?'PASS':'FAIL')+' - generated source stays out of history and errors can be retried')
if (!sourceStaysOutOfHistory || !errorAnnouncedOnce || !conversationPersistsAfterRetry) {
  console.error(JSON.stringify({ afterSourceTurn, visibleErrorMessages:visibleErrorMessages.length, retryTurn, savedConversation }))
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


const followPanel = document.querySelector('.natural-draft-conversation')
Object.defineProperty(followPanel, 'clientHeight', { configurable:true, value:200 })
Object.defineProperty(followPanel, 'scrollHeight', { configurable:true, value:1000 })
mockAiDraftDelay = 120
void sendNaturalDraft('再补一个每天下班提醒')
await new Promise(r => setTimeout(r, 25))
const resumedAiTurn = aiDraftCalls.at(-1)
const failedAndStoppedTurnsStayOutOfHistory = !resumedAiTurn?.messages?.some(message => (
  message.content === '继续说明这个草稿' || message.content === '这条流我想停止'
))
followPanel.scrollTop = 100
followPanel.dispatchEvent(new window.Event('scroll', { bubbles:true }))
await new Promise(r => setTimeout(r, 20))
const jumpToLatestAppears = !!document.querySelector('.ai-jump-latest')
await new Promise(r => setTimeout(r, 140))
mockAiDraftDelay = 0
const scrollFollowPausedWhileReadingBack = followPanel.scrollTop === 100
document.querySelector('.ai-jump-latest')?.click()
await new Promise(r => setTimeout(r, 30))
const jumpToLatestResumesFollow = followPanel.scrollTop === followPanel.scrollHeight

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
const scrollAndResetOk = failedAndStoppedTurnsStayOutOfHistory
  && jumpToLatestAppears
  && scrollFollowPausedWhileReadingBack
  && jumpToLatestResumesFollow
  && resetUsesAppDialog
  && newConversationResetsToWelcome
console.log((scrollAndResetOk?'PASS':'FAIL')+' - failed and stopped turns stay out of history while scroll and reset remain stable')
if (!scrollAndResetOk) {
  console.error(JSON.stringify({ failedAndStoppedTurnsStayOutOfHistory, jumpToLatestAppears, scrollFollowPausedWhileReadingBack, jumpToLatestResumesFollow, resetUsesAppDialog, newConversationResetsToWelcome }))
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

const logsNav = [...document.querySelectorAll('.nav-item')].find(
  button => button.textContent.includes('日志'),
)
logsNav?.click()
await new Promise(r => setTimeout(r, 80))
const runCenterOk = document.querySelector('.run-center-page')?.textContent.includes('运行记录')
  && document.querySelector('.run-card')?.textContent.includes('挂载测试规则')
  && document.querySelector('.run-card')?.textContent.includes('失败')
const runActionFilter = document.querySelector('.run-action-filter')
runActionFilter.value = 'notify'
runActionFilter.dispatchEvent(new window.Event('change', { bubbles:true }))
await new Promise(r => setTimeout(r, 20))
const runActionFilterOk = document.querySelectorAll('.run-card').length === 1
  && runActionFilter.selectedOptions[0]?.textContent.includes('显示通知')
const runTimeFilter = document.querySelector('.run-time-filter')
runTimeFilter.value = '24h'
runTimeFilter.dispatchEvent(new window.Event('change', { bubbles:true }))
await new Promise(r => setTimeout(r, 20))
const runTimeFilterOk = !document.querySelector('.run-card')
  && document.querySelector('.run-empty')?.textContent.includes('没有符合条件的运行')
runTimeFilter.value = 'all'
runTimeFilter.dispatchEvent(new window.Event('change', { bubbles:true }))
await new Promise(r => setTimeout(r, 20))
document.querySelector('.run-export-btn')?.click()
await new Promise(r => setTimeout(r, 20))
const runExportConfirmOk = document.querySelector('.app-dialog')?.textContent.includes('不包含触发输入、动作参数、动作返回值或测试期望值')
document.querySelector('.app-dialog .btn-filled')?.click()
await new Promise(r => setTimeout(r, 20))
const runExportDownloadOk = exportedRunBlob?.type === 'application/json;charset=utf-8'
  && exportedRunBlob.size > 0
  && /^NotmyFault-run-diagnostics-\d{8}-\d{6}\.json$/.test(exportedRunFileName)
console.log((runActionFilterOk && runTimeFilterOk?'PASS':'FAIL')+' - run center filters by action and time range')
if (!runActionFilterOk || !runTimeFilterOk) process.exit(1)
console.log((runExportConfirmOk && runExportDownloadOk?'PASS':'FAIL')+' - run center confirms and downloads a redacted export')
if (!runExportConfirmOk || !runExportDownloadOk) process.exit(1)
document.querySelector('.run-card-main')?.click()
await new Promise(r => setTimeout(r, 20))
const runStepsOk = document.querySelector('.run-step')?.textContent.includes('显示通知')
  && document.querySelector('.step-summary-strip')?.textContent.includes('输入')
  && document.querySelector('.step-summary-strip')?.textContent.includes('消息')
  && document.querySelector('.step-summary-strip')?.textContent.includes('输出')
  && document.querySelector('.step-summary-strip')?.textContent.includes('已送达')
  && document.querySelector('.run-card-actions button[disabled]')?.textContent.includes('重新运行')
document.querySelector('.run-step-open')?.click()
await new Promise(r => setTimeout(r, 80))
const failedStepJumpOk = document.querySelector('.node-inspector')?.textContent.includes('显示通知')
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
console.log((runCenterOk && runStepsOk?'PASS':'FAIL')+' - run center summarizes runs and expands step details')
if (!runCenterOk || !runStepsOk) process.exit(1)
console.log((failedStepJumpOk?'PASS':'FAIL')+' - failed run step opens the matching editor node')
if (!failedStepJumpOk) process.exit(1)
console.log((actionFailureSettingsOk?'PASS':'FAIL')+' - action editor exposes stop, continue and retry settings')
if (!actionFailureSettingsOk) process.exit(1)

const addFailureAction = [...failureSettings.querySelectorAll('button')].find(
  button => button.textContent.includes('添加补救动作'),
)
addFailureAction?.click()
await new Promise(r => setTimeout(r, 20))
const failurePickerOk = document.querySelector('.plugin-picker-dialog')?.textContent.includes('添加补救动作')
document.querySelector('.plugin-picker-item')?.click()
await new Promise(r => setTimeout(r, 40))
const controlLabels = [...document.querySelectorAll('.node-link-label')].map(label => label.textContent)
const failureBranchOk = failurePickerOk
  && document.querySelectorAll('.graph-node-failure-action').length === 1
  && controlLabels.includes('失败时')
  && controlLabels.includes('处理后继续')
  && document.querySelector('.node-inspector')?.textContent.includes('这个动作只会在上方主动作最终失败时执行')
  && document.querySelector('.classic-rule-editor .action-flow-card .flow-card-copy small')?.textContent.includes('1 个补救动作')
console.log((failureBranchOk?'PASS':'FAIL')+' - failed actions can run an editable recovery branch')
if (!failureBranchOk) process.exit(1)

const replaceRecoveryType = document.querySelector('.node-inspector .plugin-type-button')
replaceRecoveryType?.click()
await new Promise(r => setTimeout(r, 20))
;[...document.querySelectorAll('.plugin-picker-item')].find(
  button => button.textContent.includes('操作屏幕控件'),
)?.click()
await new Promise(r => setTimeout(r, 40))
const chooseDesktopElement = [...document.querySelectorAll('.uia-selector-field button')].find(
  button => button.textContent.includes('选择屏幕上的控件'),
)
chooseDesktopElement?.click()
await new Promise(r => setTimeout(r, 60))
const desktopElementCard = document.querySelector('.uia-selector-card')
const verifyDesktopElement = [...document.querySelectorAll('.uia-selector-field button')].find(
  button => button.textContent.trim() === '检查',
)
verifyDesktopElement?.click()
await new Promise(r => setTimeout(r, 40))
const desktopSelectorOk = desktopElementCard?.textContent.includes('保存')
  && desktopElementCard.textContent.includes('notepad.exe')
  && bridgeCalls.some(call => (
    call.path === '/api/plugins/uia_control/components/record/invoke'
    && call.data?.method === 'capture'
  ))
  && bridgeCalls.some(call => (
    call.path === '/api/plugins/uia_control/components/record/invoke'
    && call.data?.method === 'check'
  ))
  && document.querySelector('.uia-selector-status')?.textContent.includes('检查通过')
console.log((desktopSelectorOk?'PASS':'FAIL')+' - screen control selector captures and rechecks a UIA target')
if (!desktopSelectorOk) process.exit(1)

const actionCountBeforeRecording = document.querySelectorAll('.graph-node-action').length
;[...document.querySelectorAll('.node-canvas-toolbar button')].find(
  button => button.textContent.includes('录制桌面步骤'),
)?.click()
await new Promise(r => setTimeout(r, 30))
let recordNext = [...document.querySelectorAll('.desktop-recorder-dialog button')].find(
  button => button.textContent.includes('选择第一个控件'),
)
recordNext?.click()
await new Promise(r => setTimeout(r, 40))
recordNext = [...document.querySelectorAll('.desktop-recorder-dialog button')].find(
  button => button.textContent.includes('选择下一个控件'),
)
recordNext?.click()
await new Promise(r => setTimeout(r, 40))
document.querySelector('.desktop-recorder-steps li .icon-btn-danger')?.click()
await new Promise(r => setTimeout(r, 20))
const recordedStepCount = document.querySelectorAll('.desktop-recorder-steps li').length
const recordedOperation = document.querySelector('.desktop-recorder-steps select')
if (recordedOperation) {
  recordedOperation.value = 'set_text'
  recordedOperation.dispatchEvent(new window.Event('change', { bubbles:true }))
}
await new Promise(r => setTimeout(r, 20))
const recordedText = document.querySelector('.desktop-recorder-text textarea')
if (recordedText) {
  recordedText.value = '月度报告'
  recordedText.dispatchEvent(new window.Event('input', { bubbles:true }))
}
;[...document.querySelectorAll('.desktop-recorder-foot button')].find(
  button => button.textContent.includes('加入 1 个步骤'),
)?.click()
await new Promise(r => setTimeout(r, 50))
const recorderOk = recordedStepCount === 1
  && !document.querySelector('.desktop-recorder-dialog')
  && document.querySelectorAll('.graph-node-action').length === actionCountBeforeRecording + 1
  && [...document.querySelectorAll('.graph-node-action')].some(node => node.textContent.includes('操作屏幕控件'))
  && [...document.querySelectorAll('.node-inspector textarea')].some(input => input.value === '月度报告')
console.log((recorderOk?'PASS':'FAIL')+' - desktop recording session deletes mistakes and inserts editable steps')
if (!recorderOk) process.exit(1)

const actionCountBeforeRead = document.querySelectorAll('.graph-node-action').length
;[...document.querySelectorAll('.node-canvas-toolbar button')].find(
  button => button.textContent.includes('录制桌面步骤'),
)?.click()
await new Promise(r => setTimeout(r, 20))
;[...document.querySelectorAll('.desktop-recorder-dialog button')].find(
  button => button.textContent.includes('选择第一个控件'),
)?.click()
await new Promise(r => setTimeout(r, 40))
const readOperation = document.querySelector('.desktop-recorder-steps select')
if (readOperation) {
  readOperation.value = 'read_text'
  readOperation.dispatchEvent(new window.Event('change', { bubbles:true }))
}
await new Promise(r => setTimeout(r, 20))
;[...document.querySelectorAll('.desktop-recorder-foot button')].find(
  button => button.textContent.includes('加入 1 个步骤'),
)?.click()
await new Promise(r => setTimeout(r, 50))
const recordedReadOk = document.querySelectorAll('.graph-node-action').length === actionCountBeforeRead + 1
  && [...document.querySelectorAll('.graph-node-action')].some(node => node.textContent.includes('读取屏幕控件文本'))
  && document.querySelector('.node-inspector .workflow-output-hint')?.textContent.includes('读取的文本')
console.log((recordedReadOk?'PASS':'FAIL')+' - recorded text reading exposes a sensitive bindable output')
if (!recordedReadOk) process.exit(1)

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

const saveMacroBaseline = [...document.querySelectorAll('.rule-editor-actions button')].find(
  button => button.textContent.includes('保存规则'),
)
saveMacroBaseline?.click()
await new Promise(r => setTimeout(r, 50))

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
  button => button.textContent.includes('关闭'),
)?.click()
await new Promise(r => setTimeout(r, 20))

// 从规则页离开后再切页不应白屏（嵌套 Transition 叠在同一元素时 leave 永不结束）。
homeNav?.click()
await new Promise(r => setTimeout(r, 50))
const backToHomeOk = !!document.querySelector('.apatch-hero') && !document.querySelector('.rules-library')
console.log((backToHomeOk?'PASS':'FAIL')+' - leaving the rules page still mounts the next view')
if (!backToHomeOk) process.exit(1)

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
await new Promise(r => setTimeout(r, 40))
document.querySelector('.plugin-card .switch input')?.dispatchEvent(new window.Event('change', { bubbles: true }))
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

console.log('\nDashboard mount test: PASS')
process.exit(0)
