import { approveRuleDraft, saveConfig } from './api'
import { alertDialog, passwordDialog } from './dialog'

const PASSWORD_CODES = new Set(['admin_key_required', 'admin_key_invalid'])
const KEY_PROBLEM_CODES = new Set(['admin_key_missing', 'admin_key_unencrypted'])

function keyProblemMessage(result) {
  if (result?.code === 'admin_key_missing') {
    return `${result?.error || '严格模式缺少签名私钥'}。在项目目录运行 python build.py build 生成密码加密的私钥，或在设置页关闭“创建管理员规则时要求验证密钥”。`
  }
  if (result?.code === 'admin_key_unencrypted') {
    return `${result?.error || '签名私钥未加密'}。运行 python build.py build 重新生成加密私钥，或在设置页关闭“创建管理员规则时要求验证密钥”。`
  }
  return ''
}

export async function saveRulesWithApproval(rules, password = '') {
  while (true) {
    const result = await saveConfig(rules, password)
    password = ''
    if (!PASSWORD_CODES.has(result?.code)) {
      if (KEY_PROBLEM_CODES.has(result?.code)) {
        await alertDialog('无法保存这条规则', keyProblemMessage(result))
      }
      return result
    }

    const plugins = Array.isArray(result.plugins) && result.plugins.length
      ? `涉及管理员插件：${result.plugins.join('、')}`
      : '该规则包含可能调用管理员权限的步骤'
    const entered = await passwordDialog(
      '验证签名私钥',
      plugins,
      result.code === 'admin_key_invalid' ? result.error : '',
    )
    if (typeof entered !== 'string') return { ok: false, cancelled: true }
    password = entered
  }
}

export async function approveRuleBeforeEditing(rule) {
  let password = ''
  let verified = ''
  while (true) {
    const result = await approveRuleDraft(rule, password)
    password = ''
    if (!PASSWORD_CODES.has(result?.code)) {
      if (KEY_PROBLEM_CODES.has(result?.code)) {
        await alertDialog('无法打开规则编辑器', keyProblemMessage(result))
      }
      return result?.ok ? { ...result, adminKeyPassword: verified } : result
    }
    const plugins = Array.isArray(result.plugins) && result.plugins.length
      ? `涉及管理员插件或高风险动作：${result.plugins.join('、')}`
      : '该规则包含可能调用管理员权限的步骤'
    const entered = await passwordDialog(
      '验证签名私钥',
      `${plugins}。确认后才会打开规则编辑器。`,
      result.code === 'admin_key_invalid' ? result.error : '',
    )
    if (typeof entered !== 'string') return { ok: false, cancelled: true }
    password = entered
    verified = entered
  }
}
