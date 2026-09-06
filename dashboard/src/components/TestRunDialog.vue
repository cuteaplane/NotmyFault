<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { actionOutputDefs, buildAllTestInputFields, buildPreparedTestContext, buildTestInputFields } from '../lib/bindings'
import { fieldType, typeLabel, typeAtPath, typeSpec, parseExactJson } from '../lib/valueTypes'
import BaseDialog from './BaseDialog.vue'

const props = defineProps({
  open: Boolean,
  ruleName: { type: String, default: '未命名规则' },
  rule: { type: Object, default: null },
  schema: { type: Object, required: true },
  savedValues: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['cancel', 'run'])

const allFields = computed(() => props.rule ? buildAllTestInputFields(props.rule, props.schema) : [])
function buildValues() {
  return Object.fromEntries(allFields.value.map(field => [
    field.key,
    Object.prototype.hasOwnProperty.call(props.savedValues, field.key)
      ? props.savedValues[field.key]
      : field.defaultValue,
  ]))
}
const values = ref(buildValues())
const enabledFields = ref({})
const errors = ref({})
const submitError = ref('')
const remember = ref(true)
const dialogRef = ref(null)
const rangeMode = ref('all')
const selectedStepId = ref('')
const assertions = ref([])

const actions = computed(() => props.rule?.actions || [])
const selectedIndex = computed(() => Math.max(0, actions.value.findIndex(action => action.binding_id === selectedStepId.value)))
const startStepId = computed(() => rangeMode.value === 'from' ? selectedStepId.value : '')
const endStepId = computed(() => rangeMode.value === 'until' ? selectedStepId.value : '')
const startIndex = computed(() => rangeMode.value === 'from' ? selectedIndex.value : 0)
const endIndex = computed(() => rangeMode.value === 'until' ? selectedIndex.value : actions.value.length - 1)
const fields = computed(() => props.rule ? buildTestInputFields(props.rule, props.schema, {
  startStepId: startStepId.value,
  endStepId: endStepId.value,
}) : [])
const selectedActionCount = computed(() => Math.max(0, endIndex.value - startIndex.value + 1))

const groups = computed(() => {
  const result = []
  const byName = new Map()
  for (const field of fields.value) {
    if (!byName.has(field.sourceName)) {
      const group = { name: field.sourceName, fields: [] }
      byName.set(field.sourceName, group)
      result.push(group)
    }
    byName.get(field.sourceName).fields.push(field)
  }
  return result
})
const sensitiveCount = computed(() => fields.value.filter(field => field.sensitive).length)
const assertionSources = computed(() => actions.value
  .slice(startIndex.value, endIndex.value + 1)
  .flatMap((action, localIndex) => {
    const actionIndex = startIndex.value + localIndex
    const meta = props.schema.actions?.[action.type]
    return actionOutputDefs(action, props.schema, props.rule)
      .filter(output => output.sensitive !== true)
      .map(output => ({
        key: `${action.binding_id}:${output.name}`,
        stepId: action.binding_id,
        path: [output.name],
        type: fieldType(output).type,
        valueType: fieldType(output),
        label: `动作 ${actionIndex + 1}：${meta?.name || action.type} · ${output.label || output.name}`,
      }))
  }))

function sourceForAssertion(assertion) {
  return assertionSources.value.find(source => source.key === assertion.sourceKey)
}

function assertionType(assertion) {
  const source = sourceForAssertion(assertion)
  const path = parseExactJson(assertion.subpath || '[]')
  if (!Array.isArray(path)) throw new Error('字段路径必须是数组，例如 [0, "名称"]')
  const catalog = Object.entries(props.schema.data_types?.custom || {}).map(([id, definition]) => ({ id, ...definition }))
  return { path: [...source.path, ...path], type: typeAtPath(source.valueType, path, catalog).type }
}

function assertionOperators(assertion) {
  let type = sourceForAssertion(assertion)?.type
  try { type = assertionType(assertion).type.type } catch {}
  const result = [{ value: 'equals', label: '等于' }, { value: 'exists', label: '存在' }]
  if (['number', 'int', 'float', 'decimal', 'timestamp', 'duration'].includes(type)) result.push(
    { value: 'gt', label: '大于' }, { value: 'gte', label: '大于等于' },
    { value: 'lt', label: '小于' }, { value: 'lte', label: '小于等于' },
  )
  if (['text', 'array', 'object', 'any'].includes(type)) result.push({ value: 'contains', label: '包含' })
  return result
}

function addAssertion() {
  const source = assertionSources.value[0]
  if (!source) return
  assertions.value.push({ sourceKey: source.key, operator: 'equals', expected: '' })
}

function removeAssertion(index) {
  assertions.value.splice(index, 1)
}

function normalizeAssertions() {
  return assertions.value.map((assertion, index) => {
    const source = sourceForAssertion(assertion)
    if (!source) throw new Error(`检查项 #${index + 1} 的动作输出不在本次运行范围内`)
    const selected = assertionType(assertion)
    const result = { step_id: source.stepId, path: selected.path, operator: assertion.operator }
    if (assertion.operator !== 'exists') {
      const valueType = assertion.operator === 'contains' ? selected.type.type === 'array' ? typeSpec(selected.type.items) : { type: 'text' } : selected.type
      const field = { type: valueType.type, valueType, required: true }
      try {
        result.expected = buildPreparedTestContext(
          [{ ...field, key: 'expected', scope: 'event', node: '', path: ['value'] }],
          { expected: assertion.expected },
        ).event_payload.value
      } catch (error) {
        throw new Error(`检查项 #${index + 1}：${error.message}`)
      }
    }
    return result
  })
}

watch(assertionSources, sources => {
  const valid = new Set(sources.map(source => source.key))
  assertions.value = assertions.value.filter(assertion => valid.has(assertion.sourceKey))
})

function inputKind(field) {
  if (['object', 'array', 'union', 'any'].includes(field.type) && field.valueType || field.fullPayload || field.type?.includes('/')) return 'json'
  if (['bool', 'boolean'].includes(field.type)) return 'boolean'
  if (['number', 'float'].includes(field.type)) return 'number'
  return field.sensitive ? 'password' : 'text'
}

function clearError(key) {
  if (!errors.value[key]) return
  const next = { ...errors.value }
  delete next[key]
  errors.value = next
}

function submit() {
  errors.value = {}
  submitError.value = ''
  try {
    const included = fields.value.filter(field => field.required || enabledFields.value[field.key])
    const context = buildPreparedTestContext(included.map(field => ({ ...field, required: true })), values.value)
    context.start_step_id = startStepId.value
    context.end_step_id = endStepId.value
    context.test_assertions = normalizeAssertions()
    emit('run', {
      context,
      fields: fields.value,
      values: Object.fromEntries(included.map(field => [field.key, values.value[field.key]])),
      remember: remember.value,
    })
  } catch (error) {
    if (error.fieldKey) errors.value = { [error.fieldKey]: error.message }
    else submitError.value = error.message || '测试配置无效'
  }
}

watch(() => props.open, async open => {
  if (!open) return
  values.value = buildValues()
  enabledFields.value = Object.fromEntries(allFields.value.filter(field => !field.required).map(field => [field.key, Object.hasOwn(props.savedValues, field.key) && props.savedValues[field.key] !== '']))
  errors.value = {}
  submitError.value = ''
  assertions.value = []
  rangeMode.value = 'all'
  selectedStepId.value = props.rule?.actions?.[0]?.binding_id || ''
  remember.value = true
  await nextTick()
  dialogRef.value?.querySelector('input:not([type="checkbox"]), textarea, input[type="checkbox"]')?.focus()
})
</script>

<template>
  <BaseDialog :open="open" @close="emit('cancel')">
    <section ref="dialogRef" class="test-prep-dialog" role="dialog" aria-modal="true" aria-labelledby="test-prep-title" aria-describedby="test-prep-warning">
      <header class="test-prep-head">
        <div>
          <span class="test-prep-eyebrow">运行预检</span>
          <h3 id="test-prep-title">准备一次真实测试</h3>
          <p>{{ ruleName }}</p>
        </div>
        <button class="icon-btn" title="取消测试" @click="emit('cancel')"><span class="material-symbols-outlined">close</span></button>
      </header>

      <div class="test-data-route" aria-label="测试执行顺序">
        <span><span class="material-symbols-outlined">bolt</span>模拟触发数据</span>
        <i></i>
        <span><span class="material-symbols-outlined">rule</span>运行当前规则</span>
        <i></i>
        <span><span class="material-symbols-outlined">fact_check</span>查看每步结果</span>
      </div>

      <div id="test-prep-warning" class="test-prep-notice">
        <span class="material-symbols-outlined">warning</span>
        <p><b>测试会真实执行动作。</b>请使用不会造成损失的样例数据。</p>
      </div>

      <div class="test-prep-scroll">
        <section class="test-source-group test-range-group">
          <header><span class="material-symbols-outlined">conversion_path</span><div><b>选择执行范围</b><small>只运行需要验证的连续动作；前置条件仍会检查</small></div></header>
          <div class="test-range-controls">
            <label><span>方式</span><select v-model="rangeMode" class="text-field"><option value="all">运行全部动作</option><option value="until" :disabled="!actions.length">运行到指定动作</option><option value="from" :disabled="!actions.length">从指定动作继续</option></select></label>
            <label v-if="rangeMode !== 'all'"><span>{{ rangeMode === 'until' ? '结束于' : '开始于' }}</span><select v-model="selectedStepId" class="text-field"><option v-for="(action, index) in actions" :key="action.binding_id" :value="action.binding_id">动作 {{ index + 1 }}：{{ schema.actions?.[action.type]?.name || action.type }}</option></select></label>
          </div>
          <div v-if="actions.length" class="test-range-track" :aria-label="`本次会执行 ${selectedActionCount} 个动作`">
            <span v-for="(action, index) in actions" :key="action.binding_id" :class="{ active: index >= startIndex && index <= endIndex, skipped: index < startIndex || index > endIndex }"><i>{{ index + 1 }}</i><small>{{ schema.actions?.[action.type]?.name || action.type }}</small></span>
          </div>
          <p v-else class="test-empty-copy">这条规则没有动作，本次只验证触发与前置条件链路。</p>
        </section>

        <section v-for="group in groups" :key="group.name" class="test-source-group">
          <header><span class="material-symbols-outlined">input_circle</span><div><b>{{ group.name }}</b><small>动作运行时会把这些值当作真实触发结果</small></div></header>
          <div class="test-source-fields">
            <label v-for="field in group.fields" :key="field.key" class="test-data-field" :class="{ invalid: errors[field.key] }">
              <span class="test-data-label">
                <span>{{ field.label }}</span>
                <em v-if="field.sensitive"><span class="material-symbols-outlined">lock</span>敏感，不保存</em>
                <small v-else>{{ typeLabel(field.valueType || field.type) }}</small>
              </span>
              <span v-if="!field.required"><input v-model="enabledFields[field.key]" type="checkbox">{{ field.scope === 'variable' ? '覆盖本次初始值' : '提供这个可选值' }}</span>
              <input v-if="inputKind(field) === 'boolean'" v-model="values[field.key]" :disabled="!field.required && !enabledFields[field.key]" type="checkbox" class="test-boolean" @change="clearError(field.key)">
              <textarea v-else-if="inputKind(field) === 'json'" v-model="values[field.key]" :disabled="!field.required && !enabledFields[field.key]" class="text-field test-json-input" spellcheck="false" rows="3" :placeholder="field.placeholder || (field.type === 'array' ? '[]' : '{}')" @input="clearError(field.key)"></textarea>
              <input v-else v-model="values[field.key]" :disabled="!field.required && !enabledFields[field.key]" class="text-field" :type="inputKind(field)" :placeholder="field.placeholder" autocomplete="off" spellcheck="false" @input="clearError(field.key)">
              <span v-if="errors[field.key]" class="test-field-error"><span class="material-symbols-outlined">error</span>{{ errors[field.key] }}</span>
            </label>
          </div>
        </section>

        <section class="test-source-group test-assertion-group">
          <header><span class="material-symbols-outlined">fact_check</span><div><b>结果检查</b><small>只判断本次手动测试，不会改变规则的正式运行</small></div></header>
          <div v-if="assertions.length" class="test-assertion-list">
            <div v-for="(assertion, index) in assertions" :key="index" class="test-assertion-row">
              <select v-model="assertion.sourceKey" class="text-field" @change="assertion.operator = 'equals'"><option v-for="source in assertionSources" :key="source.key" :value="source.key">{{ source.label }}</option></select>
              <select v-model="assertion.operator" class="text-field"><option v-for="operator in assertionOperators(assertion)" :key="operator.value" :value="operator.value">{{ operator.label }}</option></select>
              <input v-if="assertion.operator !== 'exists'" v-model="assertion.expected" class="text-field" placeholder="期望值" autocomplete="off">
              <input v-model="assertion.subpath" class="text-field" placeholder="子字段路径，例如 [0]" aria-label="检查输出的子字段路径" autocomplete="off">
              <button class="icon-btn" title="删除检查项" @click="removeAssertion(index)"><span class="material-symbols-outlined">delete</span></button>
            </div>
          </div>
          <button class="btn btn-outlined test-add-assertion" :disabled="!assertionSources.length || assertions.length >= 50" @click="addAssertion"><span class="material-symbols-outlined">add</span>添加检查项</button>
          <p v-if="!assertionSources.length" class="test-empty-copy">本次范围内的动作没有可检查的非敏感输出。</p>
        </section>
      </div>

      <footer class="test-prep-foot">
        <div><label class="check-row test-remember"><input v-model="remember" type="checkbox"><span>下次继续使用非敏感数据<small v-if="sensitiveCount">{{ sensitiveCount }} 个敏感值每次都要重填</small></span></label><p v-if="submitError" class="test-submit-error">{{ submitError }}</p></div>
        <div class="dialog-actions">
          <button class="btn btn-text" @click="emit('cancel')">取消</button>
          <button class="btn btn-filled" @click="submit"><span class="material-symbols-outlined">play_arrow</span>用这些数据运行</button>
        </div>
      </footer>
    </section>
  </BaseDialog>
</template>
