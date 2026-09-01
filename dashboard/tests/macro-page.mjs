import fs from 'fs'
import path from 'path'
import { JSDOM, VirtualConsole } from 'jsdom'


const html = fs.readFileSync(
  path.resolve('../notmyfault/actions/uia_macro/pages/macro.html'),
  'utf8',
)
const scriptErrors = []
const virtualConsole = new VirtualConsole()
virtualConsole.on('jsdomError', error => scriptErrors.push(error))
const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  url: 'http://notmyfault.test/macro',
  virtualConsole,
})
const { window } = dom

window.dispatchEvent(new window.MessageEvent('message', {
  source: window,
  data: {
    source: 'notmyfault:extension-host',
    type: 'init',
    state: {
      recording: false,
      steps: [
        {
          kind: 'control',
          selector: { display: { app: 'notepad.exe', window: '记事本', control: '文本框' } },
          operation: 'set_text',
          text: '测试文字',
          delay_seconds: 0.5,
        },
        {
          kind: 'keyboard',
          window: { process: 'WindowsTerminal.exe', name: '终端' },
          events: [
            { event: 'down', vk: 17, scan_code: 29 },
            { event: 'down', vk: 65, scan_code: 30 },
            { event: 'up', vk: 65, scan_code: 30 },
            { event: 'up', vk: 17, scan_code: 29 },
          ],
          delay_seconds: 0.2,
        },
      ],
    },
  },
}))

const pageOk = scriptErrors.length === 0
  && html.includes("type:'ready'")
  && window.document.querySelector('style')?.textContent.includes('.recording-options { border-color: #35353b; background: #1b1b21; }')
  && window.document.querySelectorAll('.timeline-item').length === 2
  && window.document.querySelector('textarea.field-input')?.value === '测试文字'
  && window.document.querySelector('.kind.keyboard')?.textContent === '键盘'
  && window.document.querySelector('.kind.keyboard')?.closest('.step')?.textContent.includes('发送到 WindowsTerminal.exe')
  && window.document.querySelector('.step-title')?.textContent === '文本框'
  && window.document.getElementById('stepCount')?.textContent === '2 步'
  && window.document.getElementById('empty')?.hidden === true
  && window.document.querySelector('#minimizeOption[role="switch"]')?.checked === true
  && window.document.querySelector('#appendOption[role="switch"]')?.checked === false
  && window.document.getElementById('startButton')?.textContent.includes('重新录制')
  && window.document.querySelector('.step-actions .remove')?.textContent === '删除'
  && !window.document.querySelector('[title="检查这一步"]')

console.log((pageOk ? 'PASS' : 'FAIL') + ' - operation macro page renders editable control and keyboard steps')
if (!pageOk) {
  console.error(scriptErrors)
  process.exit(1)
}

const invokes = []
window.postMessage = message => invokes.push(message)
window.document.getElementById('startButton')?.click()
await new Promise(resolve => window.setTimeout(resolve, 0))
const replacementStartsClean = invokes.some(message => (
  message.type === 'invoke'
  && message.command === 'start_recording'
  && message.payload?.append === false
  && message.payload?.steps?.length === 0
))
console.log((replacementStartsClean ? 'PASS' : 'FAIL') + ' - re-recording replaces existing macro steps by default')
if (!replacementStartsClean) process.exit(1)
