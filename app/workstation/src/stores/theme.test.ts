import { beforeEach, describe, expect, it, vi } from 'vitest'

describe('themeStore', () => {
  beforeEach(() => {
    vi.resetModules()
    localStorage.clear()
    delete document.documentElement.dataset.theme
  })

  it('默认浅色（对齐 2.0 原型默认皮肤），并把选择写到根元素与本地存储', async () => {
    const { themeStore } = await import('./theme')
    expect(themeStore.theme.value).toBe('light')
    expect(document.documentElement.dataset.theme).toBe('light')

    themeStore.toggle()
    await Promise.resolve()
    expect(themeStore.theme.value).toBe('dark')
    expect(themeStore.isDark.value).toBe(true)
    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(localStorage.getItem('alioth-workstation-theme')).toBe('dark')
  })

  it('读取上次保存的皮肤，非法值回落浅色', async () => {
    localStorage.setItem('alioth-workstation-theme', 'dark')
    let store = (await import('./theme')).themeStore
    expect(store.theme.value).toBe('dark')

    vi.resetModules()
    localStorage.setItem('alioth-workstation-theme', 'neon')
    store = (await import('./theme')).themeStore
    expect(store.theme.value).toBe('light')
  })
})
