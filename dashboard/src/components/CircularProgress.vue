<script setup>
defineProps({ size: { type: Number, default: 20 } })
</script>

<template>
  <svg class="md-circular-progress" :width="size" :height="size" viewBox="0 0 48 48" aria-hidden="true">
    <circle class="md-circular-progress-arc" cx="24" cy="24" r="19" :stroke-width="size <= 24 ? 5 : 4" />
  </svg>
</template>

<style>
/* M3 不确定进度圈：整体匀速旋转，弧线在 1% 到约 75% 的周长之间伸缩。 */
.md-circular-progress{display:inline-block;flex:none;vertical-align:middle;color:inherit;animation:md-circular-rotate 1.568s linear infinite}
.md-circular-progress-arc{fill:none;stroke:currentColor;stroke-linecap:round;stroke-dasharray:1 200;stroke-dashoffset:0;animation:md-circular-dash 1.333s cubic-bezier(.4,0,.2,1) infinite}
@keyframes md-circular-rotate{to{transform:rotate(360deg)}}
@keyframes md-circular-dash{
  0%{stroke-dasharray:1 200;stroke-dashoffset:0}
  50%{stroke-dasharray:90 200;stroke-dashoffset:-16}
  100%{stroke-dasharray:90 200;stroke-dashoffset:-119}
}
@media(prefers-reduced-motion:reduce){.md-circular-progress{animation-duration:4s}.md-circular-progress-arc{animation-duration:3s}}
</style>
