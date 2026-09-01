export function getParamDefs(m) {
  if (!m) return []
  if (Array.isArray(m.params)) return m.params
  if (Array.isArray(m.required_params))
    return m.required_params.map(p => ({ name: p, label: p, type: 'string', default: '' }))
  return []
}

const unsafeParamNames = new Set(['__proto__', 'constructor', 'prototype'])

export function isSafeParamName(name) {
  return typeof name === 'string'
    && /^[a-zA-Z_][a-zA-Z0-9_-]*$/.test(name)
    && !unsafeParamNames.has(name)
}

export function buildDefaultParams(m) {
  const o = Object.create(null)
  getParamDefs(m).forEach(p => {
    if (isSafeParamName(p.name)) o[p.name] = p.default ?? ''
  })
  return o
}

export function ensureParams(node) {
  if (!node || typeof node !== 'object') return {}
  if (!node.params || typeof node.params !== 'object' || Array.isArray(node.params)) {
    node.params = Object.create(null)
  } else {
    for (const key of Object.keys(node.params)) {
      if (!isSafeParamName(key)) delete node.params[key]
    }
  }
  return node.params
}

// options 可能是字符串或带 value 字段的对象，这里统一取 value。
export function optValue(o) {
  return typeof o === 'object' && o !== null ? o.value : o
}

// options 可能是字符串或带 label 字段的对象，这里统一取显示文字。
export function optLabel(o) {
  if (typeof o === 'object' && o !== null) return o.label || friendlyOptionLabel(o.value)
  return friendlyOptionLabel(o)
}

const commonOptionLabels = {
  opened: '已打开', closed: '已关闭',
  connected: '已连接', disconnected: '已断开',
  running: '运行中', stopped: '已停止',
  on: '开启', off: '关闭', toggle: '切换', query: '查询状态',
  created: '新建', modified: '修改', deleted: '删除', all: '全部',
  above: '高于阈值', below: '低于阈值',
  low_brightness: '低亮度', high_brightness: '高亮度',
}

function friendlyOptionLabel(value) {
  const text = String(value ?? '')
  return commonOptionLabels[text] || text.replace(/[_-]+/g, ' ')
}

// visible_when 用参数名映射允许值列表，多个参数必须同时满足。
export function isVisible(paramDef, currentParams) {
  const vw = paramDef.visible_when
  if (!vw) return true
  for (const [key, vals] of Object.entries(vw)) {
    const raw = currentParams?.[key]
    // 参数值来自运行数据时，编辑期无法判断条件，这里保留显示。
    if (raw && typeof raw === 'object' && raw.$ref) continue
    const cur = String(raw ?? '')
    if (!vals.map(String).includes(cur)) return false
  }
  return true
}

export function getVisibleParamDefs(meta, currentParams) {
  return getParamDefs(meta).filter(p => isVisible(p, currentParams))
}

export function pluginUnavailableReason(meta) {
  if (!meta) return '未安装'
  if (meta.enabled === false) return '插件已禁用'
  if (meta.platform_compatible === false || meta.availability === 'unavailable') {
    const reasons = Array.isArray(meta.unavailable_reasons) ? meta.unavailable_reasons : []
    return reasons.join('；') || '当前系统不可用'
  }
  return meta._error ? String(meta._error) : ''
}

const triggerCategoryMap = {
  manual: '手动', hotkey: '手动', time_schedule: '时间', idle_detect: '时间',
  window_title: '程序', process_state: '程序',
  folder_monitor: '文件与内容', clipboard: '文件与内容', usb_insert: '设备',
  network_status: '网络', power_state: '系统', system_resource: '系统',
}
const triggerCategoryOrder = ['手动', '时间', '程序', '文件与内容', '设备', '网络', '系统', '其他']

export function groupTriggerKeys(keys) {
  const groups = new Map(triggerCategoryOrder.map(name => [name, []]))
  keys.forEach(key => groups.get(triggerCategoryMap[key] || '其他').push(key))
  return [...groups].filter(([, items]) => items.length)
}

const actionCategoryMap = {
  launch_program: '程序', kill_process: '程序', run_powershell: '程序',
  file_operation: '文件与内容', document_quiescent: '文件与内容', clipboard_set: '文件与内容',
  http_request: '网络', bluetooth_toggle: '设备',
  display_control: '桌面', screenshot: '桌面', wallpaper: '桌面', lock_screen: '桌面',
  notify: '通知与声音', text_to_speech: '通知与声音', set_volume: '通知与声音',
  shutdown_system: '系统',
}
const actionCategoryOrder = ['程序', '文件与内容', '网络', '设备', '桌面', '通知与声音', '系统', '其他']

export function groupActionKeys(keys) {
  const groups = new Map(actionCategoryOrder.map(name => [name, []]))
  keys.forEach(key => groups.get(actionCategoryMap[key] || '其他').push(key))
  return [...groups].filter(([, items]) => items.length)
}

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function valuesEqual(left, right) {
  if (left === right) return true
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left)
      && Array.isArray(right)
      && left.length === right.length
      && left.every((value, index) => valuesEqual(value, right[index]))
  }
  if (!isObject(left) || !isObject(right)) return false
  const leftKeys = Object.keys(left)
  const rightKeys = Object.keys(right)
  return leftKeys.length === rightKeys.length
    && leftKeys.every(key => Object.prototype.hasOwnProperty.call(right, key)
      && valuesEqual(left[key], right[key]))
}

export function isConditionLeaf(node) {
  return isObject(node)
    && typeof node.type === 'string'
    && node.type.length > 0
    && !Array.isArray(node.children)
    && !Array.isArray(node.events)
}

export function normalizeConditionTree(node) {
  if (!isObject(node)) return node
  if (isConditionLeaf(node)) return node

  node.children = Array.isArray(node.children)
    ? node.children
    : (Array.isArray(node.events) ? node.events : [])
  node.op = node.op || (node.type === 'and' ? 'all' : 'any')
  delete node.events
  delete node.type
  node.children.forEach(normalizeConditionTree)
  return node
}

function unwrapSingleCondition(node) {
  if (isConditionLeaf(node)) return node
  if (!isObject(node) || !Array.isArray(node.children) || node.children.length !== 1) return null
  return unwrapSingleCondition(node.children[0])
}

export function normalizeRuleDraft(rule) {
  if (!isObject(rule)) return rule
  if (!rule.event && isObject(rule.trigger)) rule.event = rule.trigger
  delete rule.trigger

  if (isObject(rule.condition)) {
    normalizeConditionTree(rule.condition)
    if (isConditionLeaf(rule.condition) && !rule.event) {
      rule.event = rule.condition
      delete rule.condition
    } else if (isObject(rule.event)) {
      const conditionEvent = unwrapSingleCondition(rule.condition)
      if (conditionEvent && valuesEqual(conditionEvent, rule.event)) delete rule.condition
    }
  }
  return rule
}
