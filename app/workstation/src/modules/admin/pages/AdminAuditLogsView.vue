<script setup lang="ts">
import { useRouter } from 'vue-router'

/** 操作日志：原型标「即将上线」。供应商切换、连接测试等运维动作暂时记在 AI 服务管理的服务事件里。 */
const router = useRouter()
</script>

<template>
  <div class="ap-page" data-testid="admin-audit-logs">
    <div class="ap-head">
      <div>
        <h1>操作日志</h1>
        <p>记录关键操作与变更（即将上线）</p>
      </div>
    </div>
    <section class="workstation-glass-panel audit-placeholder">
      <p>统一的操作日志还没上线。现阶段：</p>
      <ul>
        <li>供应商切换、连接测试、路由与预警设置的操作记录在「AI 服务管理 → 最近服务事件」。</li>
        <li>产品批量导入的每一次操作记录在「产品管理 → 导入记录」。</li>
      </ul>
      <button type="button" class="qc-btn qc-btn-sm" @click="router.push('/admin/providers')">去看服务事件</button>
    </section>
  </div>
</template>

<style scoped>
.audit-placeholder {
  padding: 24px;
  color: var(--workstation-text-muted);
  font-size: 14px;
  line-height: 1.7;
}

.audit-placeholder p {
  margin: 0 0 8px;
  color: var(--workstation-text);
}

.audit-placeholder ul {
  margin: 0 0 16px;
  padding-left: 20px;
}
</style>
