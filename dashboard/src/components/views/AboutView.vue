<script setup>
import { computed } from 'vue'
import { store } from '../../lib/store'

const appVersion = __APP_VERSION__

const ruleCount = computed(() => store.configData?.rules?.length || 0)
const triggerCount = computed(() => Object.keys(store.pluginsData?.triggers || {}).length)
const actionCount = computed(() => Object.keys(store.pluginsData?.actions || {}).length)
const modeLabel = computed(() => {
  const m = store.engineStatus?.security_mode
  return ({ strict: '严格', normal: '标准', permissive: '宽松' })[m] || '未知'
})

const features = [
  { icon: 'extension', title: '插件化架构', desc: '触发器与动作作为插件独立加载，支持签名校验、权限声明与平台兼容性检查' },
  { icon: 'account_tree', title: '规则引擎', desc: 'AND / OR 可嵌套条件树，多动作顺序执行，后续步骤可引用上一步结果' },
  { icon: 'shield', title: '安全隔离', desc: 'Ed25519 插件签名、管理员权限分级、危险能力声明扫描' },
  { icon: 'monitor_heart', title: '可观测性', desc: '结构化执行日志、动作成败统计、触发器崩溃记录与 SSE 实时事件' },
]

const ruleFlow = [
  { icon: 'sensors', step: '触发条件', desc: '定时、热键、进程、剪贴板、文件夹、设备、电源与网络状态' },
  { icon: 'fact_check', step: '执行前检查', desc: '可选：确认文件仍在写入、文档还在编辑时再决定是否继续' },
  { icon: 'play_circle', step: '动作流水线', desc: '亮度 / 音量、启动或结束程序、文件操作、截图、通知、HTTP 请求' },
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
          <span class="material-symbols-outlined about-brand-mark">manufacturing</span>
          <div>
            <p class="about-kicker">LOCAL AUTOMATION ENGINE</p>
            <h3>NotmyFault</h3>
          </div>
        </div>
        <div class="about-badges">
          <span>{{ appVersion }}</span>
          <span>Alpha</span>
          <span>GPL-3.0</span>
        </div>
        <p>
          本地桌面自动化工具：用「条件 → 检查 → 动作」的方式告诉电脑以后该怎么做。
          引擎在后台独立运行，关闭管理界面不影响已经启动的自动化。
        </p>
      </header>

      <main class="about-content">
        <div class="about-main-column">
          <section class="about-section">
            <header class="about-section-head">
              <p>能力</p>
              <h4>核心特性</h4>
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
              <p>规则模型</p>
              <h4>从触发到执行</h4>
            </header>
            <p class="about-section-lead">一条规则由三个连续阶段组成，规则页面负责编辑与校验，无需手写 JSON。</p>
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
              <p>当前环境</p>
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
              <p>兼容性</p>
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
              <p>构成</p>
              <h4>技术栈</h4>
            </header>
            <div>
              <span v-for="t in ['Python 3.11+', 'Vue 3', 'Material 3', 'FastAPI', 'SSE']" :key="t">{{ t }}</span>
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
  </section>
</template>
