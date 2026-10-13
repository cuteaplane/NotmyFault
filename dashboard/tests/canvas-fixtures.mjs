export const reference = (scope, node, path = []) => ({ $ref: { scope, node, path } })
export const trigger = (id, params = {}, parent) => ({ type: 'trigger', binding_id: `a_${id}`, trigger: { type: 'process', binding_id: `t_${id}`, params }, occurrence: 'occurred', ...(parent ? { parent } : {}) })
export const action = (id, type = 'log', params = {}, parent) => ({ type, binding_id: `a_${id}`, params, ...(parent ? { parent } : {}) })
export const eventGroup = (id, type = 'if', parent) => ({ type: 'event_group', binding_id: `a_${id}`, group_type: type, branches: type === 'if' ? [{ id: `b_${id}_if` }, { id: `b_${id}_elif` }, { id: `b_${id}_else`, else: true }] : [], ...(parent ? { parent } : {}) })
export const wire = (source, target, channel = 'control', port = 'next', target_port = 'input') => ({ source: source.binding_id || source, target: target.binding_id || target, channel, port, target_port })
export const ruleWith = (nodes, edges = [], extras = {}) => ({ rule_id: 'r_fixture001', name: '流程图测试', workflow: { entry: null, nodes, edges }, ...extras })
export const presentation = { eventName: node => node.type, actionName: node => node.type, describeItem: () => '', predicateSummary: node => node.op }
export const schema = { triggers: { process: { name: '进程事件', outputs: [{ name: 'state', value_type: 'text', required: true }] } }, actions: { measure: { name: '测速', outputs: [{ name: 'latency', value_type: 'number', required: true }] }, log: { name: '日志', params: [{ name: 'text', value_type: 'text', type: 'string' }], outputs: [{ name: 'result', value_type: 'text' }] }, pause: { permissions: ['admin'], outputs: [] } }, data_types: { custom: {} } }
export function sequentialRule(items, extra = {}) {
  const start = { type: 'trigger', binding_id: 'a_fixture_start', trigger: { type: 'time_schedule', binding_id: 't_fixture_start', params: { time: '09:00' } }, occurrence: 'occurred' }
  const nodes = [start, ...items.map((item, index) => ({ binding_id: `a_fixture_${index}`, ...item }))]
  return ruleWith(nodes, nodes.slice(0, -1).map((node, index) => wire(node, nodes[index + 1], 'control', index ? 'next' : 'event')), extra)
}
