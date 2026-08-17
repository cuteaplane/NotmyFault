export const aiProviderPresets = [
  { id: 'openai', label: 'OpenAI', endpointUrl: 'https://api.openai.com/v1', model: 'gpt-5.4', apiFormat: 'responses' },
  { id: 'deepseek', label: 'DeepSeek', endpointUrl: 'https://api.deepseek.com', model: 'deepseek-v4-pro', apiFormat: 'responses' },
  { id: 'gemini', label: 'Gemini', endpointUrl: 'https://generativelanguage.googleapis.com/v1beta/openai', model: 'gemini-3.6-flash', apiFormat: 'chat_completions' },
  { id: 'qwen', label: '通义千问', endpointUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-plus', apiFormat: 'chat_completions' },
  { id: 'zhipu', label: '智谱 GLM', endpointUrl: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-5.2', apiFormat: 'chat_completions' },
  { id: 'moonshot', label: 'Moonshot / Kimi', endpointUrl: 'https://api.moonshot.cn/v1', model: 'kimi-k2.6', apiFormat: 'chat_completions' },
]

export function aiProviderIdFor(settings) {
  const endpointUrl = String(settings?.endpoint_url || '').replace(/\/+$/, '')
  const model = String(settings?.model || '').trim()
  const apiFormat = String(settings?.api_format || '').trim()
  return aiProviderPresets.find(preset => (
    preset.endpointUrl === endpointUrl
    && preset.model === model
    && preset.apiFormat === apiFormat
  ))?.id || 'custom'
}
