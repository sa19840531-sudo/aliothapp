/** 规则分类的中文文案唯一来源（规则中心、报告问题分组共用）。 */

export const CATEGORY_LABELS: Record<string, string> = {
  text: '文案质检',
  visual: '视觉质检',
  interaction: '交互质检',
  ad: '广告质检',
  payment: '支付与价格质检',
  input_risk: '输入风险质检',
  player: '播放器质检',
  compliance: '合规质检',
  performance: '性能质检',
  navigation: '导航质检',
}

export function categoryLabel(code: string | null | undefined): string {
  if (!code) return '未分类'
  return CATEGORY_LABELS[code] ?? code
}
