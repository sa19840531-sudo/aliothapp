import { describe, expect, it } from 'vitest'
import {
  buildQuickPayload,
  defaultQuickTaskName,
  matchProductByPackage,
  matchProductByUrl,
} from './quickStart'

const products = [
  { project_code: 'SF-129', product_name: '十八汇', h5_url: 'https://35.229.238.103:2018/' },
  { project_code: 'JHA-128', product_name: 'oio禁漫', h5_url: 'https://d3jsxm6ahe66k1.cloudfront.net' },
]

const rulesetCodes = ['dept_ruleset', 'ruisheng_ruleset']

describe('quickStart', () => {
  it('按地址主域自动匹配产品，匹配不到或地址不完整时不猜', () => {
    expect(matchProductByUrl('https://35.229.238.103:2018/movie', products)?.project_code).toBe('SF-129')
    expect(matchProductByUrl('https://unknown.example/', products)).toBeNull()
    expect(matchProductByUrl('35.229.238.103', products)).toBeNull()
  })

  it('按包名业务段匹配产品，跳过 com/app 这类通用段', () => {
    expect(matchProductByPackage('com.oio.manga', products)?.project_code).toBe('JHA-128')
    expect(matchProductByPackage('com.app.android', products)).toBeNull()
    expect(matchProductByPackage(null, products)).toBeNull()
  })

  it('H5 地址走 V2 完整质检合同：提交选定的规则集，页面范围全部时留空由系统识别', () => {
    const payload = buildQuickPayload({
      mode: 'url',
      taskName: ' 十八汇 H5 质检 ',
      product: products[0]!,
      departmentName: '北京分公司',
      rulesetCodes,
      targetUrl: 'https://35.229.238.103:2018/ ',
      urlPlatform: 'h5',
    })
    expect(payload.task_name).toBe('十八汇 H5 质检')
    expect(payload.target).toEqual({
      platform: 'h5',
      url: 'https://35.229.238.103:2018/',
      exploration_target: { required_page_types: [] },
    })
    expect(payload.ruleset_codes).toEqual(['dept_ruleset', 'ruisheng_ruleset'])
    expect(payload).not.toHaveProperty('ruleset_code')
    expect(payload.channel).toBe('workbench')
    expect(payload.product).toEqual({
      department_name: '北京分公司',
      product_code: 'SF-129',
      product_name: '十八汇',
    })
    expect(payload.scan_mode).toBe('full')
    expect(payload.remark).toBe('快速质检入口创建')
    expect(payload).not.toHaveProperty('journey_steps')
  })

  it('Web 地址只选部分页面时，把这几类页面放进探索目标', () => {
    const payload = buildQuickPayload({
      mode: 'url',
      taskName: 'Web 质检',
      product: products[0]!,
      departmentName: '',
      rulesetCodes,
      requiredPageTypes: ['video_list', 'content_detail'],
      targetUrl: 'https://web.example.test/',
      urlPlatform: 'web',
    })
    expect(payload.target).toEqual({
      platform: 'web',
      url: 'https://web.example.test/',
      exploration_target: { required_page_types: ['video_list', 'content_detail'] },
    })
  })

  it('安装包走 App 合同：默认云手机、空闲设备优先，页面范围全部时不再写死页面类型', () => {
    const payload = buildQuickPayload({
      mode: 'app',
      taskName: 'oio禁漫 App 质检',
      product: products[1]!,
      departmentName: '',
      rulesetCodes,
      build: {
        platform_build_ref: 'app-build:11111111-1111-1111-1111-111111111111',
        status: 'verified',
        original_filename: 'oio.apk',
        sha256: 'x',
        size_bytes: 1,
        package_name: 'com.oio.manga',
        version_name: '1.2.0',
        version_code: 120,
      },
      deviceProfileId: 'qa-d',
    })
    // stable_business_page 不在页面目录里，后端会拒收；全部页面时只发空数组，也不再发里程碑和最低页数
    expect(payload.target).toEqual({
      platform: 'app',
      source: { type: 'build_ref', platform_build_ref: 'app-build:11111111-1111-1111-1111-111111111111' },
      package_name: 'com.oio.manga',
      device: { backend: 'cloud_phone', profile_id: 'qa-d' },
      exploration_target: { required_page_types: [] },
    })
    expect(payload.ruleset_codes).toEqual(['dept_ruleset', 'ruisheng_ruleset'])
    expect(payload.product.department_name).toBe('-')
  })

  it('安装包只选部分页面时，把这几类页面放进 App 探索目标', () => {
    const payload = buildQuickPayload({
      mode: 'app',
      taskName: 'oio禁漫 App 质检',
      product: products[1]!,
      departmentName: '',
      rulesetCodes,
      requiredPageTypes: ['home', 'content_detail'],
      build: { platform_build_ref: 'app-build:1', package_name: null } as never,
    })
    expect(payload.target).toMatchObject({
      platform: 'app',
      exploration_target: { required_page_types: ['home', 'content_detail'] },
    })
  })

  it('缺安装包或地址不完整时拒绝组装', () => {
    expect(() =>
      buildQuickPayload({
        mode: 'app',
        taskName: 'x',
        product: products[0]!,
        departmentName: 'a',
        rulesetCodes,
      }),
    ).toThrow('安装包')
    expect(() =>
      buildQuickPayload({
        mode: 'url',
        taskName: 'x',
        product: products[0]!,
        departmentName: 'a',
        rulesetCodes,
        targetUrl: 'not a url',
      }),
    ).toThrow('地址')
  })

  it('任务名按产品、版本与平台自动生成', () => {
    expect(
      defaultQuickTaskName({
        mode: 'app',
        productName: 'oio禁漫',
        build: { version_name: '1.2.0' } as never,
      }),
    ).toBe('oio禁漫 1.2.0 App 质检')
    expect(defaultQuickTaskName({ mode: 'url', targetUrl: 'https://a.example/x', urlPlatform: 'web' })).toBe(
      'a.example Web 质检',
    )
    expect(defaultQuickTaskName({ mode: 'batch' })).toBe('')
  })
})
