// API 客户端 + pywebview bridge 检测
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

export async function apiRead(path) {
  const res = await fetch(API + path, { headers: await authHeaders() })
  if (res.status === 403) {
    throw new Error('认证失败：请使用桌面端 Dashboard')
  }
  return res
}

// 带认证的写入请求（bridge 模式从 pywebview 取 token）
export async function apiWrite(path, method, body, isForm) {
  const headers = await authHeaders()
  if (!isForm) headers['Content-Type'] = 'application/json'
  const opts = { method, headers }
  if (body) opts.body = isForm ? body : JSON.stringify(body)
  const res = await fetch(API + path, opts)
  if (res.status === 403) {
    const d = await res.json().catch(() => ({}))
    throw new Error((d.detail || d.error || '认证失败') + (hasBridge() ? '' : ' — 浏览器模式不支持写入，请使用桌面端 Dashboard'))
  }
  return res
}

export async function loadConfig() {
  if (hasBridge()) return await window.pywebview.api.get_config()
  const res = await apiRead('/api/rules')
  return await res.json()
}

export async function saveConfig(rules) {
  if (hasBridge()) return await window.pywebview.api.save_config(rules)
  const res = await apiWrite('/api/rules', 'PUT', { rules })
  return await res.json()
}

export async function runRule(ruleIndex) {
  const res = await apiWrite(`/api/rules/${ruleIndex}/run`, 'POST')
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
  const r = await apiRead('/api/engine/status')
  return await r.json()
}

export async function readLogRaw(lines = 300) {
  if (hasBridge()) {
    try { return await window.pywebview.api.read_log_raw(lines) }
    catch (e) { return '读取日志失败: ' + e.message }
  }
  return '日志查看仅在 Dashboard 桌面应用中可用'
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
