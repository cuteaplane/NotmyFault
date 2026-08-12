<script setup>
import { computed, ref } from 'vue'
import { draftRuleFromText } from '../lib/api'

const emit = defineEmits(['create'])
const description = ref('')
const drafting = ref(false)
const result = ref(null)
const error = ref('')

const triggerName = computed(() => result.value?.interpretation?.trigger?.name || '还没听清什么时候开始')
const actionNames = computed(() => (
  result.value?.interpretation?.actions?.map(action => action.name).filter(Boolean) || []
))
const validationIssues = computed(() => (
  result.value?.validation?.issues?.slice(0, 5) || []
))

async function makeDraft() {
  if (!description.value.trim() || drafting.value) return
  drafting.value = true
  error.value = ''
  result.value = null
  try {
    const response = await draftRuleFromText(description.value.trim())
    if (!response?.ok) {
      error.value = response?.error || '没有生成草稿，请换一种说法。'
      return
    }
    result.value = response
  } catch (reason) {
    error.value = reason.message || '本地草稿服务暂时不可用。'
  } finally {
    drafting.value = false
  }
}

function openDraft() {
  if (!result.value?.draft) return
  emit('create', result.value.draft)
}
</script>

<template>
  <section class="natural-draft-panel">
    <div class="natural-draft-intro">
      <span class="material-symbols-outlined">chat_bubble</span>
      <div>
        <b>直接说你想让电脑做什么</b>
        <small>把“什么时候开始”和“接着做什么”写在一起。</small>
      </div>
    </div>

    <form class="natural-draft-form" @submit.prevent="makeDraft">
      <label for="natural-draft-description">你想自动化什么？</label>
      <div class="natural-draft-entry">
        <textarea id="natural-draft-description" v-model="description" class="text-field textarea-field"
          maxlength="2000" rows="2" placeholder="例如：每天 08:30 提醒我提交月报"></textarea>
        <button class="btn btn-filled" type="submit" :disabled="!description.trim() || drafting">
          <span v-if="drafting" class="spinner"></span>
          <span v-else class="material-symbols-outlined">draft</span>
          {{ drafting ? '正在理解…' : '先看看草稿' }}
        </button>
      </div>
      <small class="natural-draft-privacy"><span class="material-symbols-outlined">lock</span>这段话只在本机匹配，不会发到网络，也不会自动保存或运行。</small>
    </form>

    <p v-if="error" class="natural-draft-error" role="alert"><span class="material-symbols-outlined">error</span>{{ error }}</p>

    <div v-if="result" class="natural-draft-result" aria-live="polite">
      <div class="natural-draft-result-head">
        <div><small>我理解的是</small><b>{{ result.draft ? '先按这个结构起草' : '还缺少关键信息' }}</b></div>
        <span class="chip">本机生成</span>
      </div>
      <div class="natural-draft-sentence">
        <span><i>当</i><b>{{ triggerName }}</b></span>
        <span class="material-symbols-outlined">arrow_forward</span>
        <span><i>然后</i><b>{{ actionNames.length ? actionNames.join('、') : '还没听清要做什么' }}</b></span>
      </div>
      <ul v-if="result.assumptions?.length" class="natural-draft-notes">
        <li v-for="item in result.assumptions" :key="item"><span class="material-symbols-outlined">info</span>{{ item }}</li>
      </ul>
      <div v-if="result.missing?.length" class="natural-draft-missing">
        <b>进编辑器后还要补：</b>
        <span v-for="item in result.missing" :key="item">{{ item }}</span>
      </div>
      <div v-if="result.unavailable?.length" class="natural-draft-missing">
        <b>当前不能使用：</b>
        <span v-for="item in result.unavailable" :key="item">{{ item }}</span>
      </div>
      <div v-if="validationIssues.length" class="natural-draft-check">
        <b>规则检查还发现：</b>
        <ul>
          <li v-for="issue in validationIssues" :key="`${issue.code}-${issue.message}`">
            <span class="material-symbols-outlined">{{ issue.severity === 'warning' ? 'warning' : 'error' }}</span>{{ issue.message }}
          </li>
        </ul>
      </div>
      <footer>
        <small>这里不会替你做决定。所有参数仍会经过普通规则检查。</small>
        <button class="btn btn-tonal" :disabled="!result.draft" @click="openDraft">
          进编辑器检查<span class="material-symbols-outlined">arrow_forward</span>
        </button>
      </footer>
    </div>
  </section>
</template>
