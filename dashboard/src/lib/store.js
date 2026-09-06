import { reactive } from 'vue'

// 各视图共享这份响应式状态，字段变化时 Vue 会重新渲染。
export const store = reactive({
  schema: { triggers: {}, actions: {} },
  extensions: { commands: [], parameter_editors: [], views: [], data_types: [] },
  configData: { rules: [] },
  configLoaded: false,
  pluginsData: { triggers: {}, actions: {} },
  engineStatus: { api_alive: false, engine_running: false, engine_state: 'offline', pid: null },
  controllerOnline: false,
  engineOnline: false,
  refreshSignal: 0,   // SSE 事件到达时自增，视图监听它后刷新统计。
  engineEvents: [],   // SSE 推来的规则执行事件，规则测试回显用。
  pendingRuleId: '',
  pendingRuleName: '', // 旧调用仍可按名称跳转，规则页优先使用 rule_id。
  pendingStepId: '',
  pendingRunId: '',
  activeManualRun: null,
  pendingRuleDraft: null,
  pendingPluginFocus: null,
  pendingAutomationCreate: false,
  pendingAutomationSection: '',
  pendingSettingsSection: '',
  pendingAiPanel: false,
  aiDrafting: { enabled: false, endpoint_url: '', model: '', api_format: 'chat_completions' },
  aiApiKeyStatus: 'none',
})
