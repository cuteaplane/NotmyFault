// 项目起源彩蛋的静态内容，运行时后端不再生成这些规则。
export const ORIGIN_STORY = {
  title: 'NotmyFault 的起源',
  paragraphs: [
    '高中时教室的电子白板被半强制升级到了 Windows 11，音量调节的入口变了，老师上课调音量不习惯，认定是作者把白板「捣坏了」，在课堂上阴阳怪气了一整节。',
    '作者气不过，写了个自动调音量的脚本，给我的老师擦屁股，又不是我的锅——这就是项目名 NotmyFault 的由来。',
    '下面六条规则就是当年脚本的完整形态，随 event-v1 改造更新过一次，如今只剩历史意义。点开卡片可以导入到自动化页随时把玩。',
  ],
}

export const ORIGIN_RULES = [
  {
    name: '微信音量规则',
    event: {
      type: 'process_state',
      params: { process_name: 'WeChat.exe', state: 'running' },
    },
    actions: [
      { type: 'set_volume', params: { action: 'max' } },
      {
        type: 'notify',
        params: { title: '微信正在运行', message: '音量已设置为100%' },
      },
    ],
  },
  {
    name: 'PPT音量规则',
    event: {
      type: 'process_state',
      params: { process_name: 'POWERPNT.EXE', state: 'running' },
    },
    actions: [
      { type: 'set_volume', params: { action: 'max' } },
      {
        type: 'notify',
        params: { title: 'PowerPoint正在运行', message: '音量已设置为100%' },
      },
    ],
  },
  {
    name: '媒体播放器规则',
    event: {
      type: 'process_state',
      params: { process_name: 'wmplayer.exe', state: 'running' },
    },
    actions: [
      { type: 'set_volume', params: { action: 'half' } },
      {
        type: 'notify',
        params: { title: '媒体播放器检测', message: '音量已调整为50%' },
      },
    ],
  },
  {
    name: '微信退出-恢复音量',
    event: {
      type: 'process_state',
      params: { process_name: 'WeChat.exe', state: 'stopped' },
    },
    actions: [
      { type: 'set_volume', params: { action: 'half' } },
      {
        type: 'notify',
        params: { title: '微信已退出', message: '音量已恢复至50%' },
      },
    ],
  },
  {
    name: 'PPT退出-恢复音量',
    event: {
      type: 'process_state',
      params: { process_name: 'POWERPNT.EXE', state: 'stopped' },
    },
    actions: [
      { type: 'set_volume', params: { action: 'half' } },
      {
        type: 'notify',
        params: { title: 'PowerPoint已退出', message: '音量已恢复至50%' },
      },
    ],
  },
  {
    name: '媒体播放器退出-恢复音量',
    event: {
      type: 'process_state',
      params: { process_name: 'wmplayer.exe', state: 'stopped' },
    },
    actions: [
      { type: 'set_volume', params: { action: 'half' } },
      {
        type: 'notify',
        params: { title: '媒体播放器已退出', message: '音量已恢复至50%' },
      },
    ],
  },
]
