import { reactive } from 'vue'

export const dialogState = reactive({
  open: false,
  title: '',
  message: '',
  kind: 'alert',
  tone: 'error',
  confirmLabel: '确定',
})

let resolveCurrent = null

// 上一个弹窗还没关就弹新的时，旧的按取消结束。
function openDialog(options) {
  if (resolveCurrent) resolveCurrent(false)
  Object.assign(dialogState, options, { open: true })
  return new Promise(resolve => { resolveCurrent = resolve })
}

export function closeDialog(result) {
  dialogState.open = false
  const done = resolveCurrent
  resolveCurrent = null
  if (done) done(result)
}

export function alertDialog(title, message, tone = 'error') {
  return openDialog({ kind: 'alert', tone, title, message, confirmLabel: '确定' })
}

export function confirmDialog(title, message, confirmLabel = '确认') {
  return openDialog({ kind: 'confirm', tone: 'warn', title, message, confirmLabel })
}
