<script setup>
import { computed, ref, watch } from 'vue'
import { typesCompatible } from '../lib/bindings'
import { typeAtPath, typeLabel, typeSpec, defaultTypedValue } from '../lib/valueTypes'
import { store } from '../lib/store'
import TypedValueInput from './TypedValueInput.vue'

const props = defineProps({
  sources: { type: Array, default: () => [] },
  targetType: { type: [String, Object], default: 'any' },
})
const emit = defineEmits(['select', 'cancel'])
const selected = ref('')
const pathText = ref('[]'), policy = ref('error'), convert = ref(false)
const fallback = ref(null), rounding = ref('exact'), timezone = ref('preserve')
const variantIndex = ref(0)
const conversionVariants = computed(() => typeSpec(props.targetType).type === 'union' ? typeSpec(props.targetType).variants : [])
const conversionTarget = computed(() => conversionVariants.value?.length ? conversionVariants.value[variantIndex.value] : props.targetType)
const groups = computed(() => {
  const grouped = new Map()
  props.sources
    .forEach(source => {
      if (!grouped.has(source.group)) grouped.set(source.group, [])
      grouped.get(source.group).push(source)
    })
  return [...grouped]
})
const selectedSource = computed(() => (
  props.sources.find(item => JSON.stringify(item.value) === selected.value) || null
))
watch(selectedSource, source => { pathText.value = '[]'; convert.value = false; fallback.value = defaultTypedValue(source?.type || 'text') })
const selectedPath = computed(() => {
  try {
    const path = JSON.parse(pathText.value)
    if (!Array.isArray(path)) throw new Error('路径需要 JSON 数组')
    const catalog = Object.entries(store.schema.data_types?.custom || {}).map(([id, item]) => ({ id, ...item }))
    return { path, ...typeAtPath(selectedSource.value?.type, path, catalog) }
  } catch (e) { return { error: e.message } }
})
const compatible = computed(() => !selectedPath.value.error && typesCompatible(selectedPath.value.type, props.targetType))
const showPath = computed(() => ['array', 'object', 'union', 'any'].includes(typeSpec(selectedSource.value?.type).type) || typeSpec(selectedSource.value?.type).type.includes('/'))

function apply(event) {
  if ([...event.currentTarget.closest('.binding-picker').querySelectorAll('input,textarea,select')].some(control => !control.reportValidity())) return
  if (!selectedSource.value || selectedPath.value.error || !compatible.value && !convert.value) return
  const value = JSON.parse(JSON.stringify(selectedSource.value.value))
  value.$ref.path.push(...selectedPath.value.path)
  value.$ref.on_missing = policy.value
  if (policy.value === 'default') value.$ref.default = { $literal: fallback.value }
  emit('select', convert.value ? { $convert: { value, to: conversionTarget.value, options: { rounding: rounding.value, timezone: timezone.value } } } : value)
}
</script>

<template>
  <div class="binding-picker">
    <select v-model="selected" class="select">
      <option value="" disabled>选择本次运行产生的数据…</option>
      <optgroup v-for="([group, items]) in groups" :key="group" :label="group">
        <option v-for="item in items" :key="JSON.stringify(item.value)" :value="JSON.stringify(item.value)">
          {{ item.sensitive ? '【敏感】' : '' }}{{ item.label }} · {{ typeLabel(item.type) }}{{ item.optional ? '（可能缺失）' : '' }}
        </option>
      </optgroup>
    </select>
    <template v-if="selectedSource">
      <label v-if="showPath" class="field"><span class="field-label">继续提取字段或数组元素</span><input v-model="pathText" class="text-field" placeholder='例如 [0, "名称"]' aria-label="数据路径"><span class="inspector-lead">[] 使用完整值；[0] 使用数组第一个元素。</span></label>
      <p v-if="selectedPath.error" class="danger-text">{{ selectedPath.error }}</p>
      <p v-else class="inspector-lead">{{ typeLabel(selectedPath.type) }} → {{ typeLabel(targetType) }}</p>
      <label class="field"><span class="field-label">数据缺失时</span><select v-model="policy" class="select"><option value="error">报错</option><option value="skip">跳过本动作</option><option value="default">使用默认值</option></select></label>
      <TypedValueInput v-if="policy === 'default'" v-model="fallback" :value-type="selectedPath.type || selectedSource.type" label="缺失时的默认值" />
      <label><input v-model="convert" type="checkbox">转换为{{ typeLabel(targetType) }}</label>
      <p v-if="!compatible && !convert && !selectedPath.error" class="danger-text">这两个类型不能直接传递，请选择转换或另一个来源。</p>
      <template v-if="convert">
        <label v-if="conversionVariants.length" class="field"><span class="field-label">转换成其中一种类型</span><select v-model="variantIndex" class="select"><option v-for="(variant, index) in conversionVariants" :key="index" :value="index">{{ typeLabel(variant) }}</option></select></label>
        <label class="field"><span class="field-label">转换整数时</span><select v-model="rounding" class="select"><option value="exact">必须是整数</option><option value="truncate">截去小数</option><option value="floor">向下取整</option><option value="ceil">向上取整</option><option value="round">取最近整数</option></select></label>
        <label class="field"><span class="field-label">日期时间转换时区</span><input v-model="timezone" class="text-field" placeholder="preserve、utc、local 或 Asia/Shanghai"></label>
      </template>
    </template>
    <!-- 插件把剪贴板等输出标为 sensitive，绑定后会把内容传给动作。 -->
    <p v-if="selectedSource?.sensitive" class="flex items-start gap-1.5 text-label-m text-warn">
      <span class="material-symbols-outlined text-[16px] leading-5">warning</span>
      <span>此数据被插件标记为敏感，可能包含隐私内容。绑定后，规则每次触发都会把它传给该动作使用。</span>
    </p>
    <div class="binding-picker-actions">
      <button type="button" class="btn btn-tonal btn-sm" :disabled="!selected || !!selectedPath.error || !compatible && !convert" @click="apply">使用</button>
      <button type="button" class="btn btn-text btn-sm" @click="$emit('cancel')">取消</button>
    </div>
  </div>
</template>
