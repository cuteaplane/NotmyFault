<script setup>
import { computed } from 'vue'
import { store } from '../lib/store'
import { createBindingId, variableBindingSources, expandBindingSources, collectReferences } from '../lib/bindings'
import { defaultTypedValue } from '../lib/valueTypes'
import TypePicker from './TypePicker.vue'
import ParamInput from './ParamInput.vue'
const props = defineProps({ rule: Object })
const constants = computed(() => expandBindingSources(variableBindingSources(props.rule, { constantsOnly: true }), store.schema.data_types?.custom))
function add(field) {
  const constant = field === 'constants'
  ;(props.rule[field] ||= []).push({ id: createBindingId(constant ? 'constant' : 'variable'), name: constant ? '新常量' : '新变量', value_type: 'text', [constant ? 'value' : 'initial']: '' })
}
function references(id) { return collectReferences(props.rule).filter(ref => ref.node === id).length }
</script>
<template>
  <details class="variables-editor">
    <summary><span class="material-symbols-outlined">data_object</span>常量与变量 <span>{{ (rule.constants?.length || 0) + (rule.variables?.length || 0) }}</span></summary>
    <p class="inspector-lead">常量可用于触发配置和动作。变量在每次运行开始时独立初始化，可通过赋值动作更新。</p>
    <section v-for="field in ['constants', 'variables']" :key="field">
      <h3>{{ field === 'constants' ? '规则常量' : '运行变量' }}</h3>
      <article v-for="(item, index) in rule[field] || []" :key="item.id" class="variable-definition">
        <div class="variable-heading"><input v-model="item.name" class="text-field" aria-label="常量或变量名称"><button class="icon-btn danger-text" :title="references(item.id) ? `删除后有 ${references(item.id)} 处引用需要修改` : '删除定义'" @click="rule[field].splice(index, 1)"><span class="material-symbols-outlined">delete</span></button></div>
        <TypePicker v-model="item.value_type" />
        <label><input v-model="item.sensitive" type="checkbox">内容敏感，在运行记录中隐藏</label>
        <label v-if="field === 'variables'"><input type="checkbox" :checked="Object.hasOwn(item, 'initial')" @change="$event.target.checked ? item.initial = defaultTypedValue(item.value_type) : delete item.initial">设置初始值</label>
        <ParamInput v-if="field === 'constants' || Object.hasOwn(item, 'initial')" :def="{ name: item.id, label: field === 'constants' ? '常量值' : '初始值', type: 'string', value_type: item.value_type }" v-model="item[field === 'constants' ? 'value' : 'initial']" allow-binding :binding-sources="constants.filter(source => source.value.$ref.node !== item.id)" />
        <small>{{ item.id }} · {{ references(item.id) }} 处引用</small>
      </article>
      <button class="btn btn-tonal btn-sm" @click="add(field)"><span class="material-symbols-outlined">add</span>{{ field === 'constants' ? '添加常量' : '添加变量' }}</button>
    </section>
  </details>
</template>
<style scoped>
.variables-editor { padding: 12px 20px; border-bottom: 1px solid var(--md-outline-variant); max-height: 55vh; overflow-y: auto; }
summary { display: flex; align-items: center; gap: 8px; cursor: pointer; font-weight: 500; }
section { display: grid; gap: 12px; margin: 16px 0; }
h3 { font-size: 14px; margin: 0; }
.variable-definition { display: grid; gap: 10px; padding: 12px; border: 1px solid var(--md-outline-variant); border-radius: 12px; }
.variable-heading { display: flex; align-items: center; gap: 8px; }
.variable-heading input { flex: 1; }
small { color: var(--md-on-surface-variant); }
</style>
