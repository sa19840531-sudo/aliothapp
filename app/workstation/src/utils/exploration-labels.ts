/**
 * 探索侧代码值的中文文案唯一来源：页型、页面状态、证据原因码。
 * 任务详情、探索过程面板、报告页共用，禁止页面各自维护同义映射。
 */

export const PAGE_TYPE_NAMES: Record<string, string> = {
  home: '首页',
  content_list: '内容列表页',
  content_detail: '内容详情页',
  video_list: '视频列表页',
  video_detail: '视频详情页',
  profile: '我的 / 个人主页',
  settings: '设置页',
  payment: '支付 / 价格页',
  post_list: '帖子列表页',
  post_publish: '发帖 / 发布页',
  activity: '活动页',
  startup_ad: '启动 / 开屏广告',
  line_loading: '线路加载页',
  guide_page: '引导页',
  login: '登录页',
  register: '注册页',
  search: '搜索页',
  category: '分类页',
  ranking: '榜单页',
  message: '消息页',
  stable_business_page: '稳定业务页',
}

export const PAGE_STATE_NAMES: Record<string, string> = {
  stable: '稳定',
  loaded: '已加载',
  popup: '弹窗',
  modal: '弹层',
  overlay: '浮层',
  dialog: '对话框',
  countdown: '倒计时广告',
  loading: '加载中',
  error: '错误',
  splash: '开屏',
  blank: '空白',
}

export const REASON_CODES: Record<string, string> = {
  unresolved_rule_evidence: '仍有规则证据未齐',
  transition_proof_missing: '缺少到达证明',
  source_identity_unconfirmed: '来源页面身份未确认',
  target_identity_unconfirmed: '目标页面身份未确认',
  target_state_missing: '动作后没有目标页面',
  target_overlay: '落在弹窗上',
  source_not_business_page: '来源不是业务页面',
  external_navigation: '跳到了外部页面',
  same_capture: '前后是同一张采集',
  before_capture_missing: '缺少动作前截图',
  after_capture_missing: '缺少动作后截图',
  manifest_missing: '缺少证据清单',
  manifest_hash_mismatch: '证据清单校验不一致',
  screenshot_missing: '缺少截图',
  ui_snapshot_missing: '缺少页面结构快照',
  'error:COMMAND_TARGET_SEMANTICS_UNTRUSTED': '风控拒绝：动作目标语义不可信',
  'error:POPUP_DISMISSAL_NOT_OBSERVED': '弹窗关闭后仍可见',
  'error:POPUP_DISMISSAL_NOT_FOUND': '找不到弹窗关闭方式',
  'error:ACTION_TIMED_OUT': '动作超时',
  'error:SEMANTIC_TARGET_NOT_FOUND': '找不到动作目标',
}

export function pageTypeName(code: string | null | undefined): string {
  if (!code) return '未识别页型'
  return PAGE_TYPE_NAMES[code] ?? code
}

/** 多个页型代码合成一句中文，空列表返回传入的兜底文案。 */
export function pageTypeNames(codes: readonly string[] | null | undefined, empty = '无'): string {
  if (!codes || !codes.length) return empty
  return codes.map((code) => pageTypeName(code)).join('、')
}

export function pageStateName(code: string | null | undefined): string {
  if (!code) return ''
  return PAGE_STATE_NAMES[code] ?? code
}

export function reasonText(code: string): string {
  return REASON_CODES[code] ?? code
}
