<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { store } from '../../lib/store'
import { appLogoUrl } from '../../lib/branding'
import OriginDialog from '../OriginDialog.vue'

const appVersion = __APP_VERSION__

// 科乐美秘技：在关于页依次输入序列解锁起源彩蛋，按错即重置。
const KONAMI_SEQUENCE = [
  'ArrowUp', 'ArrowUp', 'ArrowDown', 'ArrowDown',
  'ArrowLeft', 'ArrowRight', 'ArrowLeft', 'ArrowRight', 'b', 'a',
]
let konamiProgress = 0
const showOrigin = ref(false)
function onKonamiKey(event) {
  const key = event.key.length === 1 ? event.key.toLowerCase() : event.key
  if (key === KONAMI_SEQUENCE[konamiProgress]) {
    konamiProgress++
    if (konamiProgress === KONAMI_SEQUENCE.length) {
      konamiProgress = 0
      showOrigin.value = true
    }
  } else {
    konamiProgress = key === KONAMI_SEQUENCE[0] ? 1 : 0
  }
}
onMounted(() => window.addEventListener('keydown', onKonamiKey))
onUnmounted(() => window.removeEventListener('keydown', onKonamiKey))

const ruleCount = computed(() => store.configData?.rules?.length || 0)
const triggerCount = computed(() => Object.keys(store.pluginsData?.triggers || {}).length)
const actionCount = computed(() => Object.keys(store.pluginsData?.actions || {}).length)
const modeLabel = computed(() => {
  const m = store.engineStatus?.security_mode
  return ({ strict: '严格', normal: '标准', permissive: '宽松' })[m] || '未知'
})

const features = [
  { icon: 'extension', title: '插件化', desc: '触发器与动作都是独立插件，自带签名校验、权限声明和平台兼容检查' },
  { icon: 'account_tree', title: '规则', desc: '条件支持 AND / OR 嵌套，动作按顺序执行，后面的动作可以用前面步骤的结果' },
  { icon: 'shield', title: '安全', desc: '插件需要 Ed25519 签名，管理员权限单独授权，危险能力会被扫描提示' },
  { icon: 'monitor_heart', title: '日志与诊断', desc: '动作执行记录、触发器运行状态都能在页面上直接看到' },
]

const ruleFlow = [
  { icon: 'sensors', step: '触发', desc: '定时、热键、剪贴板、进程、文件夹、设备、电源、网络等' },
  { icon: 'fact_check', step: '检查（可选）', desc: '比如确认文件不再写入、文档没在编辑，再决定是否继续' },
  { icon: 'play_circle', step: '执行', desc: '音量/亮度、启动或结束程序、文件操作、截图、通知、HTTP 请求等' },
]

const platforms = [
  { icon: 'desktop_windows', name: 'Windows', state: '主要开发平台', cls: 'text-success' },
  { icon: 'terminal', name: 'Linux', state: '实验性支持', cls: 'text-warn' },
  { icon: 'laptop_mac', name: 'macOS', state: '暂不支持', cls: 'text-outline' },
]

const runtimeInfo = computed(() => [
  { icon: 'info', key: '版本', val: 'NotmyFault ' + appVersion },
  { icon: 'shield', key: '安全模式', val: modeLabel.value },
  { icon: 'rule', key: '已配置规则', val: ruleCount.value + ' 条' },
  { icon: 'memory', key: '触发器插件', val: triggerCount.value ? triggerCount.value + ' 个' : '引擎离线时不可用' },
  { icon: 'bolt', key: '动作插件', val: actionCount.value ? actionCount.value + ' 个' : '引擎离线时不可用' },
  { icon: 'lan', key: '本地 API', val: '127.0.0.1:19198 · 本机令牌认证' },
])
</script>

<template>
  <section class="page active about-page">
    <div class="page-head"><h2>关于</h2></div>

    <div class="about-shell">
      <header class="about-intro">
        <div class="about-brand">
          <img class="about-brand-mark" :src="appLogoUrl" alt="">
          <div>
            <h3>NotmyFault</h3>
          </div>
        </div>
        <div class="about-badges">
          <span>{{ appVersion }}</span>
          <span>Alpha</span>
          <span>GPL-3.0</span>
        </div>
        <p>
          拓展万千
        </p>
      </header>

      <main class="about-content">
        <div class="about-main-column">
          <section class="about-section">
            <header class="about-section-head">
              <h4>功能</h4>
            </header>
            <div class="about-feature-list">
              <article v-for="f in features" :key="f.title" class="about-feature">
                <span class="material-symbols-outlined">{{ f.icon }}</span>
                <div>
                  <b>{{ f.title }}</b>
                  <p>{{ f.desc }}</p>
                </div>
              </article>
            </div>
          </section>

          <section class="about-section">
            <header class="about-section-head">
              <h4>自动化结构</h4>
            </header>
            <p class="about-section-lead">一条自动化由三步组成，自动化页负责创建、编辑和检查。</p>
            <div class="about-flow">
              <article v-for="(s, i) in ruleFlow" :key="s.step">
                <span class="about-flow-index">{{ String(i + 1).padStart(2, '0') }}</span>
                <span class="material-symbols-outlined">{{ s.icon }}</span>
                <div>
                  <b>{{ s.step }}</b>
                  <p>{{ s.desc }}</p>
                </div>
              </article>
            </div>
          </section>
        </div>

        <aside class="about-side-column">
          <section class="about-section about-runtime">
            <header class="about-section-head">
              <h4>运行信息</h4>
            </header>
            <dl>
              <div v-for="r in runtimeInfo" :key="r.key">
                <dt><span class="material-symbols-outlined">{{ r.icon }}</span>{{ r.key }}</dt>
                <dd>{{ r.val }}</dd>
              </div>
            </dl>
          </section>

          <section class="about-section">
            <header class="about-section-head">
              <h4>平台支持</h4>
            </header>
            <div class="about-platforms">
              <div v-for="p in platforms" :key="p.name">
                <span class="material-symbols-outlined">{{ p.icon }}</span>
                <b>{{ p.name }}</b>
                <span :class="p.cls">{{ p.state }}</span>
              </div>
            </div>
            <p class="about-platform-note">
              配置与日志位于 <code>%APPDATA%\NotmyFault\</code>，Linux 为 <code>~/.config/notmyfault/</code>。
            </p>
          </section>

          <section class="about-section about-stack">
            <header class="about-section-head">
              <h4>技术栈</h4>
            </header>
            <div>
              <span v-for="t in ['Python 引擎', 'Vue 3', 'FastAPI 本地服务', 'SSE 事件推送']" :key="t">{{ t }}</span>
            </div>
          </section>
        </aside>
      </main>

      <footer class="about-footer">
        <span>&copy; 2026 NotmyFault Project</span>
        <span>GNU General Public License v3.0</span>
        <span>问题反馈请附系统版本、复现步骤与最新日志</span>
      </footer>
    </div>

    <OriginDialog v-if="showOrigin" @close="showOrigin = false" />
  </section>
</template>
