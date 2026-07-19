import { reactive } from 'vue'

// 全局共享响应式状态，各视图读取并在变更时自动重渲染
export const store = reactive({
  schema: { triggers: {}, actions: {} },
  configData: { rules: [] },
  pluginsData: { triggers: {}, actions: {} },
  engineStatus: { running: false, pid: null },
  engineOnline: false,
  refreshSignal: 0,   // SSE 事件触发时自增，视图 watch 后刷新
})
