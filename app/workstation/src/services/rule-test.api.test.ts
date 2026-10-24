import { afterEach, describe, expect, it, vi } from 'vitest'
import { configureHttpClient } from './http'
import {
  getRuleEvaluationRun,
  listRuleEvaluationRuns,
  submitRuleEvaluationRun,
} from './rule-test.api'

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

afterEach(() => {
  configureHttpClient({})
  vi.unstubAllGlobals()
})

describe('独立规则测试接口', () => {
  it('不指定规则时提交空 body，POST 到 V2 入口', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(
        { task_id: 'task-1', rule_evaluation_run_id: 'rule-eval:abc', status: 'pending' },
        202,
      ),
    )
    vi.stubGlobal('fetch', fetchMock)

    await submitRuleEvaluationRun('task-1')

    expect(fetchMock).toHaveBeenCalledOnce()
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v2/tasks/task-1/rule-evaluation-runs')
    expect(init.method).toBe('POST')
    expect(init.body).toBeUndefined()
  })

  it('指定规则子集时携带 rule_codes', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(
        { task_id: 'task-1', rule_evaluation_run_id: 'rule-eval:abc', status: 'pending' },
        202,
      ),
    )
    vi.stubGlobal('fetch', fetchMock)

    await submitRuleEvaluationRun('task-1', ['alioth_1', 'alioth_2'])

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(JSON.parse(String(init.body))).toEqual({ rule_codes: ['alioth_1', 'alioth_2'] })
  })

  it('列表与详情按任务与运行编号取数', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ task_id: 'task-1', runs: [] }))
      .mockResolvedValueOnce(
        jsonResponse({
          task_id: 'task-1',
          run_id: 'rule-eval:abc',
          status: 'succeeded',
          page_count: 1,
          rule_count: 1,
          requested_rule_codes: [],
          error_message: null,
          created_at: null,
          started_at: null,
          finished_at: null,
          results: [],
        }),
      )
    vi.stubGlobal('fetch', fetchMock)

    await listRuleEvaluationRuns('task-1')
    await getRuleEvaluationRun('task-1', 'rule-eval:abc')

    const [listUrl] = fetchMock.mock.calls[0] as [string]
    const [detailUrl] = fetchMock.mock.calls[1] as [string]
    expect(listUrl).toBe('/api/v2/tasks/task-1/rule-evaluation-runs')
    expect(detailUrl).toBe('/api/v2/tasks/task-1/rule-evaluation-runs/rule-eval%3Aabc')
  })
})
