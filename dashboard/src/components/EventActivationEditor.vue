<script setup>
import { computed, ref } from 'vue'
import TriggerForm from './TriggerForm.vue'
import { store } from '../lib/store'
defineOptions({ name: 'EventActivationEditor' })
const props = defineProps({ node: Object, rule: Object, sources: Array, allowGroup: { type: Boolean, default: true } })
const emit = defineEmits(['pick', 'remove', 'simplify'])
function changeOperator() {
  const node = props.node
  if (node.op !== 'all') { delete node.consume; delete node.ttl_seconds }
  if (node.op !== 'any') delete node.pick
  if (node.op !== 'not') delete node.repeat
  if (node.op === 'any') { delete node.within_seconds; node.pick ||= 'latest' }
  if (node.op === 'all') node.consume ||= 'matched'
  if (node.op === 'not') { node.repeat ||= 'once_until_seen'; node.within_seconds ||= 60 }
}
function seconds(field, value) { if (value === '') delete props.node[field]; else props.node[field] = Number(value) }
function leaves(node) { return node?.type ? [node] : (node?.children || []).flatMap(leaves) }
const singleChoices = computed(() => leaves(props.node))
const choosingSingle = ref(false)
const singleId = ref('')
function requestSingle() { singleId.value = singleChoices.value[0]?.binding_id || ''; choosingSingle.value = true }
function confirmSingle() { const leaf = singleChoices.value.find(leaf => leaf.binding_id === singleId.value); if (leaf) emit('simplify', leaf); choosingSingle.value = false }
const canUseNot = computed(() => props.node.children?.length === 1 && !!props.node.children[0]?.type)
const group = computed(() => !!props.node?.op)
</script>
<template>
  <section v-if="node" class="event-activation-editor">
    <template v-if="group">
      <label class="field"><span class="field-label">事件组合</span><select v-model="node.op" class="select" @change="changeOperator"><option value="any">任一事件 · OR</option><option value="all">全部事件 · AND</option><option value="not" :disabled="!canUseNot">连续未发生 · NOT</option></select></label>
      <p v-if="!canUseNot" class="inspector-lead">NOT 只能判断一个事件。选择“改为单个条件”保留指定事件后，再设置 NOT。</p>
      <label v-if="node.op !== 'any'" class="field"><span class="field-label">时间窗口（秒）</span><input :value="node.within_seconds ?? ''" @input="seconds('within_seconds', $event.target.value)" type="number" min="0.001" step="any" class="text-field"></label>
      <label v-if="node.op === 'all'" class="field"><span class="field-label">满足后保留哪些事件</span><select v-model="node.consume" class="select"><option value="matched">移除本次使用的事件</option><option value="all">移除组内已收到的事件</option><option value="none">保留事件</option></select></label>
      <label v-if="node.op === 'not'" class="field"><span class="field-label">持续没有事件时</span><select v-model="node.repeat" class="select"><option value="once_until_seen">触发一次，收到事件后重新计时</option><option value="every_window">每个等待窗口触发一次</option></select></label>
      <label v-if="node.op === 'any'" class="field"><span class="field-label">多条事件满足时</span><select v-model="node.pick" class="select"><option value="latest">使用最新满足的事件</option><option value="first">使用列表中第一条事件</option></select></label>
      <label v-if="node.op === 'all'" class="field"><span class="field-label">事件保留时长（秒）</span><input :value="node.ttl_seconds ?? ''" @input="seconds('ttl_seconds', $event.target.value)" class="text-field" type="number" min="0.001" step="any" placeholder="3600"></label>
      <EventActivationEditor v-for="(child, index) in node.children || []" :key="child.binding_id || index" :node="child" :rule="rule" :sources="sources" :allow-group="node.op !== 'not'" @pick="emit('pick', $event)" @remove="node.children.splice(index, 1)" @simplify="replacement => node.children[index] = replacement" />
      <button class="btn btn-text btn-sm" @click="requestSingle">改为单个条件</button>
      <button v-if="node.op !== 'not'  || !node.children?.length" class="btn btn-text btn-sm" @click="emit('pick', node)"><span class="material-symbols-outlined">bolt</span>添加事件</button>
    </template>
    <template v-else><TriggerForm :node="node" :rule="rule" :sources="sources" @replace="emit('pick', node)" /><button v-if="allowGroup" class="btn btn-text btn-sm" @click="emit('simplify', { op: 'any', children: [node], pick: 'latest' })">改为事件组合</button></template>
    <div v-if="choosingSingle" class="workflow-branch-settings"><p>选择要保留的事件。此组合中的其他事件将被移除，引用这些事件的条件也需要调整。</p><select v-model="singleId" class="select" aria-label="保留事件"><option v-for="leaf in singleChoices" :key="leaf.binding_id" :value="leaf.binding_id">{{ store.schema.triggers?.[leaf.type]?.name || leaf.type }} · {{ Object.values(leaf.params || {}).filter(value => typeof value !== 'object').join('、') || leaf.binding_id }}</option></select><div class="inspector-action-row"><button class="btn btn-text" @click="choosingSingle = false">取消</button><button class="btn btn-tonal" :disabled="!singleId" @click="confirmSingle">保留选定事件</button></div></div>
    <button class="btn btn-text btn-sm danger-text" @click="emit('remove')">移除此事件条件</button>
  </section>
</template>
<style scoped>.event-activation-editor{display:grid;gap:12px;padding:12px;border:1px solid var(--md-outline-variant);border-radius:var(--r-sm)}.event-activation-editor .event-activation-editor{border-left:3px solid var(--md-primary)}</style>
