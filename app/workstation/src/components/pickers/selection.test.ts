import { describe, expect, it } from 'vitest'
import type { CatalogRule, CatalogRuleset } from '@/services/ruleCatalog.api'
import {
  PICKER_ALL,
  allSelection,
  pageScopeOptions,
  resolvePageScope,
  resolveRulesets,
  rulesOfRulesets,
  rulesetOptionLabel,
  rulesetSelectionError,
  selectedValues,
  toggleAll,
  toggleOption,
} from './selection'

function ruleset(overrides: Partial<CatalogRuleset> = {}): CatalogRuleset {
  return {
    ruleset_id: 'rs-1',
    ruleset_code: 'alioth_first_ruleset',
    version: 1,
    name: '玉衡平台首期质检规则集',
    description: '',
    status: 'enabled',
    enabled_rule_count: 2,
    rule_codes: [],
    ...overrides,
  }
}

function rule(overrides: Partial<CatalogRule> = {}): CatalogRule {
  return {
    rule_id: 'r-1',
    rule_code: 'alioth_5',
    name: '页面模块空白兜底检查',
    category: 'visual',
    severity: 'high',
    executor_type: 'hybrid',
    check_point: '',
    applicable_page_types: ['home'],
    ruleset_code: 'alioth_first_ruleset',
    ruleset_version: 1,
    ...overrides,
  }
}

const codes = ['home', 'content_list', 'content_detail']

describe('带「全部」的多选口径', () => {
  it('默认是全部，展开成全部选项；已不存在的旧值忽略', () => {
    expect(allSelection()).toEqual([PICKER_ALL])
    expect(selectedValues(allSelection(), codes)).toEqual(codes)
    expect(selectedValues(['content_detail', 'gone', 'home'], codes)).toEqual(['home', 'content_detail'])
  })

  it('全部状态下取消一项就退出全部，逐项勾满又回到全部', () => {
    const partial = toggleOption(allSelection(), 'content_list', false, codes)
    expect(partial).toEqual(['home', 'content_detail'])
    expect(toggleOption(partial, 'content_list', true, codes)).toEqual([PICKER_ALL])
  })

  it('勾选全部即全选，取消全部即全不选', () => {
    expect(toggleAll(true)).toEqual([PICKER_ALL])
    expect(toggleAll(false)).toEqual([])
  })
})

describe('规则集选择', () => {
  const first = ruleset()
  const second = ruleset({ ruleset_id: 'rs-2', ruleset_code: 'basic_compliance', name: '基础合规规则集' })

  it('全部 = 目录里全部启用规则集；只有一个时可以提交', () => {
    expect(resolveRulesets(allSelection(), [first])).toEqual([first])
    expect(rulesetSelectionError(resolveRulesets(allSelection(), [first]), [first])).toBeNull()
  })

  it('多个规则集可以一起执行：选「全部」或任意几个都能提交', () => {
    const available = [first, second]
    expect(resolveRulesets(allSelection(), available)).toEqual([first, second])
    expect(rulesetSelectionError(resolveRulesets(allSelection(), available), available)).toBeNull()
    expect(resolveRulesets(['rs-2'], available)).toEqual([second])
    expect(rulesetSelectionError([second], available)).toBeNull()
  })

  it('一个都没选时提示请选择规则集；目录里没有启用的规则集时直接说明', () => {
    expect(rulesetSelectionError(resolveRulesets([], [first]), [first])).toBe('请选择规则集')
    expect(rulesetSelectionError(resolveRulesets(allSelection(), []), [])).toBe(
      '暂无启用的规则集，请联系运营中心',
    )
  })

  it('选项显示为「名称 · N 条规则」', () => {
    expect(rulesetOptionLabel(ruleset({ enabled_rule_count: 70 }))).toBe('玉衡平台首期质检规则集 · 70 条规则')
  })

  it('规则清单取所选规则集的规则：按编码去重，保持目录顺序，其他规则集和旧版本不算', () => {
    const rules = [
      rule({ rule_code: 'alioth_8' }),
      rule({ rule_code: 'basic_01', ruleset_code: 'basic_compliance' }),
      rule({ rule_code: 'alioth_5' }),
      rule({ rule_code: 'alioth_8', rule_id: 'dup' }),
      rule({ rule_code: 'alioth_9', ruleset_version: 2 }),
    ]
    expect(rulesOfRulesets([first], rules).map((item) => item.rule_code)).toEqual(['alioth_8', 'alioth_5'])
    expect(rulesOfRulesets([first, second], rules).map((item) => item.rule_code)).toEqual([
      'alioth_8',
      'basic_01',
      'alioth_5',
    ])
  })
})

describe('页面范围选择', () => {
  it('选全部或一个都不选 → 空数组，由系统自动识别', () => {
    expect(resolvePageScope(allSelection(), codes)).toEqual([])
    expect(resolvePageScope([], codes)).toEqual([])
  })

  it('只选部分 → 这几类页面，按选项顺序', () => {
    expect(resolvePageScope(['content_detail', 'home'], codes)).toEqual(['home', 'content_detail'])
  })

  it('逐项全选也等同全部，绝不显式提交全量列表', () => {
    expect(resolvePageScope([...codes], codes)).toEqual([])
  })

  it('选项用页面名称，目录里查不到名称时退回编码', () => {
    const options = pageScopeOptions(['home', 'mystery'], [
      {
        code: 'home',
        name: '首页',
        category: 'business',
        rule_matchable: true,
        coverage_required: true,
        description: '',
        related_rule_codes: [],
      },
    ])
    expect(options).toEqual([
      { value: 'home', label: '首页' },
      { value: 'mystery', label: 'mystery' },
    ])
  })
})
