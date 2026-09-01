<script setup>
import { computed, ref } from 'vue'
import { store } from '../lib/store'
import { buildDefaultParams, ensureParams, getVisibleParamDefs, groupTriggerKeys, pluginUnavailableReason } from '../lib/utils'
import { createBindingId } from '../lib/bindings'
import ParamInput from './ParamInput.vue'
import PluginPicker from './PluginPicker.vue'

defineOptions({ name: 'ConditionEditor' })

const props = defineProps({ node: Object, nested: Boolean })
const emit = defineEmits(['remove'])

// 旧配置或手写配置可能没有 children，这里补成空数组。
if (!Array.isArray(props.node.children)) props.node.children = []

const triggerKeys = computed(() => Object.keys(store.schema.triggers).filter(
  key => !pluginUnavailableReason(store.schema.triggers[key])
))
const triggerGroups = computed(() => groupTriggerKeys(triggerKeys.value))
const pickerOpen = ref(false)
const pickerMode = ref('event')
const pickerIndex = ref(null)
const pickerTitle = computed(() => ({
  group: '添加条件组的第一个条件',
  replace: '更换触发方式',
})[pickerMode.value] || '添加条件')
const isLeaf = (node) => !!node?.type && !node.children && !node.events
const isObjectNode = (node) => !!node && typeof node === 'object' && !Array.isArray(node)
const eventName = (event) => store.schema.triggers[event?.type]?.name || event?.type || '未选择触发器'
const eventParams = (event) => getVisibleParamDefs(store.schema.triggers[event.type], ensureParams(event))

function defaultEvent(type = triggerKeys.value[0] || '') {
  // 新条件带有 binding_id，绑定选择器用它定位运行数据。
  return { binding_id: createBindingId('trigger'), type, params: buildDefaultParams(store.schema.triggers[type]) }
}
function changeEvent(index, type) {
  const previous = props.node.children[index]
  // 更换触发器类型时沿用 binding_id，下游 $ref 仍指向同一条件。
  props.node.children[index] = {
    binding_id: previous?.binding_id || createBindingId('trigger'),
    type,
    params: buildDefaultParams(store.schema.triggers[type]),
  }
}
function changeOp() {
  if (props.node.op !== 'all') delete props.node.within_seconds
}
function addEvent() {
  if (!triggerKeys.value.length) return
  pickerMode.value = 'event'
  pickerIndex.value = null
  pickerOpen.value = true
}
function addGroup() {
  if (!triggerKeys.value.length) return
  pickerMode.value = 'group'
  pickerIndex.value = null
  pickerOpen.value = true
}
function replaceEvent(index) {
  pickerMode.value = 'replace'
  pickerIndex.value = index
  pickerOpen.value = true
}
function chooseTrigger(type) {
  if (pickerMode.value === 'replace' && Number.isInteger(pickerIndex.value)) changeEvent(pickerIndex.value, type)
  else if (pickerMode.value === 'group') props.node.children.push({ op: 'any', children: [defaultEvent(type)] })
  else props.node.children.push(defaultEvent(type))
  pickerOpen.value = false
  pickerIndex.value = null
}
function removeChild(index) { props.node.children.splice(index, 1) }
function moveChild(index, offset) {
  const target = index + offset
  if (target < 0 || target >= props.node.children.length) return
  const [child] = props.node.children.splice(index, 1)
  props.node.children.splice(target, 0, child)
}
</script>

<template>
  <section class="flow-condition-group" :class="{ nested }">
    <header class="flow-condition-head">
      <span class="material-symbols-outlined">{{ node.op === 'all' ? 'done_all' : 'alt_route' }}</span>
      <select v-model="node.op" class="select condition-op" @change="changeOp">
        <option value="any">满足以下任一条件</option>
        <option value="all">以下条件需全部满足</option>
      </select>
      <label v-if="node.op === 'all'" class="condition-window">
        <span>在</span>
        <input v-model.number="node.within_seconds" type="number" min="1" class="text-field">
        <span>秒内</span>
      </label>
      <button v-if="nested" class="icon-btn icon-btn-danger" title="移除此条件组" @click="emit('remove')">
        <span class="material-symbols-outlined">delete</span>
      </button>
    </header>

    <div v-if="!node.children?.length" class="flow-inline-empty">这个条件组还是空的，请添加一个触发条件。</div>
    <div class="flow-condition-children">
      <template v-for="(child, index) in node.children" :key="child">
        <div v-if="index" class="condition-joiner"><span>{{ node.op === 'all' ? '并且' : '或者' }}</span></div>
        <details v-if="isLeaf(child)" class="flow-card condition-flow-card" :open="node.children.length === 1">
          <summary>
            <span class="flow-card-index">{{ index + 1 }}</span>
            <span class="flow-card-copy"><b>{{ eventName(child) }}</b><small>触发条件</small></span>
            <span class="flow-card-tools">
              <button class="icon-btn" :disabled="index === 0" title="上移" @click.prevent.stop="moveChild(index, -1)"><span class="material-symbols-outlined">arrow_upward</span></button>
              <button class="icon-btn" :disabled="index === node.children.length - 1" title="下移" @click.prevent.stop="moveChild(index, 1)"><span class="material-symbols-outlined">arrow_downward</span></button>
              <button class="icon-btn icon-btn-danger" title="移除条件" @click.prevent.stop="removeChild(index)"><span class="material-symbols-outlined">delete</span></button>
            </span>
            <span class="material-symbols-outlined flow-expand">expand_more</span>
          </summary>
          <div class="flow-card-body">
            <div class="field field-wide"><span class="field-label">触发方式</span>
              <button class="plugin-type-button" type="button" @click="replaceEvent(index)">
                <span class="material-symbols-outlined">bolt</span>
                <span>{{ eventName(child) }}</span>
                <span class="material-symbols-outlined">arrow_forward</span>
              </button>
            </div>
            <div class="param-grid"><ParamInput v-for="param in eventParams(child)" :key="param.name" :def="param" :plugin-id="child.type" v-model="child.params[param.name]" /></div>
          </div>
        </details>
        <ConditionEditor v-else-if="isObjectNode(child)" :node="child" nested @remove="removeChild(index)" />
        <div v-else class="flow-inline-empty">
          条件 {{ index + 1 }} 格式无效。
          <button class="btn btn-text btn-sm danger-text" @click="removeChild(index)">移除</button>
        </div>
      </template>
    </div>

    <footer class="flow-add-row">
      <button class="btn btn-text btn-sm" :disabled="!triggerKeys.length" @click="addEvent"><span class="material-symbols-outlined">add</span>添加条件</button>
      <button class="btn btn-text btn-sm" :disabled="!triggerKeys.length" @click="addGroup"><span class="material-symbols-outlined">account_tree</span>添加条件组</button>
    </footer>
    <PluginPicker :open="pickerOpen" kind="trigger" :keys="triggerKeys" :groups="triggerGroups"
      :title="pickerTitle"
      @close="pickerOpen = false" @select="chooseTrigger" />
  </section>
</template>
