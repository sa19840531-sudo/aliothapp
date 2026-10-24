import { describe, expect, it } from 'vitest'
import {
  humanVerdictLabel,
  reportStateLabel,
  reviewStatusLabel,
  ruleExecutionStatusLabel,
  taskStatusLabel,
} from './status'

describe('工作台状态文案', () => {
  it('保持任务、规则和复核状态的统一业务口径', () => {
    expect(taskStatusLabel('review')).toBe('待复核')
    expect(ruleExecutionStatusLabel('evidence_insufficient')).toBe('证据不足')
    expect(reviewStatusLabel('needs_recheck')).toBe('需复核')
    expect(humanVerdictLabel('ignored')).toBe('本产品不适用')
    expect(taskStatusLabel('queued')).toBe('排队中')
  })

  it('报告状态只分成功、生成中、生成失败三种', () => {
    expect(reportStateLabel('ready')).toBe('成功')
    expect(reportStateLabel('generating')).toBe('生成中')
    expect(reportStateLabel('not_started', 'running')).toBe('生成中')
    expect(reportStateLabel('failed')).toBe('生成失败')
    expect(reportStateLabel('not_started', 'done')).toBe('暂无报告')
  })

  it('取消终态停止生成展示，并保留实际报告与失败结果', () => {
    expect(reportStateLabel('generating', 'canceled')).toBe('暂无报告')
    expect(reportStateLabel('not_started', 'canceled')).toBe('暂无报告')
    expect(reportStateLabel('not_applicable', 'canceled')).toBe('暂无报告')
    expect(reportStateLabel('ready', 'canceled')).toBe('成功')
    expect(reportStateLabel('generated', 'canceled')).toBe('成功')
    expect(reportStateLabel('failed', 'canceled')).toBe('生成失败')
    expect(reportStateLabel('generating', 'canceling')).toBe('生成中')
  })

  it('未知状态保留原值，空人工结论返回 null', () => {
    expect(taskStatusLabel('custom_status')).toBe('custom_status')
    expect(humanVerdictLabel(null)).toBeNull()
  })
})
