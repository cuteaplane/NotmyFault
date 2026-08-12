function randomHex() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID().replaceAll('-', '').slice(0, 12)
  return Math.random().toString(16).slice(2, 14).padEnd(12, '0')
}

export function createBindingId(kind) {
  const prefix = kind === 'trigger' ? 't' : kind === 'precondition' ? 'p' : 'a'
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

export function outputDefs(meta) {
  const outputs = Array.isArray(meta?.outputs)
    ? meta.outputs
    : (meta?.outputs && typeof meta.outputs === 'object' ? [meta.outputs] : [])
  return outputs.map(output => (
    typeof output === 'string'
      ? { name: output, label: output, type: 'any', required: true }
      : { required: true, sensitive: false, ...output }
  ))
}

export function collectTriggerLeaves(rule) {
  const leaves = []
  function visit(node) {
    if (!node || typeof node !== 'object') return
    if (node.type && !Array.isArray(node.children) && !Array.isArray(node.events)) {
      leaves.push(node)
      return
    }
    ;(node.children || node.events || []).forEach(visit)
  }
  visit(rule.condition || rule.event)
  return leaves
}

export function guaranteedTriggerIds(node) {
  if (!node || typeof node !== 'object') return new Set()
  if (node.type && !Array.isArray(node.children) && !Array.isArray(node.events)) {
    return node.binding_id ? new Set([node.binding_id]) : new Set()
  }
  const children = node.children || node.events || []
  const sets = children.map(guaranteedTriggerIds)
  if (!sets.length) return new Set()
  if ((node.op || node.type) === 'all' || node.type === 'and') {
    return new Set(sets.flatMap(set => [...set]))
  }
  return new Set([...sets[0]].filter(id => sets.slice(1).every(set => set.has(id))))
}

export function ensureRuleBindingIds(rule) {
  ensureRuleId(rule)
  const seen = new Set()
  function ensureNode(node) {
    if (!node || typeof node !== 'object') return
    if (node.type && !Array.isArray(node.children) && !Array.isArray(node.events)) {
      if (!node.binding_id || seen.has(node.binding_id)) node.binding_id = createBindingId('trigger')
      if (!node.params || typeof node.params !== 'object') node.params = {}
      seen.add(node.binding_id)
      return
    }
    ;(node.children || node.events || []).forEach(ensureNode)
  }
  ensureNode(rule.event)
  ensureNode(rule.condition)
  for (const [field, kind] of [['preconditions', 'precondition'], ['actions', 'action']]) {
    for (const item of rule[field] || []) {
      if (!item.binding_id || seen.has(item.binding_id)) item.binding_id = createBindingId(kind)
      if (!item.params || typeof item.params !== 'object') item.params = {}
      seen.add(item.binding_id)
      if (field === 'actions') {
        const failureActions = Array.isArray(item.failure_actions) ? item.failure_actions : []
        for (const failureAction of failureActions) {
          if (!failureAction.binding_id || seen.has(failureAction.binding_id)) {
            failureAction.binding_id = createBindingId('action')
          }
          if (!failureAction.params || typeof failureAction.params !== 'object') failureAction.params = {}
          seen.add(failureAction.binding_id)
        }
      }
    }
  }
  return rule
}

export function regenerateBindingIds(node, kind = 'trigger') {
  if (!node || typeof node !== 'object') return node
  if (kind === 'trigger') {
    if (node.type && !Array.isArray(node.children) && !Array.isArray(node.events)) {
      node.binding_id = createBindingId('trigger')
      return node
    }
    ;(node.children || node.events || []).forEach(child => regenerateBindingIds(child, 'trigger'))
    return node
  }
  node.binding_id = createBindingId(kind)
  if (kind === 'action') {
    ;(Array.isArray(node.failure_actions) ? node.failure_actions : [])
      .forEach(action => regenerateBindingIds(action, 'action'))
  }
  return node
}

export function normalizedType(type) {
  if (['string', 'textarea', 'path', 'time', 'hotkey', 'select'].includes(type)) return 'string'
  return type || 'any'
}

export function typesCompatible(source, target) {
  source = normalizedType(source)
  target = normalizedType(target)
  return source === 'any' || target === 'any' || source === target
}

function sourceItem(group, label, output, ref, conditional = false) {
  return {
    group,
    label,
    type: output.type || 'any',
    format: output.format,
    value: { $ref: ref },
    sensitive: output.sensitive,
    conditional,
  }
}

export function buildBindingSources(
  rule,
  actionIndex,
  schema,
  { allowSteps = true, allowConditionalTriggers = true } = {},
) {
  const result = []
  const leaves = collectTriggerLeaves(rule)
  const guaranteed = guaranteedTriggerIds(rule.condition || rule.event)

  for (const leaf of leaves) {
    const conditional = !guaranteed.has(leaf.binding_id)
    if (conditional && !allowConditionalTriggers) continue
    const meta = schema.triggers[leaf.type]
    for (const output of outputDefs(meta).filter(item => item.required !== false)) {
      result.push(sourceItem(
        conditional ? '条件分支（仅命中时执行）' : '触发条件',
        `${meta?.name || leaf.type} · ${output.label || output.name}`,
        output,
        { scope: 'trigger', node: leaf.binding_id, path: [output.name] },
        conditional,
      ))
    }
    for (const param of meta?.params || []) {
      result.push(sourceItem(
        conditional ? '条件分支配置（仅命中时执行）' : '触发条件配置',
        `${meta?.name || leaf.type} · 配置·${param.label || param.name}`,
        { type: normalizedType(param.value_type || param.type), format: param.format },
        { scope: 'trigger_config', node: leaf.binding_id, path: [param.name] },
        conditional,
      ))
    }
  }

  if (allowSteps) {
    ;(rule.actions || []).slice(0, Math.max(0, actionIndex)).forEach((action, index) => {
      const meta = schema.actions[action.type]
      for (const output of outputDefs(meta).filter(item => item.required !== false)) {
        result.push(sourceItem(
          '之前的动作',
          `动作 ${index + 1}：${meta?.name || action.type} · ${output.label || output.name}`,
          output,
          { scope: 'step', node: action.binding_id, path: [output.name] },
        ))
      }
    })
  }
  return result
}

export function buildFailureBindingSources(rule, actionIndex, failureIndex, schema) {
  const result = buildBindingSources(rule, actionIndex, schema)
  const configuredActions = rule.actions?.[actionIndex]?.failure_actions
  const failureActions = Array.isArray(configuredActions) ? configuredActions : []
  failureActions.slice(0, Math.max(0, failureIndex)).forEach((action, index) => {
    const meta = schema.actions[action.type]
    for (const output of outputDefs(meta).filter(item => item.required !== false)) {
      result.push(sourceItem(
        '之前的补救动作',
        `补救动作 ${index + 1}：${meta?.name || action.type} · ${output.label || output.name}`,
        output,
        { scope: 'step', node: action.binding_id, path: [output.name] },
      ))
    }
  })
  return result
}

export function buildNodeDataPorts(node, schema) {
  const meta = node.kind === 'trigger'
    ? schema.triggers[node.source?.type]
    : schema.actions[node.source?.type]
  const dataInputs = ['action', 'failure-action', 'precondition'].includes(node.kind)
    ? (meta?.params || []).map((param, index) => ({
        id: `input:${param.name}`,
        index,
        name: param.name,
        label: param.label || param.name,
        type: normalizedType(param.value_type || param.type),
        format: param.format,
      }))
    : []
  const dataOutputs = ['trigger', 'action', 'failure-action'].includes(node.kind)
    ? outputDefs(meta).map((output, index) => ({
        id: `output:${output.name}`,
        index,
        name: output.name,
        label: output.label || output.name,
        type: normalizedType(output.type),
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

export function deriveDataEdges(rule, nodes) {
  const guaranteed = guaranteedTriggerIds(rule.condition || rule.event)
  const nodesByBindingId = new Map(
    nodes
      .filter(node => node.source?.binding_id)
      .map(node => [node.source.binding_id, node]),
  )
  const edges = []
  for (const target of nodes.filter(node => ['action', 'failure-action', 'precondition'].includes(node.kind))) {
    const params = target.source?.params || {}
    for (const [paramName, value] of Object.entries(params)) {
      const targetPort = target.dataInputs.find(port => port.name === paramName)
      for (const [referenceIndex, reference] of collectReferences(value).entries()) {
        if (!['trigger', 'step'].includes(reference.scope)) continue
        const source = nodesByBindingId.get(reference.node)
        const outputName = Array.isArray(reference.path) ? reference.path[0] : null
        const sourcePort = source?.dataOutputs.find(port => port.name === outputName)
        if (!source || !sourcePort || !targetPort) continue
        const sourceIsAction = ['action', 'failure-action'].includes(source.kind)
        const validOrder = !sourceIsAction
          || (target.availableStepIds || []).includes(source.source.binding_id)
        const validCondition = target.kind !== 'precondition'
          || source.kind !== 'trigger'
          || guaranteed.has(source.source.binding_id)
        const valid = sourcePort.required
          && typesCompatible(sourcePort.type, targetPort.type)
          && validOrder
          && validCondition
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
          label: `${sourcePort.type} → ${targetPort.label}`,
          valid,
        })
      }
    }
  }
  return edges
}

export function referenceLabel(value, sources) {
  if (!isReference(value)) return ''
  const serialized = JSON.stringify(value)
  return sources.find(source => JSON.stringify(source.value) === serialized)?.label || '不可用的数据引用'
}

export function collectReferences(value, result = []) {
  if (isReference(value)) {
    result.push(value.$ref)
    return result
  }
  if (Array.isArray(value)) value.forEach(item => collectReferences(item, result))
  else if (value && typeof value === 'object') {
    Object.values(value).forEach(item => collectReferences(item, result))
  }
  return result
}

export function collectLegacyEventPayloadPaths(value, result = []) {
  if (typeof value === 'string') {
    const pattern = /{{\s*event\.payload((?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)\s*}}/g
    for (const match of value.matchAll(pattern)) {
      result.push(match[1].split('.').filter(Boolean))
    }
  } else if (Array.isArray(value)) {
    value.forEach(item => collectLegacyEventPayloadPaths(item, result))
  } else if (value && typeof value === 'object') {
    Object.values(value).forEach(item => collectLegacyEventPayloadPaths(item, result))
  }
  return result
}

function setPath(target, path, value) {
  let current = target
  path.forEach((segment, index) => {
    if (index === path.length - 1) current[segment] = value
    else current = current[segment] ||= {}
  })
}

function parseTestValue(raw, type) {
  if (type === 'number') {
    const value = Number(raw)
    if (!Number.isFinite(value)) throw new Error('请输入有效数字')
    return value
  }
  if (type === 'bool') return ['1', 'true', 'yes', '是'].includes(String(raw).toLowerCase())
  if (type === 'array' || type === 'object') return JSON.parse(raw)
  return raw
}

function testFieldKey(scope, node, path) {
  return `${scope}:${node || 'event'}:${JSON.stringify(path)}`
}

function testDefaultValue(output, fullPayload = false) {
  if (fullPayload) return '{}'
  if (output?.example !== undefined) {
    return ['object', 'array'].includes(normalizedType(output.type))
      ? JSON.stringify(output.example, null, 2)
      : output.example
  }
  const type = normalizedType(output?.type)
  if (type === 'number' || type === 'integer') return 0
  if (type === 'bool' || type === 'boolean') return false
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
  const actions = rule.actions || []
  const startIndex = Math.max(0, actions.findIndex(action => action.binding_id === startStepId))
  const rawEndIndex = actions.findIndex(action => action.binding_id === endStepId)
  const endIndex = rawEndIndex >= 0 ? rawEndIndex : actions.length - 1
  const selectedActions = actions.slice(startIndex, endIndex + 1)
  const executionSource = { preconditions: rule.preconditions || [], actions: selectedActions }
  const references = [...new Map(
    collectReferences(executionSource)
      .filter(reference => ['trigger', 'event', 'step'].includes(reference.scope))
      .map(reference => [JSON.stringify(reference), reference]),
  ).values()]
  const legacyPaths = [...new Map(
    collectLegacyEventPayloadPaths(executionSource).map(path => [JSON.stringify(path), path]),
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
        type: normalizedType(output.type),
        required: output.required !== false,
        sensitive: output.sensitive === true,
        placeholder: output.placeholder || '',
        defaultValue: testDefaultValue(output),
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
    if (reference.scope === 'step' && (source.actionIndex < 0 || source.actionIndex >= startIndex)) continue
    const outputs = outputDefs(source.meta)
    if (reference.scope === 'trigger' && outputs.length) {
      addOutputFields(reference, source, outputs)
      if (!path.length || outputs.some(output => output.name === path[0])) continue
    } else if (!path.length && outputs.length) {
      addOutputFields(reference, source, outputs)
      continue
    }
    const output = outputs.find(item => item.name === path[0])
    const nested = path.length > 1
    addTestField(fields, {
      scope: reference.scope,
      node: ['trigger', 'step'].includes(reference.scope) ? reference.node : '',
      path,
      sourceName: source.name,
      label: output?.label || path.join('.') || '完整数据',
      type: path.length ? (nested ? 'any' : normalizedType(output?.type)) : 'object',
      required: output?.required !== false,
      sensitive: output?.sensitive === true,
      placeholder: output?.placeholder || '',
      defaultValue: testDefaultValue(output, !path.length),
      fullPayload: !path.length,
    })
  }

  for (const path of legacyPaths) {
    addTestField(fields, {
      scope: 'event',
      node: '',
      path,
      sourceName: '本次触发',
      label: path.join('.') || '完整事件数据',
      type: path.length ? 'string' : 'object',
      defaultValue: path.length ? '' : '{}',
      fullPayload: !path.length,
      legacy: true,
    })
  }
  return [...fields.values()]
}

export function buildAllTestInputFields(rule, schema) {
  const fields = new Map()
  const starts = ['', ...(rule.actions || []).map(action => action.binding_id).filter(Boolean)]
  for (const startStepId of starts) {
    for (const field of buildTestInputFields(rule, schema, { startStepId })) {
      if (!fields.has(field.key)) fields.set(field.key, field)
    }
  }
  return [...fields.values()]
}

function parsePreparedTestValue(raw, field) {
  const type = normalizedType(field.type)
  if (field.fullPayload || type === 'object' || type === 'array') {
    let value
    try { value = typeof raw === 'string' ? JSON.parse(raw) : raw }
    catch { throw new Error(type === 'array' ? '请输入有效的 JSON 数组' : '请输入有效的 JSON 对象') }
    if (field.fullPayload || type === 'object') {
      if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('请输入 JSON 对象')
    } else if (!Array.isArray(value)) throw new Error('请输入 JSON 数组')
    return value
  }
  if (type === 'number' || type === 'integer') {
    if (raw === '' || raw === null || raw === undefined) {
      if (!field.required) return undefined
      throw new Error('请输入数字')
    }
    const value = Number(raw)
    if (!Number.isFinite(value)) throw new Error('请输入有效数字')
    return type === 'integer' ? Math.trunc(value) : value
  }
  if (type === 'bool' || type === 'boolean') return raw === true || raw === 'true'
  return String(raw ?? '')
}

export function buildPreparedTestContext(fields, values) {
  const context = { trigger_payloads: {}, event_payload: {}, step_outputs: {} }
  let hasEvent = false
  for (const field of fields) {
    let value
    try { value = parsePreparedTestValue(values[field.key], field) }
    catch (error) {
      error.fieldKey = field.key
      throw error
    }
    if (value === undefined) continue
    const target = field.scope === 'event'
      ? context.event_payload
      : field.scope === 'step'
        ? (context.step_outputs[field.node] ||= {})
        : (context.trigger_payloads[field.node] ||= {})
    if (field.fullPayload) Object.assign(target, value)
    else setPath(target, field.path, value)
    if (field.scope === 'event') hasEvent = true
  }
  if (!hasEvent) delete context.event_payload
  if (!Object.keys(context.step_outputs).length) delete context.step_outputs
  return context
}

export function requestTestContext(rule, schema, promptValue = globalThis.prompt) {
  const leaves = new Map(collectTriggerLeaves(rule).map(leaf => [leaf.binding_id, leaf]))
  const references = [...new Map(
    collectReferences(rule)
      .filter(reference => ['trigger', 'event'].includes(reference.scope))
      .map(reference => [JSON.stringify(reference), reference]),
  ).values()]
  const legacyEventPaths = [...new Map(
    collectLegacyEventPayloadPaths(rule).map(path => [JSON.stringify(path), path]),
  ).values()]
  if (!references.length && !legacyEventPaths.length) return {}
  const context = { trigger_payloads: {}, event_payload: {} }
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
        payloadObject = JSON.parse(raw)
      } catch (error) {
        throw new Error(`${sourceName} · ${label}：请输入有效 JSON`)
      }
      if (!payloadObject || typeof payloadObject !== 'object' || Array.isArray(payloadObject)) {
        throw new Error(`${sourceName} · ${label}：请输入 JSON 对象`)
      }
      if (reference.scope === 'event') {
        Object.assign(context.event_payload, payloadObject)
        promptedEventPaths.add(JSON.stringify(path))
      } else {
        context.trigger_payloads[reference.node] = payloadObject
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
      const payload = context.trigger_payloads[reference.node] ||= {}
      setPath(payload, path, parsed)
    }
  }
  for (const path of legacyEventPaths) {
    const serializedPath = JSON.stringify(path)
    if (promptedEventPaths.has(serializedPath)) continue
    const label = path.length ? path.join('.') : '完整事件数据（JSON）'
    const raw = promptValue(`本次触发 · ${label}`, path.length ? '' : '{}')
    if (raw === null) return null
    let parsed = raw
    if (!path.length) {
      try {
        parsed = JSON.parse(raw)
      } catch (error) {
        throw new Error(`本次触发 · ${label}：请输入有效 JSON`)
      }
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        throw new Error(`本次触发 · ${label}：请输入 JSON 对象`)
      }
      Object.assign(context.event_payload, parsed)
    } else {
      setPath(context.event_payload, path, parsed)
    }
    promptedEventPaths.add(serializedPath)
  }
  if (!references.some(reference => reference.scope === 'event') && !legacyEventPaths.length) {
    delete context.event_payload
  }
  return context
}
