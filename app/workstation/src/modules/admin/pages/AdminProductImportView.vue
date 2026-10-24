<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import ProductImportPanel from '@/modules/products/components/ProductImportPanel.vue'

/** Excel 产品导入（对齐 2.1 原型 product-import.html）：?tab=record 打开导入记录。 */
const route = useRoute()
const router = useRouter()

const tab = computed<'import' | 'record'>(() => (route.query.tab === 'record' ? 'record' : 'import'))

function setTab(next: 'import' | 'record') {
  const query = { ...route.query }
  if (next === 'record') query.tab = 'record'
  else delete query.tab
  void router.replace({ path: route.path, query })
}
</script>

<template>
  <div class="ap-page" data-testid="admin-product-import">
    <div class="ap-head">
      <div>
        <h1>Excel 产品导入</h1>
        <p>通过 Excel 批量导入产品信息并查看导入结果</p>
      </div>
    </div>
    <section class="workstation-glass-panel">
      <ProductImportPanel :tab="tab" @update:tab="setTab" />
    </section>
  </div>
</template>
