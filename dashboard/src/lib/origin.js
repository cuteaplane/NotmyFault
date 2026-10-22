import { createBindingId } from './bindings.js'
import { createGraphAction, createTriggerStep } from './workflowGraph.js'

function connectedOriginRule(name, processName, state, volume, notificationTitle, message) {
  const triggerId = createBindingId('trigger')
  const nodes = [
    createTriggerStep({
      binding_id: triggerId,
      type: 'process_state',
      params: { process_name: processName, state },
    }),
    createGraphAction('set_volume', { action: volume }),
    createGraphAction('notify', { title: notificationTitle, message }),
  ]
  nodes.forEach((node, index) => { node.position = { x: 64 + index * 340, y: 118 } })
  return {
    name,
    folder: '未分类',
    workflow: {
      entry: null,
      nodes,
      edges: nodes.slice(0, -1).map((node, index) => ({
        source: node.binding_id,
        port: index ? 'next' : 'event',
        target: nodes[index + 1].binding_id,
        channel: 'control',
      })),
    },
  }
}

export const ORIGIN_STORY = {
  title: 'NotmyFault 的起源',
  paragraphs: [
    '高中时教室的电子白板被半强制升级到了 Windows 11，音量调节的入口变了，老师上课调音量不习惯，认定是作者把白板「捣坏了」，在课堂上阴阳怪气了一整节。',
    '作者气不过，写了个自动调音量的脚本，给我的老师擦屁股，又不是我的锅——这就是项目名 NotmyFault 的由来。',
    '下面六条规则就是当年脚本的完整形态，随 event-v1 改造更新过一次，如今只剩历史意义。点开卡片可以导入到自动化页随时把玩。',
  ],
}

export const ORIGIN_RULES = [
  connectedOriginRule('微信音量规则', 'WeChat.exe', 'running', 'max', '微信正在运行', '音量已设置为100%'),
  connectedOriginRule('PPT音量规则', 'POWERPNT.EXE', 'running', 'max', 'PowerPoint正在运行', '音量已设置为100%'),
  connectedOriginRule('媒体播放器规则', 'wmplayer.exe', 'running', 'half', '媒体播放器检测', '音量已调整为50%'),
  connectedOriginRule('微信退出-恢复音量', 'WeChat.exe', 'stopped', 'half', '微信已退出', '音量已恢复至50%'),
  connectedOriginRule('PPT退出-恢复音量', 'POWERPNT.EXE', 'stopped', 'half', 'PowerPoint已退出', '音量已恢复至50%'),
  connectedOriginRule('媒体播放器退出-恢复音量', 'wmplayer.exe', 'stopped', 'half', '媒体播放器已退出', '音量已恢复至50%'),
]
