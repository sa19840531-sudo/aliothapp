<script setup lang="ts">
/**
 * 截图证据缩略图（对齐 2.1 原型：48×64 的首张截图，多于一张时右上角标「+N」）。
 * 点击只抛出 open，由页面打开同一个大图查看器。
 */
withDefaults(defineProps<{ url: string; count: number; title?: string }>(), {
  title: '查看截图证据',
})

defineEmits<{ open: [] }>()
</script>

<template>
  <button type="button" class="evidence-thumb" :title="title" @click="$emit('open')">
    <img v-if="url" :src="url" alt="截图证据" loading="lazy" />
    <span v-else class="evidence-thumb-empty">截图</span>
    <span v-if="count > 1" class="evidence-thumb-more">+{{ count - 1 }}</span>
  </button>
</template>

<style scoped>
.evidence-thumb {
  position: relative;
  display: inline-flex;
  flex-shrink: 0;
  align-items: center;
  justify-content: center;
  width: 48px;
  height: 64px;
  padding: 0;
  border: 1px solid var(--workstation-border);
  border-radius: 6px;
  background: var(--workstation-panel-soft);
  box-shadow: var(--workstation-shadow);
  cursor: pointer;
}

.evidence-thumb img {
  width: 100%;
  height: 100%;
  border-radius: 5px;
  object-fit: cover;
  object-position: top;
}

.evidence-thumb-empty {
  color: var(--workstation-text-faint);
  font-size: 11px;
}

.evidence-thumb-more {
  position: absolute;
  top: -8px;
  right: -8px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 22px;
  height: 18px;
  padding: 0 4px;
  border-radius: 999px;
  color: var(--workstation-panel);
  background: var(--workstation-text-strong);
  font-size: 10px;
  font-weight: 600;
  line-height: 1;
}
</style>
