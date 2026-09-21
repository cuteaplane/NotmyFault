export const permissionLabels = {
  notification: '发送通知', audio: '音频', clipboard: '剪贴板', network: '网络访问',
  external_binary: '外部程序', native_api: '原生 API', filesystem: '文件系统',
  process: '进程管理', registry: '注册表', screen_reader: '屏幕读取',
  input_monitor: '监听键盘和鼠标', admin: '管理员权限',
}
export const permissionClasses = {
  notification: 'chip-clean', audio: 'chip-permission-low', clipboard: 'chip-permission-medium',
  network: 'chip-permission-medium', external_binary: 'chip-external', native_api: 'chip-native',
  filesystem: 'chip-permission-high', process: 'chip-permission-high', registry: 'chip-permission-high',
  screen_reader: 'chip-permission-high', input_monitor: 'chip-permission-high', admin: 'chip-admin',
}
export const permissionIcons = {
  notification: 'notifications', audio: 'volume_up', clipboard: 'content_paste', network: 'language',
  external_binary: 'terminal', native_api: 'code', filesystem: 'folder_open', process: 'memory',
  registry: 'account_tree', screen_reader: 'screenshot_monitor', input_monitor: 'keyboard', admin: 'admin_panel_settings',
}
