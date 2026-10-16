import { getGraphTriggers, graphExecutionActions, walkGraphActions, findGraphNode, graphPredecessors } from './workflowGraph.js'
import { compatibleTypes, fieldType, typeSpec, typeLabel, typeAtPath, isOpaqueValue, isExpression, parseExactJson, parseTypedInput, defaultTypedValue, formatTypedInput } from './valueTypes.js'
import { validateRuleGraph } from './utils.js'

function randomHex() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID().replaceAll('-', '').slice(0, 12)
  return Math.random().toString(16).slice(2, 14).padEnd(12, '0')
}

export function createBindingId(kind) {
  const prefix = { trigger: 't', constant: 'c', variable: 'v' }[kind] || 'a'
  return `${prefix}_${randomHex()}`
}

export function createRuleId() {
  return `r_${randomHex()}`
}

export function ensureRuleId(rule, seen = new Set()) {
  if (!rule || typeof rule !== 'object') return rule
  if (!/^r_[a-z0-9_]{6,64}$/.test(rule.rule_id || '') || seen.has(rule.rule_id)) {
    rule.rule_id = createRuleId()
  }
  seen.add(rule.rule_id)
  return rule
}

export function ensureRuleIds(rules) {
  const seen = new Set()
  for (const rule of rules || []) ensureRuleId(rule, seen)
  return rules
}

export function isReference(value) {
  return !!value
    && typeof value === 'object'
    && !Array.isArray(value)
    && Object.keys(value).length === 1
    && value.$ref
    && typeof value.$ref === 'object'
}

// 插件清单的 outputs 由服务器保证是对象数组，非法条目在这里跳过。
export function outputDefs(meta) {
  if (!Array.isArray(meta?.outputs)) return []
  return meta.outputs
    .filter(output => output && typeof output === 'object' && !Array.isArray(output))
    .map(output => ({ required: true, sensitive: false, ...output }))
}

export function collectTriggerLeaves(rule, includeAbsence = true) {
  return (rule.workflow?.nodes || []).filter(node => node.type === 'trigger' && (includeAbsence || node.occurrence !== 'absent')).map(node => node.trigger)
}


export function ensureRuleBindingIds(rule) {
  validateRuleGraph(rule); ensureRuleId(rule)
  walkGraphActions(rule.workflow, node => { node.binding_id ||= createBindingId('action'); if (node.type === 'trigger') node.trigger.binding_id ||= createBindingId('trigger') })
  return rule
}

export function regenerateBindingIds(node, kind = 'trigger') {
  const replacements = new Map()
  function visit(item, itemKind) {
    if (!item || typeof item !== 'object') return
    const previous = item.binding_id
    item.binding_id = createBindingId(itemKind)
    if (previous) replacements.set(previous, item.binding_id)
    if (itemKind === 'action') {
      if (item.type === 'trigger') visit(item.trigger, 'trigger')
    }
  }
  visit(node, kind)
  for (const reference of collectReferences(node)) {
    if (replacements.has(reference.node)) reference.node = replacements.get(reference.node)
  }
  return node
}

export function normalizedType(type) {
  if (['textarea', 'hotkey', 'select'].includes(type)) return 'text'
  return typeSpec(type).type
}

export function typesCompatible(source, target) {
  return compatibleTypes(source, target)
}

function sourceItem(group, label, output, ref, conditional = false) {
  return {
    group,
    label,
    type: fieldType(output),
    format: output.format,
    value: { $ref: ref },
    sensitive: output.sensitive,
    conditional,
    optional: output.required === false,
  }
}

export function variableBindingSources(rule, { constantsOnly = false } = {}) {
  return ['constant', ...(constantsOnly ? [] : ['variable'])].flatMap(scope => (
    (rule[scope === 'constant' ? 'constants' : 'variables'] || []).map(item => sourceItem(
      scope === 'constant' ? '规则常量' : '运行变量', item.name,
      { value_type: item.value_type, sensitive: item.sensitive, required: scope === 'constant' || Object.hasOwn(item, 'initial') },
      { scope, node: item.id, path: [] },
    ))
  ))
}

export function actionOutputDefs(action, schema, rule = {}, seen = new Set()) {
  if (action?.type === 'action_group') {
    if (seen.has(action.binding_id)) return []
    const nextSeen = new Set([...seen, action.binding_id])
    const catalog = Object.entries(schema.data_types?.custom || {}).map(([id, definition]) => ({ id, ...definition }))
    function info(value) {
      if (isReference(value)) {
        const ref = value.$ref, path = ref.path || []
        let output, root
        if (['constant', 'variable'].includes(ref.scope)) {
          const definition = rule[ref.scope === 'constant' ? 'constants' : 'variables']?.find(item => item.id === ref.node)
          output = definition; root = definition?.value_type || 'any'
        } else {
          const source = ref.scope === 'step' ? findGraphNode(rule, ref.node)?.action : collectTriggerLeaves(rule).find(item => item.binding_id === ref.node)
          const definitions = ref.scope === 'step' ? actionOutputDefs(source, schema, rule, nextSeen) : ref.scope === 'trigger_config' ? schema.triggers?.[source?.type]?.params || [] : outputDefs(schema.triggers?.[source?.type])
          output = definitions.find(item => item.name === path[0])
          if (path.length) root = fieldType(output, ref.scope === 'trigger_config')
          else { root = { type: 'object', properties: Object.fromEntries(definitions.map(output => [output.name, fieldType(output)])), required: definitions.filter(output => output.required !== false).map(output => output.name) }; output = { sensitive: definitions.some(output => output.sensitive === true) } }
        }
        let selected = root
        try { selected = typeAtPath(root, ['constant', 'variable'].includes(ref.scope) ? path : path.slice(1), catalog).type } catch { selected = 'any' }
        return { type: selected || 'any', sensitive: output?.sensitive === true, required: ref.on_missing !== 'skip' }
      }
      if (value?.$convert) return { ...info(value.$convert.value), type: typeSpec(value.$convert.to) }
      if (value?.$template) return { type: 'text', sensitive: collectReferences(value).some(reference => info({ $ref: reference }).sensitive), required: true }
      if (value?.$nmf_value) return { type: value.$nmf_value.type, sensitive: false, required: true }
      if (value && Object.hasOwn(value, '$literal')) return info(value.$literal)
      return { type: value === null ? 'null' : Array.isArray(value) ? { type: 'array', items: 'any' } : typeof value === 'boolean' ? 'bool' : typeof value === 'number' ? 'number' : typeof value === 'string' ? 'text' : 'object', sensitive: false, required: true }
    }
    return Object.entries(action.exports || {}).map(([name, expression]) => { const output = info(expression); return { name, label: name, value_type: output.type, required: output.required, sensitive: output.sensitive } })
  }
  if (action?.type === 'set_variable') {
    const variable = rule.variables?.find(item => item.id === action.variable)
    return [{ name: 'value', label: '赋值结果', value_type: variable?.value_type || 'any', required: true, sensitive: variable?.sensitive === true }]
  }
  return outputDefs(schema.actions?.[action?.type])
}

export function expandBindingSources(sources, catalog = {}) {
  const result = []
  function visit(source, depth) {
    result.push(source)
    if (depth >= 8) return
    const spec = typeSpec(source.type)
    if (spec.type === 'object') {
      for (const [key, child] of Object.entries(spec.properties || {})) {
        if (key.startsWith('_') || ['constructor', 'prototype'].includes(key)) continue
        visit({ ...source, label: `${source.label} · ${key}`, type: typeSpec(child), optional: source.optional || !(spec.required || []).includes(key), value: { $ref: { ...source.value.$ref, path: [...source.value.$ref.path, key] } } }, depth + 1)
      }
    } else if (spec.type.includes('/') && catalog[spec.type]?.binding === 'shared') {
      visit({ ...source, label: `${source.label} · 数据`, type: catalog[spec.type].schema, value: { $ref: { ...source.value.$ref, path: [...source.value.$ref.path, 'data'] } } }, depth + 1)
    }
  }
  sources.forEach(source => visit(source, 0))
  return result
}

export function graphBindingSources(rule, action, schema) {
  const hit = findGraphNode(rule, action?.binding_id)
  const result = buildBindingSources(rule, 0, schema, { allowSteps: false })
  const scopes = hit ? [...hit.trail.filter(parent => parent.field === 'workflow').map(parent => ({ ...findGraphNode(rule, parent.action.binding_id), action: parent.action })), hit] : []
  if (['condition', 'logic', 'trigger'].includes(action?.type) || action?.type === 'event_group') {
    const seen = new Set([action.binding_id]), pending = [action.binding_id]
    while (pending.length) {
      const identity = pending.pop()
      for (const edge of rule.workflow.edges) if (edge.channel === 'condition' && edge.source === identity && !seen.has(edge.target)) {
        seen.add(edge.target); pending.push(edge.target)
        const consumer = findGraphNode(rule, edge.target)
        if (consumer) scopes.push(consumer)
      }
    }
  }
  for (const scope of scopes) {
    for (const id of graphPredecessors(scope.workflow, scope.action.binding_id)) {
      const previous = scope.workflow.nodes.find(node => node.binding_id === id)
      if (!previous) continue
      for (const output of actionOutputDefs(previous, schema, rule)) result.push(sourceItem('前序动作输出', `${schema.actions[previous.type]?.name || previous.name || previous.type} · ${output.label || output.name}`, output, { scope: 'step', node: id, path: [output.name] }, true))
    }
  }
  return expandBindingSources([...new Map(result.map(source => [JSON.stringify(source.value.$ref), source])).values()], schema.data_types?.custom)
}

export function buildBindingSources(
  rule,
  actionIndex,
  schema,
  { allowSteps = true, allowConditionalTriggers = true } = {},
) {
  const result = variableBindingSources(rule)
  const leaves = collectTriggerLeaves(rule, false)
  const guaranteed = new Set()

  for (const leaf of leaves) {
    const conditional = !guaranteed.has(leaf.binding_id)
    if (conditional && !allowConditionalTriggers) continue
    const meta = schema.triggers[leaf.type]
    for (const output of outputDefs(meta)) {
      result.push(sourceItem(
        conditional ? '条件分支（仅命中时执行）' : '触发条件',
        `${meta?.name || leaf.type} · ${output.label || output.name}`,
        output,
        { scope: 'trigger', node: leaf.binding_id, path: [output.name] },
        conditional,
      ))
    }
    for (const param of meta?.params || []) {
      if (param.type === 'plugin_data') continue
      result.push(sourceItem(
        conditional ? '条件分支配置（仅命中时执行）' : '触发条件配置',
        `${meta?.name || leaf.type} · 配置·${param.label || param.name}`,
        { value_type: fieldType(param, true), format: param.format, sensitive: param.sensitive },
        { scope: 'trigger_config', node: leaf.binding_id, path: [param.name] },
        conditional,
      ))
    }
  }

  if (allowSteps) {
    ;graphPredecessors(rule.workflow, rule.workflow?.nodes[actionIndex]?.binding_id).map(id => rule.workflow.nodes.find(node => node.binding_id === id)).filter(Boolean).forEach((action, index) => {
      const meta = schema.actions[action.type]
      for (const output of actionOutputDefs(action, schema, rule)) {
        result.push(sourceItem(
          '之前的动作',
          `动作 ${index + 1}：${meta?.name || action.type} · ${output.label || output.name}`,
          output,
          { scope: 'step', node: action.binding_id, path: [output.name] },
        ))
      }
    })
  }
  return expandBindingSources(result, schema.data_types?.custom)
}

export function parameterAllowsBinding(meta, paramName) {
  if (meta?.params?.find(item => item.name === paramName)?.type === 'plugin_data') return false
  const fixedParams = meta?.security?.literal_only_params
  return !Array.isArray(fixedParams) || !fixedParams.includes(paramName)
}

export function buildNodeDataPorts(node, schema, rule = {}) {
  const meta = node.source?.type === 'action_group' ? { outputs: actionOutputDefs(node.source, schema, rule) } : node.source?.type === 'set_variable'
    ? { params: [{ name: 'value', label: '赋值内容', type: 'string', value_type: rule.variables?.find(item => item.id === node.source.variable)?.value_type || 'any' }], outputs: actionOutputDefs(node.source, schema, rule) }
    : node.kind === 'trigger'
    ? schema.triggers[node.source?.type]
    : schema.actions[node.source?.type]
  const dataInputs = ['action'].includes(node.kind)
    ? (meta?.params || [])
      .filter(param => parameterAllowsBinding(meta, param.name))
      .map((param, index) => ({
        id: `input:${param.name}`,
        index,
        name: param.name,
        label: param.label || param.name,
        type: fieldType(param, true),
        format: param.format,
      }))
    : []
  const dataOutputs = !node.negated && ['trigger', 'action', 'action_group'].includes(node.kind)
    ? outputDefs(meta).map((output, index) => ({
        id: `output:${output.name}`,
        index,
        name: output.name,
        label: output.label || output.name,
        type: fieldType(output),
        format: output.format,
        required: output.required !== false,
        sensitive: output.sensitive,
      }))
    : []
  return {
    ...node,
    dataInputs,
    dataOutputs,
    dataPortRows: Math.max(dataInputs.length, dataOutputs.length),
  }
}

export function deriveDataEdges(rule, nodes, catalog = []) {
  const nodesByBindingId = new Map(
    nodes
      .filter(node => node.source?.binding_id && node.dataOutputs?.length)
      .map(node => [node.source.binding_id, node]),
  )
  const edges = []
  for (const target of nodes.filter(node => ['action', 'failure-action', 'predicate'].includes(node.kind))) {
    const params = target.kind === 'predicate' ? Object.fromEntries(['left', 'right'].filter(key => Object.hasOwn(target.source, key)).map(key => [key, target.source[key]])) : target.source?.type === 'set_variable' ? { value: target.source.value } : target.source?.params || {}
    for (const [paramName, value] of Object.entries(params)) {
      const targetPort = target.dataInputs.find(port => port.name === paramName)
      for (const [referenceIndex, reference] of collectReferences(value).entries()) {
        if (!['trigger', 'step'].includes(reference.scope)) continue
        const source = nodesByBindingId.get(reference.node)
        const outputName = Array.isArray(reference.path) ? reference.path[0] : null
        const sourcePort = source?.dataOutputs.find(port => port.name === outputName)
        if (!source || !sourcePort || !targetPort) continue
        const sourceIsAction = ['action', 'failure-action', 'action_group'].includes(source.kind)
        const validOrder = !sourceIsAction
          || (target.availableStepIds || []).includes(source.source.binding_id)
        let selectedType = sourcePort.type
        try { selectedType = typeAtPath(sourcePort.type, reference.path.slice(1), catalog).type } catch {}
        const valid = (sourcePort.required || reference.on_missing)
          && (isExpression(value) && !isReference(value) || typesCompatible(selectedType, targetPort.type))
          && validOrder
        edges.push({
          id: `data:${reference.node}:${outputName}:${target.source.binding_id}:${paramName}:${referenceIndex}`,
          channel: 'data',
          kind: valid ? 'data' : 'data-invalid',
          from: source.id,
          to: target.id,
          sourcePortName: sourcePort.name,
          targetPortName: targetPort.name,
          sourcePortIndex: sourcePort.index,
          targetPortIndex: targetPort.index,
          label: `${typeLabel(selectedType)} → ${targetPort.label}`,
          valid,
        })
      }
    }
  }
  return edges
}

export function referenceLabel(value, sources) {
  if (value?.$convert) return `转换为${typeLabel(value.$convert.to)} · ${referenceLabel(value.$convert.value, sources)}`
  if (value?.$template) return '文本模板'
  if (!isReference(value)) return ''
  const ref = value.$ref
  const found = sources.find(source => source.value.$ref.scope === ref.scope && source.value.$ref.node === ref.node && JSON.stringify(source.value.$ref.path) === JSON.stringify(ref.path))
  if (found) return found.label
  const root = sources.find(source => source.value.$ref.scope === ref.scope && source.value.$ref.node === ref.node)
  return root ? `${root.label} · ${JSON.stringify(ref.path)}` : '不可用的数据引用'
}

export function bindingSourceFor(reference, sources, catalog = []) {
  const source = sources.filter(item => item.value.$ref.scope === reference.scope && item.value.$ref.node === reference.node && item.value.$ref.path.every((segment, index) => segment === reference.path?.[index])).sort((a, b) => b.value.$ref.path.length - a.value.$ref.path.length)[0]
  if (!source) return null
  try { return { ...source, ...typeAtPath(source.type, reference.path.slice(source.value.$ref.path.length), catalog) } } catch { return null }
}

export function collectReferences(value, result = []) {
  if (isOpaqueValue(value)) return result
  if (isReference(value)) {
    result.push(value.$ref)
    if (value.$ref.on_missing === 'default') collectReferences(value.$ref.default, result)
    return result
  }
  if (Array.isArray(value)) value.forEach(item => collectReferences(item, result))
  else if (value && typeof value === 'object') {
    Object.values(value).forEach(item => collectReferences(item, result))
  }
  return result
}

const UNSAFE_CONTEXT_KEYS = new Set(['__proto__', 'constructor', 'prototype'])

function assertSafeContextKey(key) {
  if (typeof key !== 'string' || !key || UNSAFE_CONTEXT_KEYS.has(key)) {
    throw new Error('规则引用包含不安全的字段名')
  }
}

function contextRecord() { return Object.create(null) }

function safeContextValue(value) {
  if (Array.isArray(value)) return value.map(safeContextValue)
  if (!value || typeof value !== 'object') return value
  const result = contextRecord()
  for (const [key, item] of Object.entries(value)) {
    assertSafeContextKey(key)
    result[key] = safeContextValue(item)
  }
  return result
}

function assignContextRecord(target, value) {
  for (const [key, item] of Object.entries(value)) {
    assertSafeContextKey(key)
    target[key] = safeContextValue(item)
  }
}

function contextBucket(target, key) {
  assertSafeContextKey(key)
  if (!Object.prototype.hasOwnProperty.call(target, key)) target[key] = contextRecord()
  return target[key]
}

function setPath(target, path, value) {
  if (!Array.isArray(path) || !path.length) throw new Error('规则引用路径无效')
  let current = target
  path.forEach((segment, index) => {
    if (!Number.isInteger(segment)) assertSafeContextKey(segment)
    else if (segment < 0) throw new Error('数组下标不能为负数')
    if (index === path.length - 1) current[segment] = safeContextValue(value)
    else {
      if (!Object.prototype.hasOwnProperty.call(current, segment)) current[segment] = Number.isInteger(path[index + 1]) ? [] : contextRecord()
      current = current[segment]
    }
  })
}

function parseTestValue(raw, type) {
  if (type === 'number') {
    const value = Number(raw)
    if (!Number.isFinite(value)) throw new Error('请输入有效数字')
    return value
  }
  if (type === 'bool') return ['1', 'true', 'yes', '是'].includes(String(raw).toLowerCase())
  if (type === 'array' || type === 'object') return parseExactJson(raw)
  return raw
}

function testFieldKey(scope, node, path) {
  return `${scope}:${node || 'event'}:${JSON.stringify(path)}`
}

function testDefaultValue(output, fullPayload = false) {
  if (fullPayload) return '{}'
  if (output?.value_type) return formatTypedInput(output.example ?? defaultTypedValue(output.value_type), output.value_type)
  if (output?.example !== undefined) {
    return ['object', 'array'].includes(normalizedType(output.type))
      ? JSON.stringify(output.example, null, 2)
      : output.example
  }
  const type = normalizedType(output?.type)
  if (['number', 'int', 'float'].includes(type)) return 0
  if (type === 'bool') return false
  if (type === 'array') return '[]'
  if (type === 'object') return '{}'
  return ''
}

function addTestField(fields, field) {
  const key = testFieldKey(field.scope, field.node, field.path)
  if (fields.has(key)) return
  fields.set(key, {
    key,
    required: true,
    sensitive: false,
    type: 'string',
    ...field,
  })
}

export function buildTestInputFields(rule, schema, { startStepId = '', endStepId = '' } = {}) {
  const leaves = collectTriggerLeaves(rule)
  const leavesById = new Map(leaves.map(leaf => [leaf.binding_id, leaf]))
  const actions = []; walkGraphActions(rule.workflow, action => actions.push(action))
  const selectedActions = graphExecutionActions(rule, { startStepId, endStepId })
  const dependencies = new Set(selectedActions.map(action => action.binding_id))
  const pending = [...dependencies]
  while (pending.length) { const target = pending.pop(); for (const edge of rule.workflow.edges) if (edge.channel === 'condition' && edge.target === target && !dependencies.has(edge.source)) { dependencies.add(edge.source); pending.push(edge.source) } }
  const executionSource = { nodes: actions.filter(action => dependencies.has(action.binding_id)) }
  const references = [...new Map(
    collectReferences(executionSource)
      .filter(reference => ['trigger', 'event', 'step'].includes(reference.scope))
      .map(reference => [JSON.stringify(reference), reference]),
  ).values()]
  const fields = new Map()

  function addOutputFields(reference, source, outputs) {
    for (const output of outputs) {
      addTestField(fields, {
        scope: reference.scope,
        node: ['trigger', 'step'].includes(reference.scope) ? reference.node : '',
        path: [output.name],
        sourceName: source.name,
        label: output.label || output.name,
        type: fieldType(output).type,
        valueType: fieldType(output),
        required: output.required !== false && !['skip', 'default'].includes(reference.on_missing),
        sensitive: output.sensitive === true,
        placeholder: output.placeholder || '',
        defaultValue: output.required === false || ['skip', 'default'].includes(reference.on_missing) ? '' : testDefaultValue(output),
      })
    }
  }

  function sourceFor(reference) {
    if (reference.scope === 'step') {
      const actionIndex = actions.findIndex(action => action.binding_id === reference.node)
      const action = actions[actionIndex]
      const meta = action ? schema.actions?.[action.type] : null
      return {
        action,
        actionIndex,
        meta,
        name: `上游结果 · 动作 ${actionIndex + 1}：${meta?.name || action?.type || reference.node}`,
      }
    }
    const leaf = reference.scope === 'trigger'
      ? leavesById.get(reference.node)
      : (leaves.length === 1 ? leaves[0] : null)
    const meta = leaf ? schema.triggers?.[leaf.type] : null
    return {
      leaf,
      meta,
      name: reference.scope === 'event'
        ? '本次触发'
        : (meta?.name || leaf?.type || reference.node || '触发器'),
    }
  }

  for (const reference of references) {
    const path = Array.isArray(reference.path) ? reference.path : []
    const source = sourceFor(reference)
    if (reference.scope === 'step' && (source.actionIndex < 0 || selectedActions.some(action => action.binding_id === reference.node))) continue
    const outputs = reference.scope === 'step' ? actionOutputDefs(source.action, schema, rule) : outputDefs(source.meta)
    if (reference.scope === 'trigger' && outputs.length) {
      addOutputFields(reference, source, outputs)
      if (!path.length || outputs.some(output => output.name === path[0])) continue
    } else if (!path.length && outputs.length) {
      addOutputFields(reference, source, outputs)
      continue
    }
    const output = outputs.find(item => item.name === path[0])
    const selectedType = !path.length ? { type: 'object' } : typeAtPath(fieldType(output), path.slice(1), Object.entries(schema.data_types?.custom || {}).map(([id, definition]) => ({ id, ...definition }))).type
    addTestField(fields, {
      scope: reference.scope,
      node: ['trigger', 'step'].includes(reference.scope) ? reference.node : '',
      path,
      sourceName: source.name,
      label: output?.label || path.join('.') || '完整数据',
      type: selectedType.type,
      valueType: selectedType.type === 'any' ? undefined : selectedType,
      required: output?.required !== false && !['skip', 'default'].includes(reference.on_missing),
      sensitive: output?.sensitive === true,
      placeholder: output?.placeholder || '',
      defaultValue: output?.required === false || ['skip', 'default'].includes(reference.on_missing) ? '' : testDefaultValue({ value_type: selectedType }, !path.length),
      fullPayload: !path.length,
    })
  }

  for (const node of executionSource.nodes) {
    if (node.type !== 'trigger') continue
    const leaf = leavesById.get(node.trigger.binding_id)
    const meta = schema.triggers?.[leaf.type]
    const name = `触发 ${leaves.indexOf(leaf) + 1} · ${meta?.name || leaf.type}`
    addTestField(fields, { scope: 'event_occurrence', node: leaf.binding_id, path: [], sourceName: name,
      label: '模拟事件已发生', type: 'bool', valueType: 'bool', defaultValue: node.occurrence !== 'absent' })
    addOutputFields({ scope: 'trigger', node: leaf.binding_id }, { name }, outputDefs(meta))
    for (const field of fields.values()) if (field.scope === 'trigger' && field.node === leaf.binding_id) field.sourceName = name
    for (const output of outputDefs(meta)) {
      if (output.required === false || node.occurrence === 'absent') continue
      const field = fields.get(testFieldKey('trigger', leaf.binding_id, [output.name]))
      field.required = true
      field.defaultValue = testDefaultValue(output)
    }
  }

  for (const variable of rule.variables || []) {
    addTestField(fields, { scope: 'variable', node: variable.id, path: [], sourceName: '运行变量（留空使用规则初始值）', label: variable.name, type: normalizedType(variable.value_type), valueType: variable.value_type, required: false, sensitive: variable.sensitive === true, defaultValue: '' })
  }
  return [...fields.values()]
}

export function buildAllTestInputFields(rule, schema) {
  const fields = new Map()
  const starts = ['']; walkGraphActions(rule.workflow, action => starts.push(action.binding_id))
  for (const startStepId of starts) {
    for (const field of buildTestInputFields(rule, schema, { startStepId })) {
      if (!fields.has(field.key)) fields.set(field.key, field)
    }
  }
  return [...fields.values()]
}

function parsePreparedTestValue(raw, field) {
  if (!field.required && (raw === '' || raw === undefined)) return undefined
  if (field.valueType) return parseTypedInput(raw, field.valueType)
  const type = normalizedType(field.type)
  if (field.fullPayload || type === 'object' || type === 'array') {
    let value
    try { value = typeof raw === 'string' ? parseExactJson(raw) : raw }
    catch { throw new Error(type === 'array' ? '请输入有效的 JSON 数组' : '请输入有效的 JSON 对象') }
    if (field.fullPayload || type === 'object') {
      if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('请输入 JSON 对象')
    } else if (!Array.isArray(value)) throw new Error('请输入 JSON 数组')
    return value
  }
  if (['number', 'int', 'float', 'decimal', 'timestamp', 'duration'].includes(type)) {
    if (raw === '' || raw === null || raw === undefined) {
      if (!field.required) return undefined
      throw new Error('请输入数字')
    }
    return parseTypedInput(raw, field.type)
  }
  if (type === 'bool') return raw === true || raw === 'true'
  return String(raw ?? '')
}

export function buildPreparedTestContext(fields, values) {
  const context = {
    trigger_payloads: contextRecord(),
    event_payload: contextRecord(),
    step_outputs: contextRecord(),
    variable_values: contextRecord(),
  }
  let hasEvent = false
  const absentTriggers = new Set()
  for (const field of fields) {
    let value
    try { value = parsePreparedTestValue(values[field.key], field) }
    catch (error) {
      error.fieldKey = field.key
      throw error
    }
    if (value === undefined) continue
    if (field.scope === 'event_occurrence') {
      if (value === true) contextBucket(context.trigger_payloads, field.node)
      else absentTriggers.add(field.node)
      continue
    }
    if (field.scope === 'variable') { context.variable_values[field.node] = safeContextValue(value); continue }
    const target = field.scope === 'event'
      ? context.event_payload
      : field.scope === 'step'
        ? contextBucket(context.step_outputs, field.node)
        : contextBucket(context.trigger_payloads, field.node)
    if (field.fullPayload) assignContextRecord(target, value)
    else setPath(target, field.path, value)
    if (field.scope === 'event') hasEvent = true
  }
  for (const node of absentTriggers) delete context.trigger_payloads[node]
  if (!hasEvent) delete context.event_payload
  if (!Object.keys(context.step_outputs).length) delete context.step_outputs
  if (!Object.keys(context.variable_values).length) delete context.variable_values
  return context
}

export function requestTestContext(rule, schema, promptValue = globalThis.prompt) {
  const leaves = new Map(collectTriggerLeaves(rule).map(leaf => [leaf.binding_id, leaf]))
  const references = [...new Map(
    collectReferences(rule)
      .filter(reference => ['trigger', 'event'].includes(reference.scope))
      .map(reference => [JSON.stringify(reference), reference]),
  ).values()]
  if (!references.length) return {}
  const context = {
    trigger_payloads: contextRecord(),
    event_payload: contextRecord(),
  }
  const promptedEventPaths = new Set()

  for (const reference of references) {
    const path = Array.isArray(reference.path) ? reference.path : []
    const fullPayload = path.length === 0
    const leaf = leaves.get(reference.node)
    const meta = leaf ? schema.triggers[leaf.type] : null
    const output = outputDefs(meta).find(item => item.name === path[0])
    const label = output?.label || path.join('.') || '完整数据'
    const sourceName = reference.scope === 'event'
      ? '本次触发'
      : (meta?.name || leaf?.type || reference.node)
    const defaultValue = fullPayload ? '{}' : output?.example ?? (
      output?.type === 'number' ? 0
        : output?.type === 'bool' ? false
          : output?.type === 'array' ? '[]'
            : output?.type === 'object' ? '{}'
              : ''
    )
    const raw = promptValue(`${sourceName} · ${label}`, String(defaultValue))
    if (raw === null) return null
    // 空 path 代表完整 payload，后端的 trigger_payloads 和 event_payload 都是对象，必须整体写入。
    if (fullPayload) {
      let payloadObject
      try {
        payloadObject = parseExactJson(raw)
      } catch (error) {
        throw new Error(`${sourceName} · ${label}：请输入有效 JSON`)
      }
      if (!payloadObject || typeof payloadObject !== 'object' || Array.isArray(payloadObject)) {
        throw new Error(`${sourceName} · ${label}：请输入 JSON 对象`)
      }
      if (reference.scope === 'event') {
        assignContextRecord(context.event_payload, payloadObject)
        promptedEventPaths.add(JSON.stringify(path))
      } else {
        const payload = contextBucket(context.trigger_payloads, reference.node)
        assignContextRecord(payload, payloadObject)
      }
      continue
    }
    let parsed
    try {
      parsed = parseTestValue(raw, output?.type)
    } catch (error) {
      throw new Error(`${sourceName} · ${label}：${error.message}`)
    }
    if (reference.scope === 'event') {
      setPath(context.event_payload, path, parsed)
      promptedEventPaths.add(JSON.stringify(path))
    }
    else {
      const payload = contextBucket(context.trigger_payloads, reference.node)
      setPath(payload, path, parsed)
    }
  }
  if (!references.some(reference => reference.scope === 'event')) {
    delete context.event_payload
  }
  return context
}
