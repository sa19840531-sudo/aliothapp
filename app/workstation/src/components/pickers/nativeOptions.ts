import { Fragment, isVNode, type VNode, type VNodeArrayChildren } from 'vue'

/** 原生 <option> 能绑定的值：字符串或数字（页码条数之类）。 */
export type NativeOptionValue = string | number

// 用 type 而不是 interface：naive-ui 的选项类型带索引签名，interface 赋不过去
export type NativeOption = {
  label: string
  value: NativeOptionValue
  disabled: boolean
}

function textOf(children: unknown): string {
  if (typeof children === 'string' || typeof children === 'number') return String(children)
  if (Array.isArray(children)) {
    return children.map((child) => (isVNode(child) ? textOf(child.children) : textOf(child))).join('')
  }
  return ''
}

/**
 * 从插槽里的 <option> 读出选项，语义同原生 select：
 * 没写 value 时取文字，disabled 写了（且不是 false）就禁用；v-for / v-if 生成的片段逐层展开。
 */
export function optionsFromSlot(nodes: VNodeArrayChildren | undefined): NativeOption[] {
  const options: NativeOption[] = []
  const walk = (list: VNodeArrayChildren | undefined) => {
    for (const node of list ?? []) {
      if (Array.isArray(node)) {
        walk(node)
        continue
      }
      if (!isVNode(node)) continue
      if (node.type === Fragment) {
        walk(node.children as VNodeArrayChildren)
        continue
      }
      if (node.type !== 'option') continue
      const props = (node as VNode).props ?? {}
      const label = textOf(node.children).replace(/\s+/g, ' ').trim()
      const value = props.value ?? label
      options.push({
        label,
        value: typeof value === 'number' ? value : String(value),
        disabled: props.disabled != null && props.disabled !== false,
      })
    }
  }
  walk(nodes)
  return options
}

/** 按原生 select 的宽松相等找选中项（'10' 与 10 视为同一项），找不到返回 null。 */
export function matchOption(
  options: NativeOption[],
  value: NativeOptionValue | null | undefined,
): NativeOption | null {
  if (value === null || value === undefined) return null
  return (
    options.find((option) => option.value === value) ??
    options.find((option) => String(option.value) === String(value)) ??
    null
  )
}
