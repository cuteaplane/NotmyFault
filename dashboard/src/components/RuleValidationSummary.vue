<script setup>
import { ref } from 'vue'
defineProps({
  validationIssues: Array, validationErrorCount: Number, validationWarningCount: Number,
  checkingRule: Boolean, checkerError: String,
})
const emit = defineEmits(['focus-issue'])
const validationExpanded = ref(false)
</script>

<template>
    <footer class="flow-validation" :class="{ valid: !validationErrorCount, warning: !validationErrorCount && validationWarningCount, expanded: validationExpanded }">
      <span class="material-symbols-outlined">{{ validationErrorCount ? 'error' : validationWarningCount ? 'warning' : 'check_circle' }}</span>
      <div class="flow-validation-body">
        <div class="flow-validation-summary">
          <b v-if="validationErrorCount">{{ validationErrorCount }} 项需要处理</b>
          <b v-else-if="validationWarningCount">可以保存，另有 {{ validationWarningCount }} 项提醒</b>
          <b v-else>{{ checkingRule ? '正在检查当前草稿…' : '规则可以保存' }}</b>
          <button v-if="validationIssues.length && !validationExpanded" type="button" class="flow-validation-preview"
            :disabled="!validationIssues[0].target" @click="emit('focus-issue', validationIssues[0])">
            <span>{{ validationIssues[0].message }}</span>
            <span v-if="validationIssues[0].target" class="material-symbols-outlined">arrow_forward</span>
          </button>
          <button v-if="validationIssues.length" type="button" class="icon-btn flow-validation-toggle"
            :title="validationExpanded ? '收起检查结果' : '展开全部检查结果'" @click="validationExpanded = !validationExpanded">
            <span class="material-symbols-outlined">{{ validationExpanded ? 'expand_less' : 'expand_more' }}</span>
          </button>
        </div>
        <div v-if="validationExpanded && validationIssues.length" class="flow-validation-list">
          <button v-for="(issue, index) in validationIssues" :key="`${issue.message}-${index}`" type="button"
            :class="`flow-validation-item ${issue.severity === 'warning' ? 'warning' : ''}`"
            :disabled="!issue.target" @click="emit('focus-issue', issue)">
            <span class="material-symbols-outlined">{{ issue.severity === 'warning' ? 'warning' : 'error' }}</span>
            <span>{{ issue.message }}</span>
            <span v-if="issue.target" class="material-symbols-outlined">arrow_forward</span>
          </button>
        </div>
        <p v-else-if="!validationIssues.length && checkerError" class="flow-validation-service-error">在线检查暂不可用：{{ checkerError }}。保存时仍会由后台校验。</p>
        <p v-else-if="!validationIssues.length && !checkingRule">修改只会在保存后应用到引擎。</p>
      </div>
    </footer>
</template>
