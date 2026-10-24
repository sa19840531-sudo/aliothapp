import { config } from '@vue/test-utils'
import { NativeSelectStub } from './NativeSelectStub'

// 页面单测里的可搜索下拉统一换成原生 select（见 NativeSelectStub）
config.global.stubs = { ...config.global.stubs, SearchSelect: NativeSelectStub }
