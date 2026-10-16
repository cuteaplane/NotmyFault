<script setup>
import ActionRunLimits from './ActionRunLimits.vue'
const props = defineProps({ action: { type: Object, required: true }, meta: { type: Object, default: () => ({}) }, recoveryGroup: { type: Object, default: null }, inRecovery: Boolean })
const emit = defineEmits(['create-recovery', 'edit-recovery', 'disconnect-recovery'])
function setOnError(event) {
  if (event.target.value === 'continue') props.action.on_error = 'continue'
  else delete props.action.on_error
}
</script>
<template>
  <section class="action-failure-settings">
    <header class="action-failure-head"><span class="material-symbols-outlined">report</span><div><b>这个动作失败以后</b><small>重试耗尽后执行失败路线</small></div></header>
    <p v-if="inRecovery" class="inspector-lead">补救中的动作失败时，停止本次运行并记为失败。</p>
    <label v-else class="field action-failure-policy"><span class="field-label">补救完成后</span>
      <select class="select" :value="action.on_error === 'continue' ? 'continue' : 'stop'" @change="setOnError"><option value="stop">停止本次运行</option><option value="continue">继续原动作的正常后继</option></select>
    </label>
    <ActionRunLimits :action="action" :meta="meta" />
    <section v-if="!inRecovery" class="recovery-graph-settings">
      <p class="inspector-lead">{{ recoveryGroup ? '失败时进入此动作组。补救失败会停止运行；补救成功后按上面的选择返回。' : action.on_error === 'continue' ? '未连接补救组：忽略本次错误，继续正常后继。' : '未连接补救组：动作失败时停止运行并记为失败。' }}</p>
      <template v-if="recoveryGroup"><button class="btn btn-tonal" @click="emit('edit-recovery')"><span class="material-symbols-outlined">account_tree</span>{{ recoveryGroup.name || '补救动作组' }}</button><button class="btn btn-text danger-text" @click="emit('disconnect-recovery')">断开失败连线</button></template>
      <button v-else class="btn btn-tonal" @click="emit('create-recovery')"><span class="material-symbols-outlined">add</span>创建补救动作组</button>
      <p v-if="recoveryGroup && action.on_error !== 'continue'" class="inspector-lead">补救成功后结束运行，状态为“已补救”。</p>
    </section>
  </section>
</template>
