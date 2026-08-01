import { ref } from 'vue'
import { store } from '../lib/store'
import { getEngineStatus, hasBridge } from '../lib/api'
import { snackbar } from '../lib/notify'

// 模块级共享状态：NavRail 快捷按钮与 HomeView 英雄卡片共用同一套 busy 标记
const starting = ref(false)
const stopping = ref(false)
const restarting = ref(false)
const shuttingDown = ref(false)

export function useEngineControl() {
  // 合并式状态同步（与 App.vue.updateStatus 对齐）：
  // 部分字段更新不能冲掉 security_mode / pid / rules_count 等其余字段
  function syncStatus(s) {
    store.engineStatus = { ...store.engineStatus, ...s }
    if ('api_alive' in s) store.controllerOnline = s.api_alive === true
    if ('engine_running' in s) store.engineOnline = s.engine_running === true
    document.body.classList.toggle('controller-online', store.controllerOnline)
    document.body.classList.toggle('engine-online', store.engineOnline)
  }

  async function startEngine() {
    if (starting.value) return
    starting.value = true
    try {
      if (!hasBridge()) throw new Error('Dashboard 桌面桥接尚未就绪')
      const r = await window.pywebview.api.launch_engine()
      if (!r.ok) throw new Error(r.error || '未知错误')
      syncStatus(r)
      if (window.__nmf) await window.__nmf.refreshAll()
      store.refreshSignal++ // 通知各视图刷新统计
      snackbar(r.engine_running ? '自动化已启动' : '后台服务正在启动自动化')
    } catch (e) {
      alert('启动失败: ' + e.message)
    } finally {
      starting.value = false
    }
  }

  async function stopEngine() {
    if (stopping.value) return
    stopping.value = true
    try {
      if (!hasBridge()) throw new Error('Dashboard 桌面桥接尚未就绪')
      const r = await window.pywebview.api.stop_engine()
      if (!r?.ok) throw new Error(r?.error || '后台服务未响应')
      const status = await getEngineStatus()
      syncStatus(status)
      snackbar(r.stopping ? '正在暂停自动化' : '自动化已暂停，后台服务仍在线')
    } catch (e) {
      alert('暂停失败: ' + e.message)
    } finally {
      stopping.value = false
    }
  }

  // 彻底退出引擎进程（后台服务 + 托盘一起退出）。进程退出后 API 会断开，
  // Dashboard 是独立进程不受影响。
  async function shutdownEngine() {
    if (shuttingDown.value) return
    if (!confirm('彻底退出引擎？将停止全部自动化，并关闭后台服务与托盘。')) return
    shuttingDown.value = true
    try {
      if (!hasBridge()) throw new Error('Dashboard 桌面桥接尚未就绪')
      await window.pywebview.api.shutdown_engine()
      snackbar('引擎进程正在退出…')
    } catch (e) {
      // 进程可能在响应返回前就断开，按已退出处理
      snackbar('引擎进程已退出')
    } finally {
      syncStatus({ api_alive: false, engine_running: false, engine_state: 'offline' })
      if (window.__nmf) await window.__nmf.refreshAll()
      shuttingDown.value = false
    }
  }

  // 重启 = 暂停自动化后立即拉起。launch_engine 幂等：
  // 服务在线走 /api/engine/start，离线直接拉起引擎进程。
  async function restartEngine() {
    if (restarting.value) return
    restarting.value = true
    try {
      if (!hasBridge()) throw new Error('Dashboard 桌面桥接尚未就绪')
      const status = await getEngineStatus()
      if (status.api_alive && status.engine_running) {
        const r = await window.pywebview.api.stop_engine()
        if (!r?.ok) throw new Error(r?.error || '停止失败')
        // 引擎停机是异步的；RuntimeController 在旧线程退出前拒绝新启动，
        // 这里等待 state 真正回到 stopped 再拉起，避免 409 竞态。
        const deadline = Date.now() + 15000
        let current = { ...status }
        while (Date.now() < deadline) {
          current = await getEngineStatus()
          if (!current.engine_running || !current.api_alive) break
          await new Promise(resolve => setTimeout(resolve, 500))
        }
        if (current.engine_running) throw new Error('引擎停止超时，请稍后重试')
      }
      const r2 = await window.pywebview.api.launch_engine()
      if (!r2.ok) throw new Error(r2.error || '启动失败')
      syncStatus(r2)
      if (window.__nmf) await window.__nmf.refreshAll()
      store.refreshSignal++
      snackbar('引擎已重启')
    } catch (e) {
      alert('重启失败: ' + e.message)
    } finally {
      restarting.value = false
    }
  }

  return { starting, stopping, restarting, shuttingDown, syncStatus, startEngine, stopEngine, restartEngine, shutdownEngine }
}
