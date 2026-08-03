function randomHex() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID().replaceAll('-', '').slice(0, 12)
  return Math.random().toString(16).slice(2, 14).padEnd(12, '0')
}

export function createBindingId(kind) {
  const prefix = kind === 'trigger' ? 't' : kind === 'precondition' ? 'p' : 'a'
  return `${prefix}_${randomHex()}`
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
  return (meta?.outputs || []).map(output => (
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

export function buildNodeDataPorts(node, schema) {
  const meta = node.kind === 'trigger'
    ? schema.triggers[node.source?.type]
    : schema.actions[node.source?.type]
  const dataInputs = ['action', 'precondition'].includes(node.kind)
    ? (meta?.params || []).map((param, index) => ({
        id: `input:${param.name}`,
        index,
        name: param.name,
        label: param.label || param.name,
        type: normalizedType(param.value_type || param.type),
        format: param.format,
      }))
    : []
  const dataOutputs = ['trigger', 'action'].includes(node.kind)
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
  for (const target of nodes.filter(node => ['action', 'precondition'].includes(node.kind))) {
    const params = target.source?.params || {}
    for (const [paramName, value] of Object.entries(params)) {
      const targetPort = target.dataInputs.find(port => port.name === paramName)
      for (const [referenceIndex, reference] of collectReferences(value).entries()) {
        if (!['trigger', 'step'].includes(reference.scope)) continue
        const source = nodesByBindingId.get(reference.node)
        const outputName = Array.isArray(reference.path) ? reference.path[0] : null
        const sourcePort = source?.dataOutputs.find(port => port.name === outputName)
        if (!source || !sourcePort || !targetPort) continue
        const validOrder = source.kind !== 'action'
          || (target.kind === 'action' && source.index < target.index)
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
