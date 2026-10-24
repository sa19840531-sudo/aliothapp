import { computed, ref, watch } from 'vue'

/**
 * 工作台皮肤：浅色 / 深色两套（对齐 2.0 原型「换肤」）。
 *
 * 选择写在根元素 `data-theme` 上，页面样式只认 tokens.css 里的变量；
 * 旧页面尚未全部改用变量，因此默认仍是深色，逐页适配后再切默认。
 */
export type WorkstationTheme = 'light' | 'dark'

const STORAGE_KEY = 'alioth-workstation-theme'
const DEFAULT_THEME: WorkstationTheme = 'light'

function isTheme(value: unknown): value is WorkstationTheme {
  return value === 'light' || value === 'dark'
}

function loadTheme(): WorkstationTheme {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    return isTheme(stored) ? stored : DEFAULT_THEME
  } catch {
    return DEFAULT_THEME
  }
}

function applyTheme(value: WorkstationTheme) {
  if (typeof document === 'undefined') return
  document.documentElement.dataset.theme = value
  document.documentElement.style.colorScheme = value
}

const theme = ref<WorkstationTheme>(loadTheme())
applyTheme(theme.value)

watch(theme, (value) => {
  applyTheme(value)
  try {
    localStorage.setItem(STORAGE_KEY, value)
  } catch {
    // 无痕模式等场景下存不了也不影响当前会话
  }
})

export const themeStore = {
  theme,
  isDark: computed(() => theme.value === 'dark'),
  setTheme(value: WorkstationTheme) {
    theme.value = value
  },
  toggle() {
    theme.value = theme.value === 'dark' ? 'light' : 'dark'
  },
}

export const THEME_LABELS: Record<WorkstationTheme, string> = {
  light: '默认皮肤',
  dark: '暗黑皮肤',
}
