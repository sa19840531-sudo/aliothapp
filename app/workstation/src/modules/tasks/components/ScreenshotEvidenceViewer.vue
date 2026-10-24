<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { NModal } from 'naive-ui'
import EvidenceImageViewport from '@/components/EvidenceImageViewport.vue'
import ScreenshotCollectionFrame from './ScreenshotCollectionFrame.vue'
import { buildScreenshotActionGroups, type ScreenshotViewerEntry } from './screenshotCollection'

const props = withDefaults(defineProps<{
  entries: ScreenshotViewerEntry[]
  title: string
  collection?: boolean
  initialArtifactId?: string
  imageUrl: (id: string) => string
  imageError?: (id: string) => string
  formatTime: (value: string | null) => string
}>(), { collection: false })

const emit = defineEmits<{
  close: []
  download: [artifactId: string]
}>()

const viewer = ref<HTMLElement | null>(null)
const currentIndex = ref(0)
const overview = ref(false)
const failedImages = ref(new Set<string>())
const openedFrame = ref<{ index: number; key: string } | null>(null)
const hasCollection = computed(() => props.collection && props.entries.length > 1)
const current = computed(() => props.entries[currentIndex.value] ?? null)
const heading = computed(() => overview.value ? `${props.title} · 图片合集` : props.title)
const actionGroups = computed(() => buildScreenshotActionGroups(props.entries))
const currentActions = computed(() => current.value?.actions ?? [])
const currentActionContexts = computed(() => currentActions.value.map((action) => {
  const captureId = current.value?.page_capture_id
  const phase = action.source_capture_id !== action.target_capture_id && captureId
    ? action.source_capture_id === captureId ? 'before' : action.target_capture_id === captureId ? 'after' : null
    : null
  return { action, phase, label: phase === 'before' ? '操作前状态' : phase === 'after' ? '操作后状态' : '采集截图' }
}))
const currentRoleLabel = computed(() => {
  const contexts = currentActionContexts.value
  const opened = openedFrame.value?.index === currentIndex.value ? contexts.find(({ action, phase }) =>
    `${action.id}:${phase}:${current.value?.artifact_id}` === openedFrame.value?.key) : undefined
  if (opened) return opened.label
  const labels = new Set(contexts.map((context) => context.label))
  if (labels.size === 1) return contexts[0]!.label
  return labels.size > 1 ? '多次操作共用截图' : roleLabel(current.value?.capture_role)
})

function entryIndex(entry: ScreenshotViewerEntry) {
  return props.entries.findIndex((item) => item.artifact_id === entry.artifact_id)
}

watch(() => [props.entries, props.collection, props.initialArtifactId] as const, (values, previous) => {
  // 关联规则和动作异步补齐时保留当前放大位置，仅更换合集才重置视图。
  const sameSelection = previous && values[1] === previous[1] && values[2] === previous[2]
    && values[0].length === previous[0].length
    && values[0].every((entry, index) => entry.artifact_id === previous[0][index]?.artifact_id)
  if (sameSelection) return
  const initialIndex = props.entries.findIndex((entry) => entry.artifact_id === props.initialArtifactId)
  currentIndex.value = Math.max(0, initialIndex)
  overview.value = hasCollection.value
  failedImages.value = new Set()
  openedFrame.value = null
}, { immediate: true })

onMounted(async () => {
  await nextTick()
  viewer.value?.focus()
})

function roleLabel(role?: string | null) {
  const labels: Record<string, string> = {
    bootstrap: '初始截图',
    baseline: '初始截图',
    before_action: '操作前',
    after_action: '操作后',
    before: '操作前',
    after: '操作后',
    primary: '主要截图',
    observation: '观察截图',
    reobservation: '重新观察',
    final_observation: '最终观察',
  }
  return labels[role || ''] || '采集截图'
}

async function openImage(index: number, frameKey: string) {
  currentIndex.value = index
  openedFrame.value = { index, key: frameKey }
  overview.value = false
  await nextTick()
  viewer.value?.focus()
}

async function backToCollection() {
  overview.value = true
  await nextTick()
  const frames = [...(viewer.value?.querySelectorAll<HTMLButtonElement>('[data-testid="collection-frame"]') ?? [])]
  // 同一张图片可能既是上一步操作后、又是下一步操作前，返回时保留原来的操作位置。
  const selected = openedFrame.value?.index === currentIndex.value
    ? frames.find((frame) => frame.dataset.frameKey === openedFrame.value?.key) : undefined
  const fallback = frames.find((frame) => frame.dataset.entryIndex === String(currentIndex.value))
  const focusTarget = selected ?? fallback
  focusTarget?.focus()
}

function moveImage(offset: number) {
  currentIndex.value = Math.max(0, Math.min(props.entries.length - 1, currentIndex.value + offset))
}

function handleKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.preventDefault()
    event.stopPropagation()
    emit('close')
    return
  }
  if (overview.value || !current.value) return
  if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
    event.preventDefault()
    moveImage(event.key === 'ArrowLeft' ? -1 : 1)
  }
}

function handleShowUpdate(show: boolean) {
  if (!show) emit('close')
}

function imageFailed(artifactId: string) {
  failedImages.value = new Set([...failedImages.value, artifactId])
}
</script>

<template>
  <n-modal :show="true" :close-on-esc="false" @update:show="handleShowUpdate">
    <div
      ref="viewer"
      class="screenshot-viewer"
      data-testid="screenshot-viewer"
      role="dialog"
      aria-modal="true"
      :aria-label="heading"
      tabindex="-1"
      @keydown="handleKeydown"
    >
      <header class="viewer-head">
        <div class="viewer-heading">
          <h3>{{ heading }}</h3>
          <p v-if="overview">共 {{ entries.length }} 张不同图片，点击图片可放大查看</p>
          <p v-else-if="current">
            {{ formatTime(current.captured_at) }} · {{ currentRoleLabel }} · {{ currentIndex + 1 }} / {{ entries.length }}
            <span v-if="current.duplicate_count && current.duplicate_count > 1"> · 同图采集 {{ current.duplicate_count }} 次</span>
          </p>
        </div>
        <div class="viewer-actions">
          <button v-if="!overview && hasCollection" type="button" class="viewer-action" data-testid="viewer-back" @click="backToCollection">返回合集</button>
          <button v-if="!overview && current" type="button" class="viewer-action" data-testid="viewer-download" @click="emit('download', current.artifact_id)">下载当前截图</button>
          <button type="button" class="viewer-action" data-testid="viewer-close" @click="emit('close')">关闭</button>
        </div>
      </header>

      <div v-if="overview" class="collection-content" data-testid="collection-grid">
        <p v-if="actionGroups.groups.length" class="collection-guide">按每次操作查看前后状态。每张截图下方列出引用它的质检规则。</p>
        <section
          v-for="(group, index) in actionGroups.groups"
          :key="group.action.id"
          class="action-group"
          data-testid="screenshot-action-group"
        >
          <header class="action-heading">
            <span class="action-number">操作 {{ index + 1 }}</span>
            <h4>{{ group.action.action_label }}</h4>
            <span class="action-status">{{ group.action.status_label }}</span>
          </header>
          <p class="action-purpose"><span>操作目的</span>{{ group.action.purpose || '未记录操作目的' }}</p>
          <div class="action-comparison">
            <div class="comparison-side" data-testid="screenshot-action-before">
              <ScreenshotCollectionFrame
                v-for="entry in group.before"
                :key="entry.artifact_id"
                :entry="entry" :index="entryIndex(entry)" label="操作前状态" phase="before"
                :frame-key="`${group.action.id}:before:${entry.artifact_id}`"
                :failed="failedImages.has(entry.artifact_id) || !!imageError?.(entry.artifact_id)" :image-url="imageUrl" :format-time="formatTime"
                @open="openImage" @failed="imageFailed"
              />
              <div v-if="!group.before.length" class="missing-frame"><strong>操作前状态</strong><span>当前合集中未提供操作前截图</span></div>
            </div>
            <span class="comparison-arrow" aria-hidden="true">→</span>
            <div class="comparison-side" data-testid="screenshot-action-after">
              <ScreenshotCollectionFrame
                v-for="entry in group.after"
                :key="entry.artifact_id"
                :entry="entry" :index="entryIndex(entry)" label="操作后状态" phase="after"
                :frame-key="`${group.action.id}:after:${entry.artifact_id}`"
                :failed="failedImages.has(entry.artifact_id) || !!imageError?.(entry.artifact_id)" :image-url="imageUrl" :format-time="formatTime"
                @open="openImage" @failed="imageFailed"
              />
              <div v-if="!group.after.length" class="missing-frame"><strong>操作后状态</strong><span>当前合集中未提供操作后截图</span></div>
            </div>
          </div>
        </section>
        <section v-if="actionGroups.unassociated.length" class="unassociated-section">
          <header class="unassociated-heading">
            <h4>其他采集截图</h4>
            <p>未记录与具体操作的关联，以下截图不按前后配对。</p>
          </header>
          <div class="collection-grid">
            <ScreenshotCollectionFrame
              v-for="entry in actionGroups.unassociated"
              :key="entry.artifact_id"
              :entry="entry" :index="entryIndex(entry)" :label="roleLabel(entry.capture_role)"
              :frame-key="`unassociated:${entry.artifact_id}`"
              :failed="failedImages.has(entry.artifact_id) || !!imageError?.(entry.artifact_id)" :image-url="imageUrl" :format-time="formatTime"
              @open="openImage" @failed="imageFailed"
            />
          </div>
        </section>
      </div>

      <template v-else-if="current">
        <div v-if="currentActions.length" class="current-actions">
          <div v-for="context in currentActionContexts" :key="context.action.id">
            <strong>{{ context.action.action_label }} · {{ context.label }}</strong>
            <span>{{ context.action.purpose || '未记录操作目的' }}</span>
          </div>
        </div>
        <div class="viewer-body">
          <button type="button" class="viewer-nav" data-testid="viewer-prev" aria-label="上一张截图" :disabled="currentIndex === 0" @click="moveImage(-1)">‹</button>
          <EvidenceImageViewport :key="current.artifact_id" data-testid="viewer-image" :src="imageUrl(current.artifact_id)" :error="imageError?.(current.artifact_id)" :alt="current.page_name" />
          <button type="button" class="viewer-nav" data-testid="viewer-next" aria-label="下一张截图" :disabled="currentIndex >= entries.length - 1" @click="moveImage(1)">›</button>
        </div>
        <footer v-if="current.related_rules.length" class="related-rules viewer-foot">
          <span>截图关联规则：</span>
          <span v-for="rule in current.related_rules" :key="`${rule.rule_code}:${rule.display_status}`" class="rule-badge" :class="rule.display_status">
            {{ rule.rule_name }} · {{ rule.display_status_label }}
          </span>
        </footer>
      </template>
      <p v-else class="viewer-empty">暂无截图</p>
    </div>
  </n-modal>
</template>

<style scoped>
.screenshot-viewer {
  width: min(1160px, 94vw);
  max-height: 90vh;
  box-sizing: border-box;
  overflow-y: auto;
  padding: 18px 22px;
  border: 1px solid var(--workstation-border);
  border-radius: 16px;
  background: var(--workstation-panel-strong);
  color: var(--workstation-text);
  outline: none;
}
.viewer-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
.viewer-heading { min-width: 0; }
.viewer-heading h3 { margin: 0; font-size: 15px; color: var(--workstation-text-strong); overflow-wrap: anywhere; }
.viewer-heading p { margin: 6px 0 0; color: var(--workstation-text-muted); font-size: 12px; }
.viewer-actions { display: flex; flex-shrink: 0; flex-wrap: wrap; gap: 8px; }
.viewer-action, .viewer-nav { border: 1px solid var(--workstation-border); background: var(--workstation-panel-soft); color: var(--workstation-text); cursor: pointer; }
.viewer-action { padding: 6px 12px; border-radius: var(--workstation-radius-sm); font-size: 12px; }
.viewer-action:hover, .viewer-nav:not(:disabled):hover { border-color: var(--workstation-primary); color: var(--workstation-primary); }
.viewer-action:focus-visible, .viewer-nav:focus-visible { outline: 2px solid var(--workstation-primary); outline-offset: 3px; }
.collection-content { display: flex; flex-direction: column; gap: 22px; }
.collection-guide { margin: 0; font-size: 12px; color: var(--workstation-text-muted); }
.action-group { min-width: 0; padding: 18px; border: 1px solid var(--workstation-border); border-radius: var(--workstation-radius); }
.action-heading { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; }
.action-heading h4 { flex: 1; min-width: 120px; margin: 0; font-size: 15px; color: var(--workstation-text-strong); overflow-wrap: anywhere; }
.action-number { padding: 4px 8px; border-radius: 5px; background: var(--workstation-panel-soft); color: var(--workstation-primary); font-size: 12px; font-weight: 600; }
.action-status { color: var(--workstation-text-muted); font-size: 12px; }
.action-purpose { display: flex; flex-wrap: wrap; gap: 6px 12px; margin: 12px 0 16px; font-size: 13px; overflow-wrap: anywhere; }
.action-purpose > span { color: var(--workstation-text-muted); }
.action-comparison { display: grid; grid-template-columns: minmax(0, 1fr) 24px minmax(0, 1fr); gap: 12px; align-items: stretch; }
.comparison-side { display: flex; flex-direction: column; min-width: 0; gap: 12px; }
.comparison-arrow { align-self: center; text-align: center; color: var(--workstation-primary); font-size: 24px; }
.missing-frame { display: flex; flex: 1; min-height: 160px; flex-direction: column; align-items: center; justify-content: center; gap: 12px; padding: 16px; border: 1px dashed var(--workstation-border); border-radius: var(--workstation-radius); color: var(--workstation-text-muted); font-size: 12px; text-align: center; }
.missing-frame strong { font-size: 14px; }
.unassociated-heading h4 { margin: 0; color: var(--workstation-text-strong); font-size: 14px; }
.unassociated-heading p { margin: 6px 0 12px; color: var(--workstation-text-muted); font-size: 12px; }
.current-actions { display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px; padding: 10px 12px; background: var(--workstation-panel-soft); border-radius: var(--workstation-radius-sm); font-size: 12px; }
.current-actions > div { display: flex; flex-wrap: wrap; gap: 6px 12px; }
.current-actions span { color: var(--workstation-text-muted); overflow-wrap: anywhere; }
.collection-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(240px, 100%), 1fr)); gap: 16px; }
.viewer-empty { padding: 24px 8px; color: var(--workstation-text-muted); font-size: 12px; }
.viewer-body { display: flex; align-items: center; gap: 12px; min-height: 0; }
.viewer-nav { display: flex; align-items: center; justify-content: center; width: 36px; height: 36px; flex-shrink: 0; border-radius: 50%; font-size: 26px; }
.viewer-nav:disabled { opacity: 0.35; cursor: not-allowed; }
.related-rules { display: flex; flex-wrap: wrap; gap: 6px; font-size: 11px; }
.viewer-foot { margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--workstation-border); color: var(--workstation-text-muted); }
.rule-badge { padding: 2px 6px; border: 1px solid var(--workstation-border); border-radius: 5px; color: var(--workstation-text-muted); }
.rule-badge.passed { color: var(--workstation-success); }
.rule-badge.failed { color: var(--workstation-danger); }
.rule-badge.needs_review, .rule-badge.evidence_insufficient, .rule-badge.error { color: var(--workstation-warning); }
@media (max-width: 640px) {
  .screenshot-viewer { padding: 16px 12px; }
  .viewer-head { align-items: flex-start; flex-direction: column; gap: 10px; }
  .collection-grid { grid-template-columns: 1fr; }
  .action-group { padding: 12px; }
  .action-comparison { grid-template-columns: minmax(0, 1fr); gap: 10px; }
  .comparison-arrow { transform: rotate(90deg); }
  .viewer-body { gap: 6px; }
  .viewer-nav { width: 28px; height: 32px; }
}
</style>
