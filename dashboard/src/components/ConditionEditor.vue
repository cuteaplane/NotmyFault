<script setup>
import { computed } from 'vue'
import { store } from '../lib/store'
import { buildDefaultParams, getVisibleParamDefs, groupTriggerKeys } from '../lib/utils'
import ParamInput from './ParamInput.vue'

defineOptions({ name: 'ConditionEditor' })

const props = defineProps({ node: Object, nested: Boolean })
const emit = defineEmits(['remove'])

const triggerKeys = computed(() => Object.keys(store.schema.triggers))
const triggerGroups = computed(() => groupTriggerKeys(triggerKeys.value))
const isLeaf = (node) => !!node?.type && !node.children && !node.events
function eventParams(event) { return getVisibleParamDefs(store.schema.triggers[event.type], event.params) }
function defaultEvent() {
  const type = triggerKeys.value[0] || ''
  return { type, params: buildDefaultParams(store.schema.triggers[type]) }
}
function changeEvent(index, type) {
  props.node.children[index] = { type, params: buildDefaultParams(store.schema.triggers[type]) }
}
function changeOp() {
  // “秒内”只对 all 有意义。切回 any 时必须删除旧值，不能只是把输入框藏掉。
  if (props.node.op !== 'all') delete props.node.within_seconds
}
function addEvent() { props.node.children.push(defaultEvent()) }
function addGroup() { props.node.children.push({ op: 'any', children: [defaultEvent()] }) }
function removeChild(index) { props.node.children.splice(index, 1) }
</script>

<template>
  <section class="condition-group" :class="{ nested }">
    <header class="condition-group-head">
      <span class="material-symbols-outlined">{{ node.op === 'all' ? 'checklist' : 'alt_route' }}</span>
      <select v-model="node.op" class="select condition-op" @change="changeOp">
        <option value="any">满足任一条件</option>
        <option value="all">同时满足全部条件</option>
      </select>
      <label v-if="node.op === 'all'" class="condition-window">
        <span>发生在</span>
        <input v-model.number="node.within_seconds" type="number" min="1" class="text-field">
        <span>秒内</span>
      </label>
      <button v-if="nested" class="icon-btn icon-btn-danger" title="移除此条件组" @click="emit('remove')">
        <span class="material-symbols-outlined">close</span>
      </button>
    </header>

    <div class="condition-children">
      <template v-for="(child, index) in node.children" :key="index">
        <div v-if="isLeaf(child)" class="rule-item condition-event">
          <span class="condition-number">条件 {{ index + 1 }}</span>
          <label class="field field-narrow"><span class="field-label">当…</span>
            <select class="select" :value="child.type" @change="changeEvent(index, $event.target.value)">
              <optgroup v-for="([group, keys]) in triggerGroups" :key="group" :label="group">
                <option v-for="key in keys" :key="key" :value="key">{{ store.schema.triggers[key].name || key }}</option>
              </optgroup>
            </select>
          </label>
          <ParamInput v-for="param in eventParams(child)" :key="param.name" :def="param" v-model="child.params[param.name]" />
          <button class="icon-btn icon-btn-danger" title="移除条件" @click="removeChild(index)">
            <span class="material-symbols-outlined">close</span>
          </button>
        </div>
        <ConditionEditor v-else :node="child" nested @remove="removeChild(index)" />
      </template>
    </div>

    <footer class="rule-actions-bar condition-actions">
      <button class="btn btn-text btn-sm" @click="addEvent"><span class="material-symbols-outlined">add</span>添加触发条件</button>
      <button class="btn btn-text btn-sm" @click="addGroup"><span class="material-symbols-outlined">account_tree</span>添加条件组</button>
    </footer>
  </section>
</template>
