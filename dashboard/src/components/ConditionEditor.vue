<script setup>
import { computed } from 'vue'
import { store } from '../lib/store'
import { buildDefaultParams, getVisibleParamDefs, groupTriggerKeys } from '../lib/utils'
import { createBindingId } from '../lib/bindings'
import ParamInput from './ParamInput.vue'

defineOptions({ name: 'ConditionEditor' })

const props = defineProps({ node: Object, nested: Boolean })
const emit = defineEmits(['remove'])

// 允许用户在界面中修复旧配置或手写配置里的缺失 children 字段。
if (!Array.isArray(props.node.children)) props.node.children = []

const triggerKeys = computed(() => Object.keys(store.schema.triggers).filter(
  key => store.schema.triggers[key]?.platform_compatible !== false
))
const triggerGroups = computed(() => groupTriggerKeys(triggerKeys.value))
const isLeaf = (node) => !!node?.type && !node.children && !node.events
const isObjectNode = (node) => !!node && typeof node === 'object' && !Array.isArray(node)
const eventName = (event) => store.schema.triggers[event?.type]?.name || event?.type || '未选择触发器'
const eventParams = (event) => getVisibleParamDefs(store.schema.triggers[event.type], event.params)

function defaultEvent() {
  const type = triggerKeys.value[0] || ''
  // 新条件必须带 binding_id，否则无法作为运行数据来源出现在绑定选择器中
  return { binding_id: createBindingId('trigger'), type, params: buildDefaultParams(store.schema.triggers[type]) }
}
function changeEvent(index, type) {
  const previous = props.node.children[index]
  // 更换触发器类型时保留 binding_id，避免下游 $ref 引用失效
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
  props.node.children.push(defaultEvent())
}
function addGroup() {
  if (!triggerKeys.value.length) return
  props.node.children.push({ op: 'any', children: [defaultEvent()] })
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
            <label class="field field-wide"><span class="field-label">触发方式</span>
              <select class="select" :value="child.type" @change="changeEvent(index, $event.target.value)">
                <optgroup v-for="([group, keys]) in triggerGroups" :key="group" :label="group">
                  <option v-for="key in keys" :key="key" :value="key">{{ store.schema.triggers[key].name || key }}</option>
                </optgroup>
              </select>
            </label>
            <div class="param-grid"><ParamInput v-for="param in eventParams(child)" :key="param.name" :def="param" v-model="child.params[param.name]" /></div>
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
  </section>
</template>
