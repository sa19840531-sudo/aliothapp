import { RULE_EXECUTION_STATUS_LABELS } from '@/utils/status'

/** 规则测试的结论文案与配色、端名、时间格式：结果卡片、测试记录共用。 */
export type RuleTestTone = 'success' | 'danger' | 'warning' | 'orange' | 'muted'

const OUTCOME_TONES: Record<string, RuleTestTone> = {
  pass: 'success',
  fail: 'danger',
  warning: 'warning',
  manual_review: 'warning',
  evidence_insufficient: 'orange',
  skipped: 'muted',
  error: 'danger',
  not_executable: 'danger',
}

// 测试场景特有的结论名；其余沿用平台统一的规则结果文案
const OUTCOME_LABELS: Record<string, string> = {
  skipped: '不适用（已跳过）',
  not_executable: '规则跑不起来',
}

export function ruleTestOutcomeLabel(outcome: string): string {
  return OUTCOME_LABELS[outcome] ?? RULE_EXECUTION_STATUS_LABELS[outcome] ?? outcome
}

export function ruleTestOutcomeTone(outcome: string): RuleTestTone {
  return OUTCOME_TONES[outcome] ?? 'muted'
}

/** 测试记录筛选用：规则测试可能出现的全部结论 */
export const RULE_TEST_OUTCOMES = Object.keys(OUTCOME_TONES)

const PLATFORM_LABELS: Record<string, string> = { h5: 'H5', web: 'Web', android: 'App', ios: 'iOS' }

export function ruleTestPlatformLabel(platform: string): string {
  return PLATFORM_LABELS[platform] ?? platform
}

export function formatRuleTestTime(value: string): string {
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}
