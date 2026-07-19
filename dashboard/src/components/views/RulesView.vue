<script setup>
import { onMounted } from 'vue'
import { store } from '../../lib/store'
import { saveConfig, hasBridge } from '../../lib/api'
import { snackbar } from '../../lib/notify'
import { buildDefaultParams } from '../../lib/utils'
import RuleEditor from '../RuleEditor.vue'

function addRule() {
  const d = Object.keys(store.schema.triggers)[0] || 'unknown'
  store.configData.rules.unshift({
    name: '新规则',
    event: { type: d, params: buildDefaultParams(store.schema.triggers[d]) },
    actions: [],
  })
}

function deleteRule(r) { store.configData.rules.splice(r, 1) }

async function doSave() {
  try {
    const r = await saveConfig(store.configData.rules)
    if (r.ok) { snackbar('配置已保存'); return }
    let msg = '保存失败: ' + (r.error || '未知错误')
    if (r.error && r.error.indexOf('Forbidden') >= 0) msg += ' - 浏览器模式不支持写入，请使用桌面端 Dashboard'
    alert(msg)
  } catch (e) { alert('保存失败: ' + e.message) }
}

onMounted(() => { if (!store.configData.rules) store.configData.rules = [] })
</script>

<template>
  <section class="page active">
    <div class="page-head"><h2>规则配置</h2><div class="actions">
      <button class="btn btn-tonal" @click="addRule"><span class="material-symbols-outlined">add</span>新建规则</button>
      <button class="btn btn-filled" @click="doSave"><span class="material-symbols-outlined">save</span>保存配置</button>
    </div></div>
    <div v-if="!store.configData.rules.length" class="empty-state">
      <div class="material-symbols-outlined">rule_folder</div><h3>还没有规则</h3><p>创建你的第一条自动化规则</p>
      <button class="btn btn-tonal" @click="addRule" style="margin-top:14px"><span class="material-symbols-outlined">add</span>新建规则</button>
    </div>
    <RuleEditor v-for="(rule, r) in store.configData.rules" :key="r" :rule="rule" @delete="deleteRule(r)" />
  </section>
</template>
