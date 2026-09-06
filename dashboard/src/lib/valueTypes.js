const aliases = { string: 'text', integer: 'int', boolean: 'bool', list: 'array' }
const customTypePattern = /^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+\/[a-zA-Z][a-zA-Z0-9_-]*@[1-9][0-9]*$/
export const typeLabels = { any: '任意数据', null: '空值', text: '文本', int: '整数', float: '浮点数', number: '数字', decimal: '精确小数', bool: '布尔值', date: '日期', time: '时间', datetime: '日期时间', timestamp: '时间戳', duration: '时长', path: '路径', url: '网址', uuid: 'UUID', bytes: '二进制', array: '数组', object: '对象', union: '多种类型' }

export function typeSpec(value = 'any') {
  const spec = typeof value === 'string' ? { type: value } : { ...value }
  spec.type = aliases[spec.type] || spec.type || 'any'
  return spec
}

export function fieldType(field = {}, parameter = false) {
  if (field.value_type) return typeSpec(field.value_type)
  if (['path', 'url', 'date', 'time', 'datetime', 'uuid'].includes(field.format)) return typeSpec(field.format)
  const kind = field.type || 'any'
  if (parameter && ['textarea', 'select', 'path', 'time', 'hotkey', 'string'].includes(kind)) return { type: 'text' }
  if (parameter && kind === 'plugin_data') return { type: 'object' }
  return typeSpec(kind === 'array' ? { type: 'array', items: field.item_type || 'any' } : kind)
}

export function typeLabel(value) {
  const spec = typeSpec(value)
  let label = typeLabels[spec.type] || spec.type
  if (spec.type === 'array') label += `<${typeLabel(spec.items || 'any')}>`
  if (spec.unit) label += spec.unit === 'seconds' ? '（秒）' : '（毫秒）'
  return label + (spec.nullable ? ' / 空值' : '')
}

export function compatibleTypes(source, target) {
  const a = typeSpec(source), b = typeSpec(target)
  if (a.type === 'any' || b.type === 'any') return true
  if (a.type === 'null') return b.type === 'null' || b.nullable === true || b.type === 'union' && b.variants.some(v => compatibleTypes(a, v))
  if (a.nullable && !b.nullable) return compatibleTypes('null', b) && compatibleTypes({ ...a, nullable: false }, b)
  if (a.type === 'union') return a.variants.every(v => compatibleTypes(v, b))
  if (b.type === 'union') return b.variants.some(v => compatibleTypes(a, v))
  if (b.enum && (!a.enum || !a.enum.every(v => b.enum.some(w => typeof w === typeof v && w === v)))) return false
  if (a.type !== b.type) {
    if (a.type === 'text' && ['path', 'url', 'uuid', 'date', 'time', 'datetime'].includes(b.type)) return true
    if (a.type === 'number' && ['int', 'float', 'decimal', 'duration'].includes(b.type)) return true
    if (a.type === 'int' && ['float', 'number', 'decimal'].includes(b.type)) return true
    if (a.type === 'float' && b.type === 'number') return true
    return b.type === 'text' && ['date', 'time', 'datetime', 'path', 'url', 'uuid'].includes(a.type)
  }
  if (['timestamp', 'duration'].includes(a.type) && (a.unit || 'seconds') !== (b.unit || 'seconds')) return false
  if (b.timezone && b.timezone !== 'any' && a.timezone !== b.timezone) return false
  if (b.flavor && b.flavor !== 'any' && a.flavor !== b.flavor) return false
  if (b.schemes && (!a.schemes || !a.schemes.every(scheme => b.schemes.includes(scheme)))) return false
  if (a.type === 'array') return compatibleTypes(a.items || 'any', b.items || 'any')
  if (a.type === 'object') {
    if ((b.required || []).some(key => !(a.required || []).includes(key))) return false
    const properties = a.properties || {}, targetProperties = b.properties || {}
    const extra = a.additional_properties ?? true, targetExtra = b.additional_properties ?? true
    for (const [key, schema] of Object.entries(properties)) {
      const targetSchema = Object.hasOwn(targetProperties, key) ? targetProperties[key] : targetExtra
      if (targetSchema === false || targetSchema !== true && !compatibleTypes(schema, targetSchema)) return false
    }
    if (extra !== false) {
      const extraSchema = extra === true ? 'any' : extra
      for (const [key, schema] of Object.entries(targetProperties)) {
        if (!Object.hasOwn(properties, key) && !compatibleTypes(extraSchema, schema)) return false
      }
      if (targetExtra === false || targetExtra !== true && !compatibleTypes(extraSchema, targetExtra)) return false
    }
  }
  return true
}

export function typeAtPath(value, path, catalog = []) {
  let spec = typeSpec(value), optional = false
  for (const segment of path) {
    optional ||= spec.nullable === true
    if (spec.type.includes('/')) {
      const definition = catalog.find(item => item.id === spec.type)
      if (!definition) throw new Error('插件数据类型未安装')
      if (definition.binding !== 'shared') throw new Error('插件私有数据不能提取字段')
      if (segment === 'data') spec = typeSpec(definition.schema)
      else if (segment === '$type' || segment === 'summary') {
        spec = { type: 'text' }
        optional ||= segment === 'summary'
      } else throw new Error('自定义数据字段须从 data 读取')
    } else if (spec.type === 'array') {
      if (!Number.isInteger(segment) || segment < 0) throw new Error('数组路径需要非负整数下标')
      spec = typeSpec(spec.items); optional = true
    } else if (spec.type === 'object') {
      if (typeof segment !== 'string' || segment.startsWith('_') || ['constructor', 'prototype'].includes(segment)) throw new Error('对象路径字段无效')
      optional ||= !(spec.required || []).includes(segment)
      if (spec.properties?.[segment]) spec = typeSpec(spec.properties[segment])
      else if (spec.additional_properties === false) throw new Error('类型中没有这个字段')
      else spec = typeSpec(spec.additional_properties === true || spec.additional_properties === undefined ? 'any' : spec.additional_properties)
    } else if (spec.type === 'union') {
      const choices = (spec.variants || []).map(item => { try { return typeAtPath(item, [segment], catalog) } catch { return null } })
      const found = choices.filter(Boolean)
      if (!found.length) throw new Error('类型中没有这个字段')
      optional ||= choices.some(item => !item || item.optional)
      spec = { type: 'union', variants: found.map(item => item.type) }
    } else if (spec.type === 'any') optional = true
    else throw new Error('该类型没有子字段')
  }
  return { type: spec, optional }
}

export function isExpression(value) {
  return !!value && !Array.isArray(value) && typeof value === 'object' && Object.keys(value).length === 1 && ['$ref', '$convert', '$template'].includes(Object.keys(value)[0])
}

function hasSingleKey(value, key) {
  return !!value && !Array.isArray(value) && typeof value === 'object' && Object.keys(value).length === 1 && Object.hasOwn(value, key)
}

export function isLiteralValue(value) {
  return hasSingleKey(value, '$literal')
}

export function isEncodedValue(value) {
  return hasSingleKey(value, '$nmf_value')
}

export function isOpaqueValue(value) {
  return isLiteralValue(value) || isEncodedValue(value) || !!value && !Array.isArray(value) && typeof value === 'object'
    && Object.hasOwn(value, 'data') && typeof value.$type === 'string' && customTypePattern.test(value.$type)
}

export function parseExactJson(text) {
  const rewritten = String(text).replace(/"(?:\\.|[^"\\])*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g, token => {
    if (/^-?\d+$/.test(token) && !Number.isSafeInteger(Number(token))) return JSON.stringify({ $nmf_value: { type: 'int', data: token } })
    return token
  })
  return JSON.parse(rewritten)
}

export function parseTypedInput(raw, declaration) {
  const spec = typeSpec(declaration), type = spec.type
  if (type === 'null') return null
  if (spec.nullable && raw === 'null') return null
  if (type === 'bool') {
    if (raw === true || raw === 'true') return true
    if (raw === false || raw === 'false') return false
    throw new Error('布尔值只能是 true 或 false')
  }
  if (type === 'int') {
    if (!/^-?(0|[1-9]\d*)$/.test(String(raw))) throw new Error('请输入整数')
    return Number.isSafeInteger(Number(raw)) ? Number(raw) : { $nmf_value: { type: 'int', data: String(raw) } }
  }
  if (type === 'decimal') {
    if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(String(raw))) throw new Error('请输入精确小数')
    return { $nmf_value: { type: 'decimal', data: String(raw) } }
  }
  if (type === 'bytes') return { $nmf_value: { type: 'bytes', data: String(raw) } }
  if (['number', 'float', 'duration', 'timestamp'].includes(type)) {
    if (String(raw).trim() === '' || !Number.isFinite(Number(raw))) throw new Error('请输入有限数字')
    if (/^-?\d+$/.test(String(raw)) && !Number.isSafeInteger(Number(raw))) {
      if (type === 'float') throw new Error('这个整数无法精确保存为浮点数')
      return parseTypedInput(raw, 'int')
    }
    return Number(raw)
  }
  if (['array', 'object', 'any', 'union'].includes(type) || type.includes('/')) return parseExactJson(raw)
  return String(raw)
}

export function formatTypedInput(value, declaration) {
  if (isLiteralValue(value)) value = value.$literal
  if (value === undefined) return ''
  const type = typeSpec(declaration).type
  if (['array', 'object', 'any', 'union'].includes(type) || type.includes('/')) return JSON.stringify(value, null, 2)
  if (isEncodedValue(value) && value.$nmf_value?.type !== 'object') return value.$nmf_value?.data
  return value === null ? 'null' : String(value)
}

export function defaultTypedValue(declaration) {
  const type = typeSpec(declaration).type
  if (type === 'null') return null
  if (type === 'bool') return false
  if (['number', 'int', 'float', 'duration', 'timestamp'].includes(type)) return 0
  if (type === 'decimal') return { $nmf_value: { type: 'decimal', data: '0' } }
  if (type === 'bytes') return { $nmf_value: { type: 'bytes', data: '' } }
  if (type === 'array') return []
  if (['object', 'any', 'union'].includes(type)) return {}
  return ''
}
