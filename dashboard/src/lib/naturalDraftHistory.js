import { normalizeDraftResult } from './naturalDraftRules.js'

const STORAGE_KEY = 'nmf_ai_conversation'
export const MAX_HISTORY_ITEMS = 40

export function loadConversation() {
  try {
    localStorage.removeItem(STORAGE_KEY)
    const saved = sessionStorage.getItem(STORAGE_KEY)
    if (!saved) return []
    const parsed = JSON.parse(saved)
    if (Array.isArray(parsed) && parsed.length > 0) {
      const messages = parsed.map(msg => {
        const activity = msg.activity
          ? {
            ...msg.activity,
            open: false,
            finished: true,
            failed: msg.activity.finished ? msg.activity.failed : true,
          }
          : null
        return {
          ...msg,
          result: normalizeDraftResult(msg.result),
          timestamp: msg.timestamp || Date.now(),
          transient: false,
          streaming: false,
          stopped: msg.stopped === true,
          error: msg.error === true,
          progress: null,
          activity,
          requestMessageId: msg.requestMessageId || null,
          retryPrompt: typeof msg.retryPrompt === 'string' ? msg.retryPrompt : '',
          ruleDetailsOpen: false,
          reasoningOpen: false,
        }
      })
      return messages
    }
  } catch (err) {
    console.warn('加载对话失败:', err)
  }
  return []
}

export function saveConversation(messages) {
  try {
    const toSave = messages
      .filter(m => !m.transient)
      .slice(-MAX_HISTORY_ITEMS)
      .map(({ id, role, timestamp, content, stopped, error, requestMessageId, retryPrompt }) => ({
        id, role, timestamp, content, stopped, error, requestMessageId, retryPrompt,
      }))
    if (toSave.length) sessionStorage.setItem(STORAGE_KEY, JSON.stringify(toSave))
    else sessionStorage.removeItem(STORAGE_KEY)
  } catch (err) {
    console.warn('保存对话失败:', err)
  }
}

export function clearSavedConversation() {
  try { sessionStorage.removeItem(STORAGE_KEY) } catch {}
  try { localStorage.removeItem(STORAGE_KEY) } catch {}
}
