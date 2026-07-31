// 插件参数定义工具（规则编辑器使用）

export function getParamDefs(m) {
  if (!m) return []
  if (Array.isArray(m.params)) return m.params
  if (Array.isArray(m.required_params))
    return m.required_params.map(p => ({ name: p, label: p, type: 'string', default: '' }))
  return []
}

export function buildDefaultParams(m) {
  const o = {}
  getParamDefs(m).forEach(p => { o[p.name] = p.default ?? '' })
  return o
}

// options 统一提取 value（兼容字符串和 {value,label} 对象）
export function optValue(o) {
  return typeof o === 'object' && o !== null ? o.value : o
}

// options 统一提取 label（兼容字符串和 {value,label} 对象）
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

// 判断参数是否应该显示（基于 visible_when 条件）
// visible_when 格式: {"param_name": ["value1", "value2"]}，多条件为 AND
export function isVisible(paramDef, currentParams) {
  const vw = paramDef.visible_when
  if (!vw) return true
  for (const [key, vals] of Object.entries(vw)) {
    const raw = currentParams?.[key]
    // 控制参数来自运行数据时，编辑期无法判断 visible_when；全部显示以免丢配置。
    if (raw && typeof raw === 'object' && raw.$ref) continue
    const cur = String(raw ?? '')
    if (!vals.map(String).includes(cur)) return false
  }
  return true
}

// 返回当前应该显示的参数定义（过滤掉 visible_when 不满足的）
export function getVisibleParamDefs(meta, currentParams) {
  return getParamDefs(meta).filter(p => isVisible(p, currentParams))
}

const triggerCategoryMap = {
  manual: '手动', hotkey: '手动', time_schedule: '时间', idle_detect: '时间',
  window_title: '程序', process_state: '程序',
  folder_monitor: '文件与内容', clipboard: '文件与内容', usb_insert: '设备', bluetooth_device: '设备',
  network_status: '网络', power_state: '系统', system_resource: '系统',
}
const triggerCategoryOrder = ['手动', '时间', '程序', '文件与内容', '设备', '网络', '系统', '其他']

export function groupTriggerKeys(keys) {
  const groups = new Map(triggerCategoryOrder.map(name => [name, []]))
  keys.forEach(key => groups.get(triggerCategoryMap[key] || '其他').push(key))
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
