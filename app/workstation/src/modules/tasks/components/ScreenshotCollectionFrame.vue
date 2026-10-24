<script setup lang="ts">
import type { ScreenshotViewerEntry } from './screenshotCollection'

defineProps<{
  entry: ScreenshotViewerEntry
  index: number
  label: string
  phase?: 'before' | 'after'
  frameKey: string
  failed: boolean
  imageUrl: (id: string) => string
  formatTime: (value: string | null) => string
}>()

defineEmits<{ open: [index: number, frameKey: string]; failed: [artifactId: string] }>()
</script>

<template>
  <button
    type="button"
    class="collection-frame"
    :class="phase"
    data-testid="collection-frame"
    :data-artifact-id="entry.artifact_id"
    :data-entry-index="index"
    :data-frame-key="frameKey"
    :aria-label="`${entry.page_name}，${label}，放大第 ${index + 1} 张截图`"
    @click="$emit('open', index, frameKey)"
  >
    <span class="frame-heading">
      <strong class="frame-role">{{ label }}</strong>
      <span class="frame-time">{{ formatTime(entry.captured_at) }}</span>
    </span>
    <span class="frame-image">
      <img
        v-if="imageUrl(entry.artifact_id) && !failed"
        :src="imageUrl(entry.artifact_id)"
        :alt="`${entry.page_name} · ${label}`"
        @error="$emit('failed', entry.artifact_id)"
      >
      <span v-else-if="failed" class="frame-unavailable">截图暂不可用，点击查看或下载原图</span>
      <span v-else class="frame-unavailable">正在加载截图…</span>
    </span>
    <span class="frame-caption">
      <span>{{ entry.page_name }}</span>
      <span v-if="entry.duplicate_count > 1">同图采集 {{ entry.duplicate_count }} 次</span>
      <span class="frame-zoom">点击放大</span>
    </span>
    <span v-if="entry.related_rules.length" class="frame-rules">
      <span class="frame-rules-label">截图关联规则</span>
      <span v-for="rule in entry.related_rules" :key="`${rule.rule_code}:${rule.display_status}`" class="rule-badge" :class="rule.display_status">
        {{ rule.rule_name }} · {{ rule.display_status_label }}
      </span>
    </span>
  </button>
</template>

<style scoped>
.collection-frame { display: flex; flex-direction: column; width: 100%; min-width: 0; padding: 14px; gap: 12px; text-align: left; border: 1px solid var(--workstation-border); border-radius: var(--workstation-radius); background: var(--workstation-panel-soft); color: var(--workstation-text); cursor: zoom-in; }
.collection-frame:hover { border-color: var(--workstation-primary); }
.collection-frame:focus-visible { outline: 2px solid var(--workstation-primary); outline-offset: 3px; }
.frame-heading { display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 6px 10px; }
.frame-role { color: var(--workstation-text-strong); font-size: 14px; }
.before .frame-role { color: var(--workstation-text-muted); }
.after .frame-role { color: var(--workstation-primary); }
.frame-time { color: var(--workstation-text-muted); font-size: 12px; }
.frame-image { display: flex; align-items: center; justify-content: center; width: 100%; min-height: 160px; }
.frame-image img { display: block; max-width: 100%; max-height: 46vh; object-fit: contain; border-radius: var(--workstation-radius-sm); }
.frame-unavailable { padding: 24px 8px; color: var(--workstation-text-muted); font-size: 12px; }
.frame-caption { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 10px; color: var(--workstation-text-muted); font-size: 11px; }
.frame-zoom { margin-left: auto; }
.frame-rules { display: flex; flex-wrap: wrap; gap: 6px; padding-top: 10px; border-top: 1px solid var(--workstation-border); font-size: 11px; }
.frame-rules-label { width: 100%; color: var(--workstation-text-muted); }
.rule-badge { padding: 2px 6px; border: 1px solid var(--workstation-border); border-radius: 5px; color: var(--workstation-text-muted); overflow-wrap: anywhere; }
.rule-badge.passed { color: var(--workstation-success); }
.rule-badge.failed { color: var(--workstation-danger); }
.rule-badge.needs_review, .rule-badge.evidence_insufficient, .rule-badge.error { color: var(--workstation-warning); }
</style>
