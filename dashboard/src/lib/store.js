import { reactive } from 'vue'

// 各视图共享这份响应式状态，字段变化时 Vue 会重新渲染。
export const store = reactive({
  schema: { triggers: {}, actions: {} },
  configData: { rules: [] },
  pluginsData: { triggers: {}, actions: {} },
  engineStatus: { api_alive: false, engine_running: false, engine_state: 'offline', pid: null },
  controllerOnline: false,
  engineOnline: false,
  refreshSignal: 0,   // SSE 事件到达时自增，视图监听它后刷新统计。
})
