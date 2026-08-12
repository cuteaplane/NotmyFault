function text(value, limit = 1000) {
  return String(value ?? '').slice(0, limit)
}

function numberOr(value, fallback = null) {
  if (value === null || value === undefined || value === '') return fallback
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : fallback
}

function safeError(error) {
  if (!error) return null
  if (typeof error === 'string') return text(error)
  if (typeof error !== 'object') return text(error)
  return Object.fromEntries(
    ['code', 'location', 'message']
      .filter(key => error[key] !== undefined)
      .map(key => [key, text(error[key])]),
  )
}

function safeSummary(summary) {
  if (!Array.isArray(summary)) return []
  return summary.slice(0, 8).flatMap(item => {
    if (!item || typeof item !== 'object') return []
    return [{
      name: text(item.name, 80),
      label: text(item.label, 80),
      type: text(item.type || 'any', 30),
      display: text(item.display, 120),
      redacted: item.redacted === true,
    }]
  })
}

function safeStep(step) {
  return {
    step_id: text(step?.step_id, 100),
    action_type: text(step?.action_type, 100),
    status: text(step?.status, 30),
    finished_at: numberOr(step?.finished_at),
    duration_ms: numberOr(step?.duration_ms),
    attempt: numberOr(step?.attempt),
    reason: step?.reason ? text(step.reason) : null,
    error: safeError(step?.error),
    input_summary: safeSummary(step?.input_summary),
    output_summary: safeSummary(step?.output_summary),
  }
}

function safeAssertion(assertion) {
  return {
    step_id: text(assertion?.step_id, 100),
    path: Array.isArray(assertion?.path)
      ? assertion.path.slice(0, 20).map(item => text(item, 100))
      : [],
    operator: text(assertion?.operator, 40),
    passed: assertion?.passed === true,
    message: text(assertion?.message),
  }
}

function safeRun(run) {
  return {
    run_id: text(run?.run_id, 120),
    rule_id: text(run?.rule_id, 120),
    rule_name: text(run?.rule_name, 200),
    event_type: text(run?.event_type, 100),
    status: text(run?.status, 30),
    started_at: numberOr(run?.started_at),
    finished_at: numberOr(run?.finished_at),
    duration_ms: numberOr(run?.duration_ms),
    action_count: numberOr(run?.action_count, 0),
    start_step_id: text(run?.start_step_id, 100),
    end_step_id: text(run?.end_step_id, 100),
    assertions_passed: numberOr(run?.assertions_passed, 0),
    assertions_total: numberOr(run?.assertions_total, 0),
    failure_kind: text(run?.failure_kind, 80),
    error: safeError(run?.error),
    steps: Array.isArray(run?.steps) ? run.steps.slice(0, 200).map(safeStep) : [],
    assertion_results: Array.isArray(run?.assertion_results)
      ? run.assertion_results.slice(0, 50).map(safeAssertion)
      : [],
  }
}

export function buildRunExport(runs, filters = {}, exportedAt = new Date()) {
  return {
    format: 'NotmyFault run diagnostics',
    version: 1,
    exported_at: exportedAt.toISOString(),
    filters: {
      status: text(filters.status || 'all', 30),
      time_range: text(filters.timeRange || 'all', 30),
      action_type: text(filters.actionType || 'all', 100),
      search: text(filters.search || '', 200),
    },
    runs: Array.isArray(runs) ? runs.slice(0, 500).map(safeRun) : [],
  }
}
