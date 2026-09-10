<script setup>
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useTheme } from '../composables/useTheme'

const emit = defineEmits(['complete'])
const { setMode } = useTheme()
const draft = (() => {
  try { return JSON.parse(localStorage.getItem('nmf-first-run') || '{}') || {} }
  catch { return {} }
})()
const step = ref(draft.step === 1 ? 1 : 0)
const theme = ref(localStorage.getItem('nmf-theme') || 'system')
const autoStart = ref(draft.autoStart === true)
const autoStartChosen = ref(draft.autoStartChosen === true)
const loading = ref(true)
const loaded = ref(false)
const saving = ref(false)
const error = ref('')
const heading = ref(null)
const themes = [
  { value: 'system', label: '跟随系统', icon: 'contrast', detail: '随 Windows 的外观变化' },
  { value: 'light', label: '浅色', icon: 'light_mode', detail: '明亮的背景和蓝色点缀' },
  { value: 'dark', label: '深色', icon: 'dark_mode', detail: '柔和的深色背景' },
]

function saveDraft() {
  localStorage.setItem('nmf-first-run', JSON.stringify({
    step: step.value, autoStart: autoStart.value, autoStartChosen: autoStartChosen.value,
  }))
}

function selectTheme(value) {
  try {
    setMode(value)
    theme.value = value
    error.value = ''
  } catch (cause) { error.value = cause.message || '无法保存主题，请重试' }
}

async function changeStep(value) {
  try {
    step.value = value
    saveDraft()
    error.value = ''
    await nextTick()
    heading.value?.focus({ preventScroll: true })
    if (value === 1 && !loaded.value) await loadAutoStart()
  } catch (cause) { error.value = cause.message || '无法保存配置进度，请重试' }
}

function selectAutoStart(value) {
  try {
    autoStart.value = value
    autoStartChosen.value = true
    saveDraft()
    error.value = ''
  } catch (cause) { error.value = cause.message || '无法保存选项，请重试' }
}

async function loadAutoStart() {
  if (!window.pywebview?.api) return
  loading.value = true
  error.value = ''
  try {
    const result = await window.pywebview.api.get_auto_start()
    if (!result?.ok) throw new Error(result?.error || '无法读取开机启动设置')
    if (!autoStartChosen.value) autoStart.value = result.enabled === true
    loaded.value = true
  } catch (cause) { error.value = cause.message || '无法读取开机启动设置，请重试' }
  finally { loading.value = false }
}

async function finish() {
  if (saving.value || loading.value || !loaded.value) return
  saving.value = true
  error.value = ''
  try {
    setMode(theme.value)
    if (autoStartChosen.value) {
      if (!window.pywebview?.api) throw new Error('Dashboard 尚未连接，请稍后重试')
      const result = await window.pywebview.api.set_auto_start(autoStart.value)
      if (!result?.ok) throw new Error(result?.error || '无法保存开机启动设置')
    }
    localStorage.removeItem('nmf-first-run')
    emit('complete')
  } catch (cause) { error.value = cause.message || '无法完成配置，请重试' }
  finally { saving.value = false }
}

onMounted(() => {
  window.addEventListener('pywebviewready', loadAutoStart)
  void loadAutoStart()
  heading.value?.focus({ preventScroll: true })
})
onUnmounted(() => window.removeEventListener('pywebviewready', loadAutoStart))
</script>

<template>
  <main class="first-run" aria-label="NotmyFault 初次配置">
    <div class="first-run-content">
      <header class="first-run-header">
        <div class="first-run-brand">NotmyFault <span>初次配置</span></div>
        <p class="first-run-step">{{ step + 1 }} / 2</p>
      </header>
      <div class="first-run-body">
        <h1 ref="heading" tabindex="-1">{{ step === 0 ? '选择外观' : '设置开机启动' }}</h1>
        <p class="first-run-description">{{ step === 0 ? '选择你喜欢的主题，稍后也可以在设置中更改。' : '登录 Windows 后，让 NotmyFault 在后台运行自动化。' }}</p>
        <fieldset v-if="step === 0" class="first-run-choices theme-choices" :disabled="saving">
          <legend class="first-run-sr-only">主题</legend>
          <label v-for="option in themes" :key="option.value" class="first-run-choice" :class="{ selected: theme === option.value }">
            <input type="radio" name="setup-theme" :value="option.value" :checked="theme === option.value" @change="selectTheme(option.value)">
            <span class="material-symbols-outlined choice-icon" aria-hidden="true">{{ option.icon }}</span>
            <strong>{{ option.label }}</strong>
            <span class="choice-detail">{{ option.detail }}</span>
          </label>
        </fieldset>
        <fieldset v-else class="first-run-choices startup-choices" :disabled="loading || saving">
          <legend class="first-run-sr-only">开机启动</legend>
          <label class="first-run-choice" :class="{ selected: !autoStart }">
            <input type="radio" name="setup-startup" :checked="!autoStart" @change="selectAutoStart(false)">
            <span><strong>暂不开启</strong><span class="choice-detail">需要时手动打开 NotmyFault</span></span>
          </label>
          <label class="first-run-choice" :class="{ selected: autoStart }">
            <input type="radio" name="setup-startup" :checked="autoStart" @change="selectAutoStart(true)">
            <span><strong>开机时启动 NotmyFault</strong><span class="choice-detail">登录后自动运行，也可以通过托盘菜单关闭</span></span>
          </label>
        </fieldset>
        <p v-if="step === 1 && loading" class="first-run-status" role="status">正在读取开机启动设置…</p>
        <div v-if="error" class="first-run-error" role="alert">
          <span>{{ error }}</span>
          <button v-if="step === 1 && !saving" class="btn btn-text" @click="loadAutoStart">重新读取</button>
        </div>
      </div>
      <footer class="first-run-footer">
        <button v-if="step > 0" class="btn btn-text" :disabled="saving" @click="changeStep(0)">上一步</button>
        <span v-else />
        <button v-if="step === 0" class="btn btn-filled" @click="changeStep(1)">下一步<span class="material-symbols-outlined" aria-hidden="true">arrow_forward</span></button>
        <button v-else class="btn btn-filled" :disabled="loading || !loaded || saving" @click="finish">{{ saving ? '正在保存…' : '完成配置' }}<span v-if="!saving" class="material-symbols-outlined" aria-hidden="true">check</span></button>
      </footer>
    </div>
  </main>
</template>

<style scoped>
.first-run{width:100%;height:100vh;overflow:auto;background:var(--md-surface);padding:40px 56px;color:var(--md-on-surface)}
.first-run-content{min-height:100%;max-width:1000px;margin:0 auto;display:flex;flex-direction:column}
.first-run-header{display:flex;align-items:center;justify-content:space-between;gap:24px;color:var(--md-on-surface-variant)}
.first-run-brand{font:var(--ts-title-m);font-family:var(--md-font-display)}
.first-run-brand span{font:var(--ts-body-m);margin-left:16px}
.first-run-step{font:var(--ts-label-l);font-variant-numeric:tabular-nums}
.first-run-body{flex:1;padding:68px 0 36px}
.first-run h1{font:var(--ts-display-s);letter-spacing:-.6px;margin:0 0 16px}
.first-run h1:focus{outline:none}
.first-run-description{font:var(--ts-body-l);color:var(--md-on-surface-variant);line-height:1.7;max-width:660px}
.first-run-choices{border:0;padding:0;margin:40px 0 0;display:grid;gap:16px;min-width:0}
.theme-choices{grid-template-columns:repeat(3,minmax(0,1fr))}
.first-run-choice{position:relative;display:flex;flex-direction:column;gap:10px;border:1px solid var(--md-outline-variant);border-radius:var(--r-lg);padding:24px;background:var(--md-surface-c-low);cursor:pointer;transition:background-color .16s,border-color .16s}
.first-run-choice:hover{background:var(--md-surface-c)}
.first-run-choice.selected{border:2px solid var(--md-primary);padding:23px;background:var(--md-primary-container);color:var(--md-on-primary-container)}
.first-run-choice:focus-within{outline:2px solid var(--md-primary);outline-offset:4px}
.first-run-choice input{accent-color:var(--md-primary);width:20px;height:20px;cursor:pointer}
.theme-choices input{position:absolute;top:24px;right:24px}
.choice-icon{font-size:32px;margin-bottom:22px;color:var(--md-primary)}
.first-run-choice strong{display:block;font:var(--ts-title-m)}
.choice-detail{display:block;font:var(--ts-body-m);line-height:1.6;color:var(--md-on-surface-variant)}
.selected .choice-detail{color:var(--md-on-primary-container)}
.startup-choices{max-width:700px;gap:12px}
.startup-choices .first-run-choice{flex-direction:row;align-items:center;gap:20px;padding:22px 24px}
.startup-choices .first-run-choice.selected{padding:21px 23px}
.startup-choices .choice-detail{margin-top:6px}
.first-run-footer{display:flex;align-items:center;justify-content:space-between;gap:20px;border-top:1px solid var(--md-outline-variant);padding-top:24px}
.first-run-footer .btn{min-width:116px;height:48px;justify-content:center}
.first-run-status{margin-top:16px;color:var(--md-on-surface-variant)}
.first-run-error{margin-top:20px;color:var(--md-error);font:var(--ts-body-m);display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.first-run-sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap}
@media(max-width:700px){.first-run{padding:28px}.first-run-body{padding-top:40px}.theme-choices{gap:10px}.first-run-choice{padding:18px}.first-run-choice.selected{padding:17px}.theme-choices input{top:18px;right:18px}.choice-detail{font-size:13px}.first-run h1{font:var(--ts-head-l)}}
@media(prefers-reduced-motion:reduce){.first-run-choice{transition:none}}
</style>
