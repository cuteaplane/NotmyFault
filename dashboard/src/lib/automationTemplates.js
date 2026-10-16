import { createBindingId, ensureRuleBindingIds } from './bindings.js'
import { buildDefaultParams, pluginUnavailableReason } from './utils.js'

function clone(value) {
  return JSON.parse(JSON.stringify(value))
}

import { createGraphAction, createTriggerStep } from './workflowGraph.js'

function connectedRule(name, folder, trigger, actions) {
  const entry = createTriggerStep(trigger), nodes = [entry, ...actions.map(action => createGraphAction(action.type, action.params))]
  nodes.forEach((node, index) => { node.position = { x: 64 + index * 340, y: 118 } })
  return { name, folder, workflow: { entry: null, nodes, edges: nodes.slice(0, -1).map((node, index) => ({ source: node.binding_id, port: index ? 'next' : 'event', target: nodes[index + 1].binding_id, channel: 'control' })) } }
}
export const automationTemplates = [
  { id: 'usb-backup', icon: 'usb', title: 'U盘插入后备份文件', description: '检测到任意 U 盘后，把盘内文件复制到你指定的备份目录。', triggerType: 'usb_insert', actionTypes: ['file_operation'], createRule() {
    const triggerId = createBindingId('trigger')
    return connectedRule('U盘插入后备份文件', '文件自动化', { binding_id: triggerId, type: 'usb_insert', params: { drive_letter: 'ANY' } }, [{ type: 'file_operation', params: { operation: 'copy', source: { $ref: { scope: 'trigger', node: triggerId, path: ['actual_drive'] } }, destination: '' } }])
  } },
  { id: 'folder-change-notify', icon: 'folder_open', title: '文件夹有新文件时通知我', description: '监控一个目录；出现新文件时，通知里直接显示文件路径。', triggerType: 'folder_monitor', actionTypes: ['notify'], createRule() {
    const triggerId = createBindingId('trigger')
    return connectedRule('文件夹有新文件时通知我', '文件自动化', { binding_id: triggerId, type: 'folder_monitor', params: { folder_path: '', event_type: 'created', file_pattern: '*' } }, [{ type: 'notify', params: { title: '发现新文件', message: { $ref: { scope: 'trigger', node: triggerId, path: ['path'] } } } }])
  } },
  { id: 'daily-reminder', icon: 'alarm', title: '每天固定时间提醒我', description: '每天 22:00 显示系统通知，时间和提醒内容都可以继续修改。', triggerType: 'time_schedule', actionTypes: ['notify'], createRule() {
    return connectedRule('每天固定时间提醒我', '日常提醒', { binding_id: createBindingId('trigger'), type: 'time_schedule', params: { time: '22:00' } }, [{ type: 'notify', params: { title: 'NotmyFault', message: '到时间了，别忘了处理今天的事项。' } }])
  } },
]

function pluginMeta(data, kind, id) {
  const schemaGroup = kind === 'trigger' ? data.schema?.triggers : data.schema?.actions
  const installedGroup = kind === 'trigger' ? data.pluginsData?.triggers : data.pluginsData?.actions
  return { usable: schemaGroup?.[id], installed: installedGroup?.[id] }
}

function requirementState(data, kind, id) {
  const { usable, installed } = pluginMeta(data, kind, id)
  const meta = installed || usable
  const label = meta?.name || id
  if (meta?.platform_compatible === false) {
    return { available: false, kind, id, label, state: 'incompatible', reason: `“${label}”不支持当前系统` }
  }
  if (meta?.enabled === false) {
    return { available: false, kind, id, label, state: 'disabled', reason: `“${label}”尚未启用` }
  }
  const unavailable = pluginUnavailableReason(meta)
  if (meta && unavailable) {
    return { available: false, kind, id, label, state: 'unavailable', reason: `“${label}”${unavailable}` }
  }
  if (!usable) {
    return { available: false, kind, id, label, state: installed ? 'pending' : 'missing', reason: installed ? `“${label}”重启后可用` : `缺少“${label}”插件` }
  }
  return { available: true, kind, id, label, state: 'ready', reason: '可以直接使用' }
}

export function templateAvailability(template, data) {
  const requirements = [
    requirementState(data, 'trigger', template.triggerType),
    ...template.actionTypes.map(id => requirementState(data, 'action', id)),
  ]
  return requirements.find(item => !item.available) || {
    available: true,
    state: 'ready',
    reason: '可以直接使用',
    requirements,
  }
}

export function instantiateTemplate(template) {
  return ensureRuleBindingIds(clone(template.createRule()))
}

export function createQuickDraft(triggerType, actionType, schema) {
  const trigger = schema.triggers?.[triggerType]
  const action = schema.actions?.[actionType]
  return ensureRuleBindingIds(connectedRule(`${trigger?.name || triggerType}后${action?.name || actionType}`, '未分类', { binding_id: createBindingId('trigger'), type: triggerType, params: buildDefaultParams(trigger) }, [{ type: actionType, params: buildDefaultParams(action) }]))
}
