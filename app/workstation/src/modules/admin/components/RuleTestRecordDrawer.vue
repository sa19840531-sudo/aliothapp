<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { NDrawer, NDrawerContent, NImage, NImageGroup } from 'naive-ui'
import type { ImageInst } from 'naive-ui'
import { pageStateName } from '@/utils/exploration-labels'
import {
  fetchRuleTestRecordScreenshot,
  getRuleTestRecord,
  type RuleTestPrefill,
  type RuleTestRecordDetail,
} from '../api/ruleTests'
import RuleTestResultCard from './RuleTestResultCard.vue'
import { formatRuleTestTime, ruleTestPlatformLabel } from './ruleTestOutcome'

/** 一条测试记录：当时的结论（与测试页同一张结果卡片）+ 当时的全部证据，可以拿这份证据回去重测。 */
const props = defineProps<{
  /** 要看的记录；为空时抽屉收起 */
  recordId: string | null
}>()
const emit = defineEmits<{
  close: []
  retest: [prefill: RuleTestPrefill]
}>()

// 源码、界面快照可能有几 MB，这里只显示开头一段；重测时用全文
const PREVIEW_CHARACTERS = 100_000

interface LoadedShot {
  index: number
  filename: string
  /** primary 页面截图 / before 操作前的截图 */
  role: string
  url: string
  blob: Blob
}

const loading = ref(false)
const loadError = ref('')
const detail = ref<RuleTestRecordDetail | null>(null)
const shots = ref<LoadedShot[]>([])
let loadSeq = 0

const show = computed({
  get: () => props.recordId !== null,
  set: (value: boolean) => {
    if (!value) emit('close')
  },
})
const missingShots = computed(() =>
  detail.value ? detail.value.screenshots.length - shots.value.length : 0,
)
// 换记录时整组重建：组件库的图片组不会注销旧图，放大翻页会翻到上一条记录的截图
const shotsGroupKey = computed(() => shots.value.map((item) => item.url).join('|'))
const pageShots = computed(() => shots.value.filter((item) => item.role !== 'before'))
// 结论里的证据引用形如「截图 1（01.png）」「操作前的截图（xx.png）」，按末尾括号里的文件名对回这张图
const imageRefs = new Map<number, ImageInst>()
function setImageRef(index: number, instance: unknown) {
  if (instance) imageRefs.set(index, instance as ImageInst)
  else imageRefs.delete(index)
}
function shotOfEvidence(name: string): LoadedShot | null {
  const filename = /（([^（）]+)）\s*$/.exec(name)?.[1]
  if (!filename) return null
  return shots.value.find((item) => item.filename === filename) ?? null
}
const previewableEvidence = computed(() => {
  const result = detail.value?.result
  if (!result) return []
  const names = new Set<string>()
  for (const name of [...result.findings.flatMap((item) => item.evidence), ...result.used_evidence])
    if (shotOfEvidence(name)) names.add(name)
  return [...names]
})
function openEvidenceShot(name: string) {
  const shot = shotOfEvidence(name)
  if (shot) imageRefs.get(shot.index)?.showPreview()
}
const beforeShot = computed(() => shots.value.find((item) => item.role === 'before') ?? null)
const textBlocks = computed(() => {
  const record = detail.value
  if (!record) return []
  return [
    { key: 'visible_text', label: '页面文字', text: record.visible_text },
    {
      key: 'second_location_text',
      label: '第二个位置的页面文字',
      text: record.second_location_text ?? '',
    },
    { key: 'dom', label: '页面源码（HTML）', text: record.dom_html },
    {
      key: 'ui_snapshot',
      label: '界面快照（页面元素结构）',
      text: record.ui_snapshot ? JSON.stringify(record.ui_snapshot, null, 2) : '',
    },
    {
      key: 'judge_input',
      label: '补充判定输入',
      text: Object.keys(record.judge_input).length
        ? JSON.stringify(record.judge_input, null, 2)
        : '',
    },
  ].filter((item) => item.text.trim())
})

watch(
  () => props.recordId,
  (recordId) => void load(recordId),
  { immediate: true },
)
onBeforeUnmount(releaseShots)

function releaseShots() {
  shots.value.forEach((item) => URL.revokeObjectURL(item.url))
  shots.value = []
}

async function load(recordId: string | null) {
  loadSeq += 1
  const seq = loadSeq
  releaseShots()
  detail.value = null
  loadError.value = ''
  if (!recordId) return
  loading.value = true
  try {
    const record = await getRuleTestRecord(recordId)
    if (seq !== loadSeq) return
    detail.value = record
    const loaded = await Promise.all(
      record.screenshots.map(async (item): Promise<LoadedShot | null> => {
        try {
          const blob = await fetchRuleTestRecordScreenshot(record.id, item.index)
          return {
            index: item.index,
            filename: item.filename,
            role: item.capture_role ?? 'primary',
            blob,
            url: URL.createObjectURL(blob),
          }
        } catch {
          return null
        }
      }),
    )
    const present = loaded.filter((item): item is LoadedShot => item !== null)
    if (seq !== loadSeq) {
      present.forEach((item) => URL.revokeObjectURL(item.url))
      return
    }
    shots.value = present
  } catch (error) {
    if (seq === loadSeq) loadError.value = error instanceof Error ? error.message : '记录加载失败'
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}

function preview(text: string) {
  return text.length > PREVIEW_CHARACTERS ? text.slice(0, PREVIEW_CHARACTERS) : text
}

function shotLabel(item: LoadedShot) {
  if (item.role === 'before') return '操作前的截图'
  return `截图 ${pageShots.value.indexOf(item) + 1}`
}

function toFile(item: LoadedShot) {
  return new File([item.blob], item.filename, { type: 'image/png' })
}

function retest() {
  const record = detail.value
  if (!record) return
  emit('retest', {
    ruleId: record.rule_id,
    ruleCode: record.rule_code,
    pageUrl: record.page_url,
    pageTitle: record.page_title,
    visibleText: record.visible_text,
    domHtml: record.dom_html,
    uiSnapshot: record.ui_snapshot ? JSON.stringify(record.ui_snapshot, null, 2) : '',
    judgeInput: Object.keys(record.judge_input).length
      ? JSON.stringify(record.judge_input, null, 2)
      : '',
    screenshots: pageShots.value.map(toFile),
    beforeScreenshot: beforeShot.value ? toFile(beforeShot.value) : null,
    secondLocationText: record.second_location_text ?? '',
  })
}
</script>

<template>
  <n-drawer v-model:show="show" width="720" placement="right">
    <n-drawer-content title="测试记录" closable>
      <p v-if="loading" class="rr-muted">记录加载中…</p>
      <p v-else-if="loadError" class="ap-note is-warning">{{ loadError }}</p>
      <div v-else-if="detail" class="rr-detail" data-testid="rule-test-record-detail">
        <div class="rr-head">
          <dl class="rr-facts">
            <dt>测试时间</dt>
            <dd>{{ formatRuleTestTime(detail.created_at) }}</dd>
            <dt>测试人</dt>
            <dd>{{ detail.tester_name }}</dd>
            <dt>端</dt>
            <dd>{{ ruleTestPlatformLabel(detail.platform) }}</dd>
            <template v-if="detail.page_url">
              <dt>页面网址</dt>
              <dd class="rr-break">{{ detail.page_url }}</dd>
            </template>
            <template v-if="detail.page_title">
              <dt>页面标题</dt>
              <dd class="rr-break">{{ detail.page_title }}</dd>
            </template>
            <template v-if="detail.page_state">
              <dt>页面状态</dt>
              <dd>{{ pageStateName(detail.page_state) }}</dd>
            </template>
          </dl>
          <button
            type="button"
            class="qc-btn qc-btn-primary"
            data-testid="rule-test-record-retest"
            @click="retest"
          >
            用这份证据重测
          </button>
        </div>

        <section class="rr-section">
          <h4>当时的结论</h4>
          <RuleTestResultCard
            :result="detail.result"
            :ran-at="formatRuleTestTime(detail.created_at)"
            :previewable-evidence="previewableEvidence"
            @preview-evidence="openEvidenceShot"
          />
        </section>

        <section class="rr-section">
          <h4>当时的证据</h4>
          <p v-if="!shots.length && !textBlocks.length && !missingShots" class="rr-muted">
            没有留下证据
          </p>
          <n-image-group v-if="shots.length" :key="shotsGroupKey">
            <ul class="rr-shots">
              <li v-for="item in shots" :key="item.index">
                <n-image
                  :ref="(el: unknown) => setImageRef(item.index, el)"
                  :src="item.url"
                  :alt="item.filename"
                  width="120"
                  height="160"
                  object-fit="cover"
                  :img-props="{ title: '点击放大' }"
                />
                <span>{{ shotLabel(item) }}</span>
              </li>
            </ul>
          </n-image-group>
          <p v-if="missingShots > 0" class="rr-muted">
            有 {{ missingShots }} 张截图文件已经不在了，重测时不会带上
          </p>
          <details v-for="block in textBlocks" :key="block.key" class="rr-text">
            <summary>{{ block.label }}（{{ block.text.length.toLocaleString('zh-CN') }} 字）</summary>
            <pre>{{ preview(block.text) }}</pre>
            <p v-if="block.text.length > PREVIEW_CHARACTERS" class="rr-muted">
              内容太长，这里只显示开头一段；重测时会用全文
            </p>
          </details>
        </section>
      </div>
    </n-drawer-content>
  </n-drawer>
</template>

<style scoped>
.rr-detail {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.rr-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.rr-facts {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 6px 12px;
  margin: 0;
  font-size: 13px;
}

.rr-facts dt {
  color: var(--workstation-text-muted);
  white-space: nowrap;
}

.rr-facts dd {
  margin: 0;
  color: var(--workstation-text);
}

.rr-break {
  overflow-wrap: anywhere;
}

.rr-section {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding-top: 16px;
  border-top: 1px solid var(--workstation-border);
}

.rr-section h4 {
  margin: 0;
  color: var(--workstation-text-strong);
  font-size: 14px;
}

.rr-shots {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.rr-shots li {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  color: var(--workstation-text-muted);
  font-size: 12px;
}

.rr-shots :deep(img) {
  width: 120px;
  height: 160px;
  object-fit: cover;
  border: 1px solid var(--workstation-border);
  border-radius: 6px;
  background: var(--workstation-panel);
  cursor: zoom-in;
}

.rr-text summary {
  color: var(--workstation-text);
  font-size: 13px;
  cursor: pointer;
}

.rr-text pre {
  max-height: 360px;
  margin: 8px 0 0;
  padding: 12px;
  overflow: auto;
  border: 1px solid var(--workstation-border);
  border-radius: var(--workstation-radius-sm);
  background: var(--workstation-panel-soft);
  color: var(--workstation-text);
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
}

.rr-muted {
  margin: 0;
  color: var(--workstation-text-muted);
  font-size: 12px;
  line-height: 1.6;
}
</style>
