<script setup lang="ts">
import { NCard, NList, NListItem, NTag, NSpace } from 'naive-ui'
</script>

<template>
  <n-space vertical :size="12">
    <h3 class="section-title">角色管理</h3>
    <n-card>
      <n-list>
        <n-list-item>
          <strong>平台管理员</strong>
          <span class="role-desc">总部账号，能看所有公司的任务，管所有公司和账号</span>
          <template #suffix><n-tag round type="error">高权限</n-tag></template>
        </n-list-item>
        <n-list-item>
          <strong>公司管理员</strong>
          <span class="role-desc">只管本公司的账号，能看本公司全部任务</span>
          <template #suffix><n-tag round type="warning">本公司</n-tag></template>
        </n-list-item>
        <n-list-item>
          <strong>规则配置员</strong>
          <span class="role-desc">发起任务，只看自己发起的任务</span>
          <template #suffix><n-tag round type="info">配置</n-tag></template>
        </n-list-item>
        <n-list-item>
          <strong>审计员</strong>
          <span class="role-desc">只读，能看本公司全部任务</span>
          <template #suffix><n-tag round type="success">只读</n-tag></template>
        </n-list-item>
      </n-list>
    </n-card>
  </n-space>
</template>

<style scoped>
.role-desc {
  margin-left: 12px;
  color: #688098;
  font-size: 13px;
}
</style>
