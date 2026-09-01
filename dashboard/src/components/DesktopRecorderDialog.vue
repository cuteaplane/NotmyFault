<script setup>
import { computed, ref, watch } from 'vue'
import { hasBridge, invokeExtensionCommand } from '../lib/api'
import BaseDialog from './BaseDialog.vue'

const props = defineProps({
  open: Boolean,
  editor: { type: Object, default: null },
  mode: { type: String, default: 'actions' },
  commitLabel: { type: String, default: '保存' },
})
const emit = defineEmits(['close', 'insert', 'commit'])
const steps = ref([])
const selecting = ref(false)
const countdown = ref(0)
const status = ref('')
let nextStepId = 1

const editorAvailable = computed(() => Boolean(
  props.editor?.plugin_id && props.editor?.command && props.editor?.id,
))
const canRecord = computed(() => hasBridge() && editorAvailable.value && !selecting.value && steps.value.length < 50)

function reset() {
  steps.value = []
  selecting.value = false
  countdown.value = 0
  status.value = ''
  nextStepId = 1
}

function stepDisplay(step) {
  const display = step.selector?.display || {}
  const control = display.control || '未命名控件'
  const title = step.operation === 'set_text'
    ? `在“${control}”中写入文本`
    : step.operation === 'focus'
      ? `让焦点移到“${control}”`
      : step.operation === 'wait_present'
        ? `等待“${control}”出现`
        : step.operation === 'focus_window'
          ? `切换到“${display.window || '未命名窗口'}”窗口`
          : step.operation === 'read_text'
            ? `读取“${control}”的文本`
            : `按下“${control}”`
  return {
    title,
    detail: `${display.app || '未知程序'} · ${display.window || '未命名窗口'}`,
  }
}

async function recordStep() {
  if (!editorAvailable.value) {
    status.value = '录制扩展已不可用，请关闭后重新打开。'
    return
  }
  if (!canRecord.value) return
  selecting.value = true
  countdown.value = 3
  status.value = '把鼠标移到下一步要操作的控件上，不需要点击。'
  const timer = window.setInterval(() => {
    countdown.value = Math.max(0, countdown.value - 1)
  }, 1000)
  try {
    const result = await invokeExtensionCommand(
      props.editor.plugin_id,
      props.editor.command,
      {
        sourceKind: 'parameter_editors',
        sourceId: props.editor.id,
        payload: { operation: 'capture', delay_seconds: 3 },
      },
    )
    const selector = result?.value
    if (!result?.ok || !selector) {
      status.value = result?.error || '没有读到屏幕控件，请再试一次。'
      return
    }
    const operation = selector.capabilities?.invoke !== false
      ? 'invoke'
      : selector.capabilities?.set_text ? 'set_text' : 'focus'
    steps.value.push({
      id: nextStepId++,
      selector,
      operation,
      text: '',
      waitSeconds: 30,
    })
    status.value = `已录下第 ${steps.value.length} 步。可以继续选择下一个控件。`
  } catch (error) {
    status.value = error.message || '读取屏幕控件失败。'
  } finally {
    window.clearInterval(timer)
    countdown.value = 0
    selecting.value = false
  }
}

function removeStep(index) {
  steps.value.splice(index, 1)
  status.value = steps.value.length ? '已删除这一步。' : '录制列表已清空。'
}

async function insertSteps() {
  if (!steps.value.length) return
  const payload = steps.value.map(step => ({
    selector: step.selector,
    operation: step.operation,
    text: step.operation === 'set_text' ? step.text : '',
    waitSeconds: step.operation === 'wait_present' ? step.waitSeconds : 30,
  }))
  if (props.mode === 'value') {
    emit('commit', payload)
    emit('close')
    return
  }
  if (!editorAvailable.value) {
    status.value = '录制扩展已不可用，请关闭后重新打开。'
    return
  }
  try {
    const result = await invokeExtensionCommand(
      props.editor.plugin_id,
      props.editor.command,
      {
        sourceKind: 'parameter_editors',
        sourceId: props.editor.id,
        payload: { operation: 'to_actions', steps: payload },
      },
    )
    const actions = result?.data?.actions
    if (!result?.ok || !Array.isArray(actions) || !actions.length) {
      status.value = result?.error || '录制组件没有生成动作。'
      return
    }
    emit('insert', actions)
    emit('close')
  } catch (error) {
    status.value = error.message || '转换步骤失败。'
  }
}

watch(() => props.open, open => {
  if (open) reset()
})
</script>

<template>
  <BaseDialog :open="open" :closable="!selecting" :layer-top="true" @close="emit('close')">
    <section class="desktop-recorder-dialog" role="dialog" aria-modal="true" aria-labelledby="desktop-recorder-title">
        <header class="desktop-recorder-head">
          <div>
            <small>桌面步骤录制</small>
            <h2 id="desktop-recorder-title">依次指出要操作的控件</h2>
            <p>每次选择一个按钮、菜单项或输入区域。NotmyFault 保存控件特征，不保存回放坐标。</p>
          </div>
          <button class="icon-btn" :disabled="selecting" title="关闭录制" @click="emit('close')"><span class="material-symbols-outlined">close</span></button>
        </header>

        <div class="desktop-recorder-toolbar">
          <button class="btn btn-filled" :disabled="!canRecord" @click="recordStep">
            <span class="material-symbols-outlined">center_focus_strong</span>
            {{ selecting ? `${countdown || '正在'} 秒后读取` : (steps.length ? '选择下一个控件' : '选择第一个控件') }}
          </button>
          <span>{{ steps.length }} 个步骤</span>
        </div>

        <p v-if="!hasBridge()" class="desktop-recorder-message error">
          <span class="material-symbols-outlined">desktop_windows</span>只有 NotmyFault 桌面应用可以录制屏幕控件。
        </p>
        <p v-else-if="!editorAvailable" class="desktop-recorder-message error">
          <span class="material-symbols-outlined">extension_off</span>录制扩展已不可用，请关闭后重新打开。
        </p>
        <p v-else-if="status" class="desktop-recorder-message" aria-live="polite">
          <span class="material-symbols-outlined">info</span>{{ status }}
        </p>

        <ol v-if="steps.length" class="desktop-recorder-steps">
          <li v-for="(step, index) in steps" :key="step.id">
            <span class="desktop-recorder-index">{{ index + 1 }}</span>
            <span class="desktop-recorder-step-copy">
              <b>{{ stepDisplay(step).title }}</b>
              <small>{{ stepDisplay(step).detail }}</small>
            </span>
            <select v-model="step.operation" class="select" aria-label="这一步怎么操作">
              <option value="invoke" :disabled="step.selector.capabilities?.invoke === false">按下控件</option>
              <option value="focus">让控件获得焦点</option>
              <option value="set_text" :disabled="!step.selector.capabilities?.set_text">写入文本</option>
              <option value="wait_present">等待控件出现</option>
              <option value="focus_window">切换到这个窗口</option>
              <option value="read_text" :disabled="!step.selector.capabilities?.read_text">读取控件文本</option>
            </select>
            <button class="icon-btn icon-btn-danger" title="删除这一步" @click="removeStep(index)"><span class="material-symbols-outlined">delete</span></button>
            <label v-if="step.operation === 'set_text'" class="desktop-recorder-text">
              <span>要写入的文本</span>
              <textarea v-model="step.text" class="text-field textarea-field" rows="2" placeholder="这段文字会保存在规则中，请不要填写密码"></textarea>
              <small>运行记录会隐藏内容；规则文件仍会保存这段文字。</small>
            </label>
            <label v-else-if="step.operation === 'wait_present'" class="desktop-recorder-wait">
              <span>最多等待多少秒</span>
              <input v-model.number="step.waitSeconds" class="text-field" type="number" min="1" max="600" step="1">
              <small>控件提前出现就立即继续；超过时间仍未出现则这一步失败。</small>
            </label>
          </li>
        </ol>
        <div v-else class="desktop-recorder-empty">
          <span class="material-symbols-outlined">touch_app</span>
          <b>还没有录下步骤</b>
          <p>点“选择第一个控件”，然后在倒计时结束前把鼠标移到目标控件上。</p>
        </div>

        <footer class="desktop-recorder-foot">
          <small>加入后，每一步仍是普通动作，可以单独修改、移动或删除。</small>
          <div>
            <button class="btn btn-text" :disabled="selecting" @click="emit('close')">取消</button>
            <button class="btn btn-filled" :disabled="!steps.length || selecting || (mode !== 'value' && !editorAvailable)" @click="insertSteps">
              {{ mode === 'value' ? `${commitLabel}（${steps.length} 步）` : `加入 ${steps.length} 个步骤` }}<span class="material-symbols-outlined">arrow_forward</span>
            </button>
          </div>
        </footer>
      </section>
  </BaseDialog>
</template>
