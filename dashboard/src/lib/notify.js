import { reactive } from 'vue'

export const snack = reactive({ show: false, msg: '' })
let timer = null

export function snackbar(msg) {
  snack.show = true
  snack.msg = msg
  clearTimeout(timer)
  timer = setTimeout(() => { snack.show = false }, 2200)
}
