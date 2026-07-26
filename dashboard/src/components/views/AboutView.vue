<script setup>
const appVersion = __APP_VERSION__
</script>
<template>
  <section class="page active">
    <div class="page-head"><h2>关于</h2></div>
    <div class="about-hero"><span class="material-symbols-outlined about-logo">manufacturing</span>
      <div><h3>NotmyFault</h3><p class="about-ver">{{ appVersion }} · 2026</p></div></div>
    <div class="about-card">
      <p class="about-desc">轻量级 Windows 系统自动化工具。设置触发条件与动作，让电脑在你需要时自动响应。</p>
      <div class="about-features">
        <div class="about-feat"><span class="material-symbols-outlined">extension</span><div><b>插件化架构</b><small>触发器与动作皆可扩展，支持签名校验</small></div></div>
        <div class="about-feat"><span class="material-symbols-outlined">rule</span><div><b>规则引擎</b><small>事件匹配、OR 条件、配置热加载</small></div></div>
        <div class="about-feat"><span class="material-symbols-outlined">shield</span><div><b>安全隔离</b><small>Ed25519 插件签名、管理员权限分级、能力声明扫描</small></div></div>
        <div class="about-feat"><span class="material-symbols-outlined">monitor_heart</span><div><b>可观测性</b><small>结构化日志、诊断面板、SSE 实时事件</small></div></div>
      </div>
      <p class="about-note">引擎在后台独立运行，关闭管理界面不影响引擎工作。</p>
      <p class="about-copy">&copy; 2026 NotmyFault Project · 配置目录 %APPDATA%/NotmyFault/</p>
    </div>
  </section>
</template>
