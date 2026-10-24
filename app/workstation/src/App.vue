<script setup lang="ts">
import { computed } from 'vue'
import {
  darkTheme,
  dateZhCN,
  lightTheme,
  NConfigProvider,
  NDialogProvider,
  NMessageProvider,
  NNotificationProvider,
  zhCN,
} from 'naive-ui'
import type { GlobalThemeOverrides } from 'naive-ui'
import { themeStore } from '@/stores/theme'

// naive-ui 弹层（下拉菜单/气泡确认/消息条）挂在 body 下，拿不到页面局部样式，
// 必须在 ConfigProvider 层对齐皮肤配色与主色（取自 styles/tokens.css 的令牌）。
const sharedFontSizes = {
  fontSize: '14px',
  fontSizeMini: '12px',
  fontSizeTiny: '12px',
  fontSizeSmall: '13px',
  fontSizeMedium: '14px',
  fontSizeLarge: '15px',
  fontSizeHuge: '16px',
  borderRadius: '8px',
}

// 默认皮肤：原型 tailwind slate/blue 浅色。
const lightThemeOverrides: GlobalThemeOverrides = {
  common: {
    ...sharedFontSizes,
    primaryColor: '#2563eb',
    primaryColorHover: '#1d4ed8',
    primaryColorPressed: '#1e40af',
    primaryColorSuppl: '#2563eb',
    popoverColor: '#ffffff',
    modalColor: '#ffffff',
    cardColor: '#ffffff',
    bodyColor: '#f8fafc',
    borderColor: '#e2e8f0',
    textColorBase: '#1e293b',
    textColor1: '#0f172a',
    textColor2: '#1e293b',
    textColor3: '#64748b',
  },
}

// 暗黑皮肤：原型 data-demo-skin="dark" 的近黑中性色与 #5e6ad2 主色。
const darkThemeOverrides: GlobalThemeOverrides = {
  common: {
    ...sharedFontSizes,
    primaryColor: '#5e6ad2',
    primaryColorHover: '#6b77e8',
    primaryColorPressed: '#4f5ac2',
    primaryColorSuppl: '#5e6ad2',
    popoverColor: '#0f1011',
    modalColor: '#0f1011',
    cardColor: '#0f1011',
    bodyColor: '#010102',
    borderColor: '#23252a',
    textColorBase: '#d0d6e0',
    textColor1: '#f7f8f8',
    textColor2: '#d0d6e0',
    textColor3: '#8a8f98',
  },
}

const naiveTheme = computed(() => (themeStore.isDark.value ? darkTheme : lightTheme))
const naiveOverrides = computed(() =>
  themeStore.isDark.value ? darkThemeOverrides : lightThemeOverrides,
)
</script>

<template>
  <!-- 中文语言包：下拉、表格空的时候显示「无数据」而不是英文 No Data -->
  <n-config-provider :theme="naiveTheme" :theme-overrides="naiveOverrides" :locale="zhCN" :date-locale="dateZhCN">
    <n-notification-provider>
      <n-message-provider>
        <n-dialog-provider>
          <router-view />
        </n-dialog-provider>
      </n-message-provider>
    </n-notification-provider>
  </n-config-provider>
</template>
