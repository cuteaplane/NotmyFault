import { reactive } from 'vue'

export const dialogState = reactive({
  open: false,
  title: '',
  message: '',
  kind: 'alert',
  tone: 'error',
  confirmLabel: '确定',
  inputValue: '',
  inputError: '',
})

let resolveCurrent = null

// 上一个弹窗还没关就弹新的时，旧的按取消结束。
function openDialog(options) {
  if (resolveCurrent) resolveCurrent(false)
  Object.assign(dialogState, { inputValue: '', inputError: '' }, options, { open: true })
  return new Promise(resolve => { resolveCurrent = resolve })
}

export function closeDialog(result) {
  dialogState.open = false
  dialogState.inputValue = ''
  dialogState.inputError = ''
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

export function passwordDialog(title, message, inputError = '') {
  return openDialog({
    kind: 'password',
    tone: 'warn',
    title,
    message,
    inputError,
    confirmLabel: '验证并保存',
  })
}
