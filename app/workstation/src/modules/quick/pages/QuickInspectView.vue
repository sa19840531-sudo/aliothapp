<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { usePortalNav } from '@/composables/usePortalNav'
import { useMessage } from 'naive-ui'
import QuickStartCard from '@/modules/quick/components/QuickStartCard.vue'

const route = useRoute()
const nav = usePortalNav()
const message = useMessage()

const presetProductCode = computed(() => {
  const value = route.query.product
  return typeof value === 'string' && value ? value : null
})

function handleSubmitted(payload: { task_id: string; task_name: string }) {
  message.info(`「${payload.task_name}」已排队，正在跳转任务详情`)
  void nav.push(`/tasks/${payload.task_id}`)
}
</script>

<template>
  <div class="workstation-page quick-page">
    <header class="page-head">
      <div>
        <h1>快速质检</h1>
        <p>无需选择公司和部门，上传安装包或填写页面地址即可开始本部门质检；需要选择设备等更多设置时用完整配置。</p>
      </div>
      <button type="button" class="ghost-btn" @click="nav.push('/tasks/new')">完整配置任务</button>
    </header>
    <QuickStartCard :preset-product-code="presetProductCode" @submitted="handleSubmitted" />
  </div>
</template>

<style scoped>
.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.page-head h1 {
  margin: 0;
  font-size: 22px;
  color: var(--workstation-text-strong);
}

.page-head p {
  margin: 6px 0 0;
  font-size: 13px;
  color: var(--workstation-text-muted);
}

.ghost-btn {
  padding: 9px 16px;
  border: 1px solid var(--workstation-border);
  border-radius: 10px;
  color: var(--workstation-text);
  background: var(--workstation-panel);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}
</style>
