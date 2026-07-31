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
  <section class="page active">
    <div class="page-head"><h2>关于</h2></div>

    <div class="flex flex-col gap-4">
      <!-- Hero：品牌 + 版本徽章 + 一句话简介 -->
      <div class="rounded-lg bg-surface-c-low p-6 shadow-elev1">
        <div class="flex items-center gap-4">
          <span class="material-symbols-outlined text-[56px] leading-none text-primary" style="font-variation-settings:'FILL' 1">manufacturing</span>
          <div class="min-w-0">
            <h3 class="text-headline-s text-on-surface">NotmyFault</h3>
            <div class="mt-2 flex flex-wrap items-center gap-2">
              <span class="chip chip-origin-builtin">{{ appVersion }}</span>
              <span class="chip">Alpha 测试版</span>
              <span class="chip">GPL-3.0</span>
            </div>
          </div>
        </div>
        <p class="mt-4 max-w-2xl text-body-m text-on-surface-variant">
          本地桌面自动化工具：用「条件 → 检查 → 动作」的方式告诉电脑以后该怎么做。
          引擎在后台独立运行，关闭管理界面不影响已经启动的自动化。
        </p>
      </div>

      <!-- 核心特性：2×2 网格 -->
      <div class="rounded-lg bg-surface-c-low p-6 shadow-elev1">
        <h4 class="mb-4 text-title-m text-on-surface">核心特性</h4>
        <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div v-for="f in features" :key="f.title"
            class="flex items-start gap-3 rounded-sm border border-outline-variant bg-surface-c-lowest p-4 transition-all duration-200 hover:-translate-y-0.5 hover:shadow-elev1">
            <span class="material-symbols-outlined mt-0.5 text-[22px] text-primary">{{ f.icon }}</span>
            <div class="min-w-0">
              <b class="block text-title-s text-on-surface">{{ f.title }}</b>
              <p class="mt-1 text-body-s text-on-surface-variant">{{ f.desc }}</p>
            </div>
          </div>
        </div>
      </div>

      <!-- 规则模型：三步流程 -->
      <div class="rounded-lg bg-surface-c-low p-6 shadow-elev1">
        <h4 class="mb-1 text-title-m text-on-surface">规则模型</h4>
        <p class="mb-5 text-body-s text-on-surface-variant">一条规则由三部分组成，一般不需要手写 JSON，规则页面会负责编辑和校验。</p>
        <div class="grid grid-cols-1 gap-3 md:grid-cols-3">
          <template v-for="(s, i) in ruleFlow" :key="s.step">
            <div class="relative flex flex-col gap-2 rounded-sm border border-outline-variant bg-surface-c-lowest p-4">
              <div class="flex items-center gap-2.5">
                <span class="flex h-9 w-9 items-center justify-center rounded-full bg-primary-container text-on-primary-container">
                  <span class="material-symbols-outlined text-[20px]">{{ s.icon }}</span>
                </span>
                <span class="text-title-s text-on-surface">{{ s.step }}</span>
              </div>
              <p class="text-body-s text-on-surface-variant">{{ s.desc }}</p>
              <span v-if="i < ruleFlow.length - 1"
                class="material-symbols-outlined absolute top-1/2 -right-[15px] z-10 hidden -translate-y-1/2 text-[20px] text-outline md:block">arrow_forward</span>
            </div>
          </template>
        </div>
      </div>

      <!-- 运行信息：动态读取引擎与配置 -->
      <div class="rounded-lg bg-surface-c-low p-6 shadow-elev1">
        <h4 class="mb-4 text-title-m text-on-surface">运行信息</h4>
        <div class="grid grid-cols-1 gap-x-8 gap-y-3 sm:grid-cols-2">
          <div v-for="r in runtimeInfo" :key="r.key" class="flex items-center justify-between gap-4 border-b border-outline-variant pb-3">
            <span class="flex items-center gap-2 text-body-m text-on-surface-variant">
              <span class="material-symbols-outlined text-[18px] text-primary">{{ r.icon }}</span>{{ r.key }}
            </span>
            <span class="text-right text-body-m font-medium text-on-surface">{{ r.val }}</span>
          </div>
        </div>
      </div>

      <!-- 平台支持 -->
      <div class="rounded-lg bg-surface-c-low p-6 shadow-elev1">
        <h4 class="mb-4 text-title-m text-on-surface">平台支持</h4>
        <div class="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <div v-for="p in platforms" :key="p.name"
            class="flex items-center gap-3 rounded-sm border border-outline-variant bg-surface-c-lowest px-4 py-3">
            <span class="material-symbols-outlined text-[24px] text-on-surface-variant">{{ p.icon }}</span>
            <div class="min-w-0">
              <b class="block text-label-l text-on-surface">{{ p.name }}</b>
              <span class="text-label-m" :class="p.cls">{{ p.state }}</span>
            </div>
          </div>
        </div>
        <p class="mt-4 text-body-s text-on-surface-variant">
          部分功能依赖操作系统或硬件支持，例如 Windows 全局热键、窗口标题检测与外接显示器亮度控制。
          配置与日志位于 <code class="rounded-xs bg-surface-c-high px-1.5 py-0.5 text-label-m text-on-surface">%APPDATA%\NotmyFault\</code>（Linux 为 <code class="rounded-xs bg-surface-c-high px-1.5 py-0.5 text-label-m text-on-surface">~/.config/notmyfault/</code>）。
        </p>
      </div>

      <!-- 技术栈与版权 -->
      <div class="rounded-lg bg-surface-c-low p-6 shadow-elev1">
        <h4 class="mb-4 text-title-m text-on-surface">技术栈</h4>
        <div class="flex flex-wrap gap-2">
          <span v-for="t in ['Python 3.11+ 引擎', 'Vue 3 Dashboard', 'Material Design 3', 'FastAPI 本地服务', 'SSE 实时事件']" :key="t" class="chip">{{ t }}</span>
        </div>
        <p class="mt-5 text-body-s text-outline">
          &copy; 2026 NotmyFault Project · 基于 GNU General Public License v3.0 开源 ·
          遇到问题时请附上操作系统、复现步骤与最新日志文件
        </p>
      </div>
    </div>
  </section>
</template>
