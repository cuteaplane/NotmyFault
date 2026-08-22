// pywebview 桌面客户端，普通 JSON 请求经 Python bridge 代理，包含 File 的 FormData 上传直接由 WebView 发送。
export const API = 'http://127.0.0.1:19198'

export function hasBridge() {
  return !!(window.pywebview && window.pywebview.api)
}

async function authHeaders() {
  if (!hasBridge()) return {}
  try {
    const t = await window.pywebview.api.get_api_token()
    return t ? { Authorization: 'Bearer ' + t } : {}
  } catch (e) {
    return {}
  }
}

async function fetchAuthenticated(path, options = {}) {
  const request = async () => {
    const headers = { ...(options.headers || {}), ...await authHeaders() }
    return await fetch(API + path, { ...options, headers })
  }
  let res = await request()
  // 认证返回 403 时重新从 bridge 读取内存 token 再重试一次，运行中的 engine 会在这里重新发布 token。
  if (res.status === 403 && hasBridge()) res = await request()
  return res
}

function bridgeResponse(data) {
  const status = Number(data?.status || (data?.ok === false ? 400 : 200))
  return {
    ok: status >= 200 && status < 300 && data?.ok !== false,
    status,
    json: async () => data,
  }
}

async function bridgeRequest(path, method = 'GET', data = null) {
  if (!hasBridge()) throw new Error('Dashboard 桌面桥接尚未就绪')
  const result = await window.pywebview.api.request_api(path, method, data)
  if (result?.status === 403) throw new Error('Dashboard 与后台服务认证不同步')
  return bridgeResponse(result)
}

export async function apiRead(path) {
  return await bridgeRequest(path, 'GET')
}

// 含文件的 FormData 无法通过 pywebview JSON bridge，这里直接发 HTTP 请求。
export async function apiWrite(path, method, body, isForm) {
  if (!isForm) return await bridgeRequest(path, method, body || null)
  const res = await fetchAuthenticated(path, { method, body })
  if (res.status === 403) throw new Error('Dashboard 与后台服务认证不同步')
  return res
}

export async function loadConfig() {
  if (!hasBridge()) throw new Error('Dashboard 桌面桥接尚未就绪')
  return await window.pywebview.api.get_config()
}

export async function saveConfig(rules, adminKeyPassword = '') {
  if (!hasBridge()) throw new Error('Dashboard 桌面桥接尚未就绪')
  return await window.pywebview.api.save_config(rules, adminKeyPassword)
}

export async function runRule(ruleIndex, rule = null, testContext = null) {
  const body = {
    ...(rule ? { rule } : {}),
    ...(testContext || {}),
  }
  const res = await apiWrite(
    `/api/rules/${ruleIndex}/run`,
    'POST',
    Object.keys(body).length ? body : null,
  )
  return await res.json()
}

export async function cancelRun(runId) {
  const res = await apiWrite(
    `/api/runs/${encodeURIComponent(runId)}/cancel`,
    'POST',
  )
  return await res.json()
}

export async function captureDesktopElement(delaySeconds = 3) {
  const res = await apiWrite(
    '/api/desktop-elements/capture',
    'POST',
    { delay_seconds: delaySeconds },
  )
  return await res.json()
}

export async function getPluginComponents() {
  const res = await apiRead('/api/plugins/components')
  const data = await res.json()
  return Array.isArray(data?.components) ? data.components : []
}

export async function getPluginExtensions() {
  const res = await apiRead('/api/plugins/extensions')
  const data = await res.json()
  return {
    commands: Array.isArray(data?.commands) ? data.commands : [],
    parameter_editors: Array.isArray(data?.parameter_editors) ? data.parameter_editors : [],
    views: Array.isArray(data?.views) ? data.views : [],
    data_types: Array.isArray(data?.data_types) ? data.data_types : [],
  }
}

export async function invokeExtensionCommand(
  pluginId,
  commandId,
  {
    payload = null,
    sessionId = '',
    sourceKind = '',
    sourceId = '',
    currentValue = null,
  } = {},
) {
  const body = { payload }
  if (sessionId) body.session_id = sessionId
  else {
    body.source_kind = sourceKind
    body.source_id = sourceId
    body.current_value = currentValue
  }
  const res = await apiWrite(
    `/api/plugins/${encodeURIComponent(pluginId)}/extensions/commands/${encodeURIComponent(commandId)}/invoke`,
    'POST',
    body,
  )
  return await res.json()
}

export async function getExtensionViewPage(pluginId, viewId) {
  const res = await apiRead(
    `/api/plugins/${encodeURIComponent(pluginId)}/extensions/views/${encodeURIComponent(viewId)}/page`,
  )
  const data = await res.json()
  if (!res.ok || data?.ok === false || typeof data?.html !== 'string') {
    throw new Error(data?.error || '无法加载插件视图')
  }
  return data.html
}

export async function closeExtensionSession(pluginId, sessionId) {
  if (!sessionId) return { ok: true }
  const res = await apiWrite(
    `/api/plugins/${encodeURIComponent(pluginId)}/extensions/sessions/${encodeURIComponent(sessionId)}`,
    'DELETE',
  )
  return await res.json()
}

export async function invokeComponent(pluginId, componentId, method, payload = null, sessionId = '') {
  const body = { method, payload }
  if (sessionId) body.session_id = sessionId
  const res = await apiWrite(
    `/api/plugins/${encodeURIComponent(pluginId)}/components/${encodeURIComponent(componentId)}/invoke`,
    'POST',
    body,
  )
  return await res.json()
}

export async function checkDesktopElement(selector) {
  const res = await apiWrite(
    '/api/desktop-elements/check',
    'POST',
    { selector },
  )
  return await res.json()
}

export async function validateRuleDraft(rule) {
  const res = await apiWrite('/api/rules/validate', 'POST', { rule })
  return await res.json()
}

const AI_DRAFT_EVENT_TYPES = new Set(['status', 'reasoning', 'text', 'progress', 'result', 'error', 'done'])
const AI_DRAFT_HISTORY_LIMIT = 40
const AI_DRAFT_MESSAGE_LIMIT = 4000

function aiDraftRequestBody(messages, consent = null, apiKey = '') {
  const body = {
    messages: (Array.isArray(messages) ? messages : [])
      .filter(message => message?.role === 'user' || message?.role === 'assistant')
      .map(message => ({
        role: message.role,
        content: String(message.content ?? '').trim().slice(0, AI_DRAFT_MESSAGE_LIMIT),
      }))
      .filter(message => message.content)
      .slice(-AI_DRAFT_HISTORY_LIMIT),
  }
  const pluginId = typeof consent?.plugin_id === 'string' ? consent.plugin_id.trim() : ''
  const key = typeof apiKey === 'string' ? apiKey.trim() : ''
  if (pluginId) body.consent = { plugin_id: pluginId }
  if (key) body.api_key = key
  return body
}

function dispatchAIDraftEvent(eventName, dataText, onEvent) {
  if (!AI_DRAFT_EVENT_TYPES.has(eventName) || !dataText) return
  onEvent({ type: eventName, data: JSON.parse(dataText) })
}

export async function consumeAIDraftSSE(body, onEvent, signal) {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let eventName = ''
  let dataLines = []
  const flushEvent = () => {
    if (dataLines.length) dispatchAIDraftEvent(eventName, dataLines.join('\n'), onEvent)
    eventName = ''
    dataLines = []
  }
  const consumeLine = (line) => {
    if (line === '') {
      flushEvent()
      return
    }
    if (line.startsWith(':')) return
    if (line.startsWith('event:')) {
      eventName = line.slice(6).trim()
      return
    }
    if (line.startsWith('data:')) {
      dataLines.push(line.startsWith('data: ') ? line.slice(6) : line.slice(5))
    }
  }
  const consumeBuffer = () => {
    let lineEnd
    while ((lineEnd = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, lineEnd).replace(/\r$/, '')
      buffer = buffer.slice(lineEnd + 1)
      consumeLine(line)
    }
  }
  const cancelReader = () => { void reader.cancel().catch(() => {}) }
  signal?.addEventListener('abort', cancelReader, { once: true })
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      consumeBuffer()
    }
    buffer += decoder.decode()
    consumeBuffer()
    if (buffer) consumeLine(buffer.replace(/\r$/, ''))
    flushEvent()
  } finally {
    signal?.removeEventListener('abort', cancelReader)
  }
}

export async function streamRuleDraftWithAI(messages, {
  consent = null,
  apiKey = '',
  signal,
  onEvent = () => {},
} = {}) {
  const response = await fetchAuthenticated('/api/rules/draft/ai/stream', {
    method: 'POST',
    headers: { Accept: 'text/event-stream', 'Content-Type': 'application/json' },
    body: JSON.stringify(aiDraftRequestBody(messages, consent, apiKey)),
    signal,
  })
  if (!response.ok || !response.body) {
    let message = `AI 草稿服务请求失败（${response.status}）`
    const data = await response.json().catch(() => null)
    if (typeof data?.error === 'string' && data.error) message = data.error
    throw new Error(message)
  }
  await consumeAIDraftSSE(response.body, onEvent, signal)
}

export async function loadPlugins() {
  try {
    const r = await apiRead('/api/plugins/list')
    return await r.json()
  } catch (e) {
    return { triggers: {}, actions: {} }
  }
}

export async function getSchema() {
  const r = await apiRead('/api/plugins')
  return await r.json()
}

export async function getEngineStatus() {
  if (!hasBridge()) {
    return { api_alive: false, engine_running: false, engine_state: 'offline' }
  }
  return await window.pywebview.api.get_engine_status()
}

export async function getConfigSecurityStatus() {
  try {
    const r = await apiRead('/api/config/security-status')
    return await r.json()
  } catch (e) {
    return { status: 'unavailable', reason: '无法获取配置安全状态', summary: null }
  }
}

export async function approveConfigSecurity() {
  const r = await apiWrite('/api/config/security-approve', 'POST')
  return await r.json()
}

export async function getAdminAuthorizationSetting() {
  const r = await apiRead('/api/settings/admin-authorization')
  return await r.json()
}

export async function updateAdminAuthorizationSetting(mode) {
  const r = await apiWrite('/api/settings/admin-authorization', 'PUT', { mode })
  return await r.json()
}

export async function getAdminRuleVerificationSetting() {
  const r = await apiRead('/api/settings/admin-rule-verification')
  return await r.json()
}

export async function updateAdminRuleVerificationSetting(keyVerification) {
  const r = await apiWrite('/api/settings/admin-rule-verification', 'PUT', {
    key_verification: keyVerification,
  })
  return await r.json()
}

export async function getAIDraftingSetting() {
  const r = await apiRead('/api/settings/ai-drafting')
  return await r.json()
}

export async function updateAIDraftingSetting(settings) {
  const r = await apiWrite('/api/settings/ai-drafting', 'PUT', settings)
  return await r.json()
}

export async function saveAIApiKey(apiKey) {
  const r = await apiWrite('/api/settings/ai-drafting/api-key', 'PUT', { api_key: apiKey })
  return await r.json()
}

export async function deleteAIApiKey() {
  const r = await apiWrite('/api/settings/ai-drafting/api-key', 'DELETE')
  return await r.json()
}

export async function getBluetoothSetting() {
  const r = await apiRead('/api/settings/bluetooth')
  return await r.json()
}

export async function installBluetoothPlugin() {
  const r = await apiWrite('/api/settings/bluetooth/install', 'POST')
  return await r.json()
}

export async function uninstallBluetoothPlugin() {
  const r = await apiWrite('/api/settings/bluetooth/uninstall', 'POST')
  return await r.json()
}

export async function approveRuleDraft(rule, adminKeyPassword = '') {
  const r = await apiWrite('/api/rules/approve', 'POST', {
    rules: [rule], admin_key_password: adminKeyPassword,
  })
  return await r.json()
}

export async function readLogRaw(lines = 300) {
  try { return await window.pywebview.api.read_log_raw(lines) }
  catch (e) { return '读取日志失败: ' + e.message }
}

export async function readLogEntries(lines = 600) {
  try { return await window.pywebview.api.read_log_entries(lines) }
  catch (e) { return [{ ts: '', level: 'ERROR', text: '读取日志失败: ' + e.message, data: null }] }
}

export async function listLogFiles() {
  try { return await window.pywebview.api.list_log_files() }
  catch (e) { return [] }
}

export async function readLogFileEntries(name, lines = 600) {
  try { return await window.pywebview.api.read_log_file_entries(name, lines) }
  catch (e) { return [] }
}

// 诊断优先从认证 HTTP 读取引擎实时数据，离线时读取 bridge 日志，因为日志只有 action_failed，无法统计成功次数。
export async function readDiagnostics() {
  try {
    const r = await apiRead('/api/engine/diagnostics')
    if (!r.ok) return null
    const d = await r.json()
    // get_diagnostics 返回嵌套结构，展平成 build_diagnostics 格式。
    const errs = d.errors || []
    return {
      error_count: errs.length,
      warn_count: 0,
      plugin_errors: d.plugins?.errors || [],
      rule_issues: d.rules?.issues || [],
      action_ok: d.actions?.ok || 0,
      action_fails: d.actions?.fail || 0,
      hot_reload_errors: d.hot_reload_errors || 0,
      trigger_crashes: d.trigger_crashes || 0,
      trigger_crash_details: d.trigger_crash_details || [],
      last_errors: errs.map(e => typeof e === 'string' ? e : (e[1] || JSON.stringify(e))),
      last_warns: [],
    }
  } catch (e) {
    return null
  }
}

export async function listRuns(limit = 100) {
  try {
    const response = await apiRead(`/api/runs?limit=${limit}`)
    const data = await response.json()
    return Array.isArray(data?.runs) ? data.runs : []
  } catch (e) {
    return []
  }
}

export async function getRun(runId) {
  const response = await apiRead(`/api/runs/${encodeURIComponent(runId)}`)
  if (!response.ok) return null
  return await response.json()
}

// 平台能力报告：每个能力带 available / backend / reason / degraded
export async function readPlatformCapabilities() {
  try {
    const r = await apiRead('/api/platform')
    if (!r.ok) return null
    return await r.json()
  } catch (e) { return null }
}
