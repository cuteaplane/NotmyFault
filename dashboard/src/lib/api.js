// pywebview 桌面客户端。普通 JSON 请求统一经 Python bridge 代理；
// 只有包含 File 的 FormData 上传需要由 WebView 直接发送。
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
  // 运行中的 engine 会在认证失败时重新发布其内存 token。重新从 bridge
  // 读取并重试一次，可从 token 文件被清理/覆盖的状态中立即自愈。
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

// 带文件的上传无法穿过 pywebview JSON bridge，保留唯一一条直连路径。
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

export async function saveConfig(rules) {
  if (!hasBridge()) throw new Error('Dashboard 桌面桥接尚未就绪')
  return await window.pywebview.api.save_config(rules)
}

export async function runRule(ruleIndex, rule = null) {
  const res = await apiWrite(
    `/api/rules/${ruleIndex}/run`,
    'POST',
    rule ? { rule } : null,
  )
  return await res.json()
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

export async function readLogRaw(lines = 300) {
  try { return await window.pywebview.api.read_log_raw(lines) }
  catch (e) { return '读取日志失败: ' + e.message }
}

// 诊断：优先经认证 HTTP 直读引擎实时诊断（含 action_ok），
// 引擎离线时回退 bridge 直读日志。日志里只有 action_failed 没有“成功”条目，
// bridge 的 build_diagnostics 凑不出 action_ok，桌面端会永远显示“正常”而非执行次数。
export async function readDiagnostics() {
  try {
    const r = await apiRead('/api/engine/diagnostics')
    if (!r.ok) return null
    const d = await r.json()
    // get_diagnostics 返回嵌套结构，展平成 build_diagnostics 格式
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
