<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import {
  NImage,
  NImageGroup,
  NSelect,
  useMessage,
  type SelectGroupOption,
  type SelectOption,
} from 'naive-ui'
import { pageStateName } from '@/utils/exploration-labels'
import { categoryLabel } from '@/utils/rule-labels'
import { SEVERITY_META } from '@/utils/status'
import {
  captureRuleTestPage,
  listRuleTestOptions,
  runRuleTest,
  type RuleTestOption,
  type RuleTestPageType,
  type RuleTestPlatform,
  type RuleTestPrefill,
  type RuleTestRunResult,
} from '../api/ruleTests'
import RuleTestResultCard from './RuleTestResultCard.vue'
import { ruleTestOutcomeLabel, ruleTestOutcomeTone } from './ruleTestOutcome'

/**
 * 规则配置 → 规则测试：选一条规则，按这条规则在正式任务里要的证据放出对应输入框，
 * 填网址可以用正式采集同一套程序把页面抓下来填进去；判定与正式任务相同。
 * 每次测试的证据和结论存进「测试记录」，不进任务、不进报告、不改规则。
 */
const props = defineProps<{
  /** 板块当前是否可见；每次切回来重新拉规则，列表里刚改的配置立刻体现 */
  active: boolean
}>()

const MAX_SCREENSHOTS = 6
const MAX_SCREENSHOT_BYTES = 8 * 1024 * 1024
const MAX_HISTORY = 20

const EXECUTOR_LABELS: Record<string, string> = {
  llm: '大模型判定',
  rule_engine: '规则引擎（固定检查）',
  hybrid: '混合（固定检查 + 大模型）',
}
const RULE_STATUS_LABELS: Record<string, string> = {
  draft: '草稿',
  enabled: '启用',
  disabled: '停用',
  archived: '归档',
}
const CLIENT_LABELS: Record<string, string> = { H5: 'H5', WEB: 'Web', APP: 'App' }

interface ScreenshotItem {
  id: number
  file: File
  url: string
}

interface HistoryItem {
  id: number
  ranAt: string
  result: RuleTestRunResult
}

const message = useMessage()

const loading = ref(true)
const loadError = ref('')
const options = ref<RuleTestOption[]>([])
const pageTypes = ref<RuleTestPageType[]>([])
const selectedRuleId = ref<string | null>(null)

const form = reactive({
  pageUrl: '',
  pageTitle: '',
  visibleText: '',
  domHtml: '',
  uiSnapshot: '',
  judgeInput: '',
  secondLocationText: '',
})
const screenshots = ref<ScreenshotItem[]>([])
/** 比「操作前 / 操作后」的规则：操作前那张截图（页面截图算操作后） */
const beforeShot = ref<ScreenshotItem | null>(null)
const dragging = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const beforeFileInput = ref<HTMLInputElement | null>(null)
let screenshotSeq = 0
/** 上一次按网址抓来的截图；重新抓取时替换它，不重复堆 */
let capturedScreenshotId: number | null = null

const fetching = ref(false)
const running = ref(false)
const history = ref<HistoryItem[]>([])
const activeHistoryId = ref<number | null>(null)
let historySeq = 0

const selectedRule = computed(
  () => options.value.find((item) => item.rule_id === selectedRuleId.value) ?? null,
)
const pageTypeNames = computed(
  () => new Map(pageTypes.value.map((item) => [item.code, item.name])),
)

/** 端、页面类型、页面状态按规则在正式任务里的挑选条件自动带，不再让人选。 */
const platform = computed<RuleTestPlatform>(() => {
  const clients = selectedRule.value?.target_clients ?? []
  if (!clients.length || clients.includes('H5')) return 'h5'
  if (clients.includes('WEB')) return 'web'
  return 'android'
})
const isApp = computed(() => platform.value === 'android')
const pageType = computed(() => selectedRule.value?.applicable_page_types[0] ?? '')
const pageState = computed(() => selectedRule.value?.required_page_states[0] ?? 'stable')

const needTypes = computed(
  () => new Set((selectedRule.value?.evidence_needs ?? []).map((item) => item.artifact_type)),
)
/** 规则没声明测试页能给的证据时，按最常用的截图 + 页面文字兜底 */
const fallbackInputs = computed(
  () =>
    Boolean(selectedRule.value) &&
    !(selectedRule.value?.evidence_needs ?? []).some((item) => item.form_supported),
)
const showScreenshots = computed(
  () =>
    needTypes.value.has('screenshot') ||
    (selectedRule.value?.min_screenshot_count ?? 0) > 0 ||
    fallbackInputs.value,
)
const showVisibleText = computed(() => needTypes.value.has('visible_text') || fallbackInputs.value)
const showDom = computed(() => needTypes.value.has('dom') && !isApp.value)
const showUiSnapshot = computed(() => needTypes.value.has('ui_snapshot'))
const judgeInputFields = computed(() => selectedRule.value?.judge_input_fields ?? [])
const showUrlCapture = computed(() => Boolean(selectedRule.value) && !isApp.value)
const unsupportedNeeds = computed(() =>
  (selectedRule.value?.evidence_needs ?? [])
    .filter((item) => !item.form_supported)
    .map((item) => item.label),
)
const judgeInputPlaceholder = computed(() =>
  JSON.stringify(Object.fromEntries(judgeInputFields.value.map((field) => [field, '']))),
)
/** 正式任务里另一组采集才有的证据：规则要、测试页也给得了才放框 */
const beforeInput = computed(
  () => (selectedRule.value?.extra_inputs ?? []).find((item) => item.role === 'before') ?? null,
)
const secondLocationInput = computed(
  () =>
    (selectedRule.value?.extra_inputs ?? []).find((item) => item.role === 'location_1') ?? null,
)
// 比操作前后的规则按采集角色各取一张截图，页面截图就是操作后那张
const maxScreenshots = computed(() => (beforeInput.value ? 1 : MAX_SCREENSHOTS))
const screenshotLabel = computed(() => (beforeInput.value ? '操作后的截图' : '页面截图'))
// 截图增删后整组重建：组件库的图片组不会注销移除掉的图，放大翻页时会翻到已移除的截图
const screenshotGroupKey = computed(() => screenshots.value.map((item) => item.id).join('-'))

const hasEvidence = computed(
  () =>
    (showScreenshots.value && screenshots.value.length > 0) ||
    (showVisibleText.value && Boolean(form.visibleText.trim())) ||
    (showDom.value && Boolean(form.domHtml.trim())) ||
    (showUiSnapshot.value && Boolean(form.uiSnapshot.trim())),
)
const canRun = computed(
  () => Boolean(selectedRule.value?.testable) && hasEvidence.value && !running.value,
)
const activeHistory = computed(
  () =>
    history.value.find((item) => item.id === activeHistoryId.value) ?? history.value[0] ?? null,
)

const ruleSelectOptions = computed<Array<SelectOption | SelectGroupOption>>(() => {
  const groups = new Map<string, SelectGroupOption & { children: SelectOption[] }>()
  for (const item of options.value) {
    let group = groups.get(item.ruleset_id)
    if (!group) {
      const suffix = item.ruleset_status === 'enabled' ? '' : '（未启用）'
      group = {
        type: 'group',
        key: item.ruleset_id,
        label: `${item.ruleset_name || item.ruleset_code} · v${item.ruleset_version}${suffix}`,
        children: [],
      }
      groups.set(item.ruleset_id, group)
    }
    group.children.push({
      label: ruleOptionLabel(item),
      value: item.rule_id,
      disabled: !item.testable,
    })
  }
  return [...groups.values()]
})

/** 最近一次拉规则列表；从测试记录带证据回来重测时要等它拉完再选规则 */
let optionsLoaded: Promise<void> = Promise.resolve()

watch(
  () => props.active,
  (active) => {
    if (active) optionsLoaded = loadOptions()
  },
  { immediate: true },
)
onBeforeUnmount(() => {
  screenshots.value.forEach((item) => URL.revokeObjectURL(item.url))
  if (beforeShot.value) URL.revokeObjectURL(beforeShot.value.url)
})

async function loadOptions() {
  loading.value = true
  try {
    const response = await listRuleTestOptions()
    options.value = response.items
    pageTypes.value = response.page_types
    loadError.value = ''
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '规则列表加载失败'
  } finally {
    loading.value = false
  }
}

function ruleOptionLabel(item: RuleTestOption) {
  const tags: string[] = []
  if (item.status !== 'enabled') tags.push(RULE_STATUS_LABELS[item.status] ?? item.status)
  if (!item.testable) tags.push('暂不支持单页测试')
  return `${item.rule_code} · ${item.name}${tags.length ? `（${tags.join('，')}）` : ''}`
}

function pageTypeName(code: string) {
  return pageTypeNames.value.get(code) ?? code
}

function severityClass(severity: string) {
  return SEVERITY_META[severity]?.cls ?? 'sev-unknown'
}

function usableImage(file: File): boolean {
  if (!file.type.startsWith('image/')) {
    message.warning(`「${file.name}」不是图片，已跳过`)
    return false
  }
  if (file.size > MAX_SCREENSHOT_BYTES) {
    message.warning(`「${file.name}」超过 8MB，已跳过`)
    return false
  }
  return true
}

function addFiles(files: File[]): number[] {
  const added: number[] = []
  for (const file of files) {
    if (!usableImage(file)) continue
    if (screenshots.value.length >= maxScreenshots.value) {
      message.warning(
        beforeInput.value
          ? '操作后的截图只放 1 张'
          : `截图最多 ${MAX_SCREENSHOTS} 张（判定一次最多看这么多张）`,
      )
      break
    }
    screenshotSeq += 1
    screenshots.value.push({ id: screenshotSeq, file, url: URL.createObjectURL(file) })
    added.push(screenshotSeq)
  }
  return added
}

/** 操作前的截图只有一张，再放就换掉；传 null 清空 */
function setBeforeShot(file: File | null) {
  if (file && !usableImage(file)) return
  if (beforeShot.value) URL.revokeObjectURL(beforeShot.value.url)
  beforeShot.value = null
  if (!file) return
  screenshotSeq += 1
  beforeShot.value = { id: screenshotSeq, file, url: URL.createObjectURL(file) }
}

function handleBeforeInput(event: Event) {
  const input = event.target as HTMLInputElement
  const [file] = Array.from(input.files ?? [])
  if (file) setBeforeShot(file)
  input.value = ''
}

function handleBeforeDrop(event: DragEvent) {
  const [file] = Array.from(event.dataTransfer?.files ?? [])
  if (file) setBeforeShot(file)
}

function handleBeforePaste(event: ClipboardEvent) {
  const [file] = Array.from(event.clipboardData?.files ?? [])
  if (!file) return
  event.preventDefault()
  setBeforeShot(new File([file], '粘贴的操作前截图.png', { type: file.type }))
}

function handleFileInput(event: Event) {
  const input = event.target as HTMLInputElement
  addFiles(Array.from(input.files ?? []))
  input.value = ''
}

function handleDrop(event: DragEvent) {
  dragging.value = false
  addFiles(Array.from(event.dataTransfer?.files ?? []))
}

function handlePaste(event: ClipboardEvent) {
  const files = Array.from(event.clipboardData?.files ?? [])
  if (!files.length) return
  event.preventDefault()
  // 剪贴板里的截图都叫 image.png，重命名方便在结果里对上号
  addFiles(
    files.map(
      (file, index) =>
        new File([file], `粘贴截图-${screenshotSeq + index + 1}.png`, { type: file.type }),
    ),
  )
}

function removeScreenshot(id: number) {
  const target = screenshots.value.find((item) => item.id === id)
  if (target) URL.revokeObjectURL(target.url)
  screenshots.value = screenshots.value.filter((item) => item.id !== id)
  if (capturedScreenshotId === id) capturedScreenshotId = null
}

function pngFileFromBase64(base64: string, name: string) {
  const binary = atob(base64)
  const bytes = new Uint8Array(binary.length)
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index)
  return new File([bytes], name, { type: 'image/png' })
}

/** 按网址把页面打开一次，抓下来的内容只填进这条规则要的输入框，填完还能改。 */
async function handleCapturePage() {
  const url = form.pageUrl.trim()
  if (!/^https?:\/\//i.test(url)) {
    message.error('请填以 http:// 或 https:// 开头的网址')
    return
  }
  fetching.value = true
  try {
    const page = await captureRuleTestPage({
      url,
      platform: platform.value,
      ...(pageType.value ? { page_type: pageType.value } : {}),
    })
    form.pageUrl = page.final_url || url
    form.pageTitle = page.title
    if (showVisibleText.value) form.visibleText = page.visible_text
    if (showDom.value) form.domHtml = page.dom_html
    if (showUiSnapshot.value && page.ui_snapshot) {
      form.uiSnapshot = JSON.stringify(page.ui_snapshot, null, 2)
    }
    if (showScreenshots.value && page.screenshot_png_base64) {
      if (capturedScreenshotId !== null) removeScreenshot(capturedScreenshotId)
      const [added] = addFiles([pngFileFromBase64(page.screenshot_png_base64, '网址截图.png')])
      capturedScreenshotId = added ?? null
    }
    page.notices.forEach((notice) => message.warning(notice))
    message.success('已抓取页面，内容填进了下面的输入框，可以再改')
  } catch (error) {
    message.error(`抓取失败：${error instanceof Error ? error.message : '请稍后重试'}`)
  } finally {
    fetching.value = false
  }
}

/** 把测试记录里的证据填回来重测：规则按 ID 找，找不到再按编码找；证据全部替换成记录里的。 */
async function prefill(data: RuleTestPrefill) {
  await optionsLoaded
  const rule =
    options.value.find((item) => item.rule_id === data.ruleId) ??
    options.value.find((item) => item.rule_code === data.ruleCode) ??
    null
  selectedRuleId.value = rule?.rule_id ?? null
  form.pageUrl = data.pageUrl
  form.pageTitle = data.pageTitle
  form.visibleText = data.visibleText
  form.domHtml = data.domHtml
  form.uiSnapshot = data.uiSnapshot
  form.judgeInput = data.judgeInput
  form.secondLocationText = data.secondLocationText ?? ''
  screenshots.value.forEach((item) => URL.revokeObjectURL(item.url))
  screenshots.value = []
  capturedScreenshotId = null
  addFiles(data.screenshots)
  setBeforeShot(data.beforeScreenshot ?? null)
  if (rule) {
    message.success('已把这条记录的证据填好，确认后点「开始检查」')
  } else {
    message.warning(`规则 ${data.ruleCode} 已经不在了，证据已填好，请重新选一条规则`)
  }
}

defineExpose({ prefill })

function jsonProblem(text: string, label: string): string | null {
  if (!text.trim()) return null
  try {
    const parsed: unknown = JSON.parse(text)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return `${label}必须是 JSON 对象（用大括号包起来）`
    }
    return null
  } catch {
    return `${label}不是有效的 JSON`
  }
}

async function handleRun() {
  const rule = selectedRule.value
  if (!rule) {
    message.error('请先选一条规则')
    return
  }
  if (!rule.testable) {
    message.error(rule.untestable_reason ?? '这条规则暂不支持单页测试')
    return
  }
  if (!hasEvidence.value) {
    message.error('请先填证据：可以填网址自动抓取，也可以自己上传截图、粘贴内容')
    return
  }
  const uiSnapshot = showUiSnapshot.value ? form.uiSnapshot : ''
  const judgeInput = judgeInputFields.value.length ? form.judgeInput : ''
  const problem = jsonProblem(uiSnapshot, '界面快照') ?? jsonProblem(judgeInput, '补充判定输入')
  if (problem) {
    message.error(problem)
    return
  }
  running.value = true
  try {
    const result = await runRuleTest({
      ruleId: rule.rule_id,
      platform: platform.value,
      checkMode: 'relaxed',
      pageType: pageType.value,
      pageState: pageState.value,
      pageUrl: form.pageUrl.trim(),
      pageTitle: form.pageTitle,
      appPackageName: '',
      visibleText: showVisibleText.value ? form.visibleText : '',
      domHtml: showDom.value ? form.domHtml : '',
      uiSnapshot,
      judgeInput,
      screenshots: showScreenshots.value ? screenshots.value.map((item) => item.file) : [],
      beforeScreenshot: beforeInput.value ? (beforeShot.value?.file ?? null) : null,
      secondLocationText: secondLocationInput.value ? form.secondLocationText : '',
    })
    historySeq += 1
    const item: HistoryItem = {
      id: historySeq,
      ranAt: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
      result,
    }
    history.value = [item, ...history.value].slice(0, MAX_HISTORY)
    activeHistoryId.value = item.id
  } catch (error) {
    message.error(`检查失败：${error instanceof Error ? error.message : '请稍后重试'}`)
  } finally {
    running.value = false
  }
}
</script>

<template>
  <div class="rt-page" data-testid="rule-test-panel">
    <p class="ap-note">
      在这里单独测试一条规则：选好规则后，页面会按这条规则在正式任务里要的证据放出对应的输入框；填网址可以自动把页面抓下来填进去。系统用和正式任务完全相同的判定逻辑检查，告诉你能不能通过、为什么不通过。每次测试的证据和结论会存进「测试记录」，不会进任务和报告，也不会改动规则。
    </p>

    <div class="rt-grid">
      <section class="workstation-glass-panel rt-panel">
        <h3 class="rt-step">1 选规则</h3>
        <p v-if="loadError" class="ap-note is-warning">规则列表加载失败：{{ loadError }}</p>
        <n-select
          v-model:value="selectedRuleId"
          :options="ruleSelectOptions"
          :loading="loading"
          filterable
          clearable
          placeholder="搜索规则编码或名称"
          data-testid="rule-test-rule-select"
        />

        <div v-if="selectedRule" class="rt-rule-card" data-testid="rule-test-rule-card">
          <div class="rt-rule-head">
            <strong>{{ selectedRule.name }}</strong>
            <div class="rt-chips">
              <span class="qc-chip tone-muted">{{ categoryLabel(selectedRule.category) }}</span>
              <span class="severity-chip" :class="severityClass(selectedRule.severity)">
                {{ SEVERITY_META[selectedRule.severity]?.label ?? selectedRule.severity }}
              </span>
              <span class="qc-chip tone-primary">
                {{ EXECUTOR_LABELS[selectedRule.executor_type] ?? selectedRule.executor_type }}
              </span>
              <span
                class="qc-chip"
                :class="selectedRule.status === 'enabled' ? 'tone-success' : 'tone-warning'"
              >
                {{ RULE_STATUS_LABELS[selectedRule.status] ?? selectedRule.status }}
              </span>
            </div>
          </div>
          <p v-if="selectedRule.check_point" class="rt-check-point">
            检查点：{{ selectedRule.check_point }}
          </p>
          <dl class="rt-facts">
            <dt>适用页面</dt>
            <dd>
              {{
                selectedRule.applicable_page_types.length
                  ? selectedRule.applicable_page_types.map(pageTypeName).join('、')
                  : '不限'
              }}
            </dd>
            <dt>适用端</dt>
            <dd>
              {{
                selectedRule.target_clients.length
                  ? selectedRule.target_clients.map((item) => CLIENT_LABELS[item] ?? item).join('、')
                  : '不限'
              }}
            </dd>
            <template v-if="selectedRule.required_page_states.length">
              <dt>只在这些页面状态下判</dt>
              <dd>{{ selectedRule.required_page_states.map(pageStateName).join('、') }}</dd>
            </template>
          </dl>
          <ul v-if="selectedRule.notes.length" class="rt-notes">
            <li v-for="note in selectedRule.notes" :key="note">{{ note }}</li>
          </ul>
          <p v-if="selectedRule.untestable_reason" class="ap-note is-warning">
            {{ selectedRule.untestable_reason }}
          </p>
          <p v-if="selectedRule.config_problem" class="ap-note is-warning">
            这条规则的判定配置有问题，正式任务里也跑不起来：{{ selectedRule.config_problem }}
          </p>
        </div>

        <h3 class="rt-step">2 填证据</h3>
        <p v-if="!selectedRule" class="rt-muted">
          先选规则，这里会按这条规则在正式任务里要的证据放出输入框。
        </p>
        <template v-else>
          <div v-if="showUrlCapture" class="rt-field">
            <span class="qc-label">页面网址（填好点「抓取页面」，自动把页面内容填进下面的输入框）</span>
            <div class="rt-url-row">
              <input
                v-model="form.pageUrl"
                class="qc-input"
                placeholder="https://…"
                data-testid="rule-test-page-url"
                @keydown.enter.prevent="handleCapturePage"
              />
              <button
                type="button"
                class="qc-btn"
                :disabled="fetching || !form.pageUrl.trim()"
                data-testid="rule-test-capture"
                @click="handleCapturePage"
              >
                {{ fetching ? '抓取中…' : '抓取页面' }}
              </button>
            </div>
          </div>

          <div v-if="showScreenshots" class="rt-field">
            <span class="qc-label">
              {{ screenshotLabel }}（{{ screenshots.length }}/{{ maxScreenshots }}<template
                v-if="selectedRule.min_screenshot_count > 0 && !beforeInput"
                >，这条规则至少要 {{ selectedRule.min_screenshot_count }} 张</template
              >，支持 PNG / JPG，可直接粘贴）
            </span>
            <div
              class="rt-drop"
              :class="{ 'is-dragging': dragging }"
              tabindex="0"
              data-testid="rule-test-drop"
              @click="fileInput?.click()"
              @keydown.enter.prevent="fileInput?.click()"
              @dragover.prevent="dragging = true"
              @dragleave="dragging = false"
              @drop.prevent="handleDrop"
              @paste="handlePaste"
            >
              点这里选图片，或把截图拖进来；也可以先点一下这里再按 Ctrl+V 粘贴
            </div>
            <input
              ref="fileInput"
              class="rt-hidden-input"
              type="file"
              accept="image/*"
              multiple
              data-testid="rule-test-file-input"
              @change="handleFileInput"
            />
            <n-image-group v-if="screenshots.length" :key="screenshotGroupKey">
              <ul class="rt-shots">
                <li v-for="(item, index) in screenshots" :key="item.id">
                  <n-image
                    :src="item.url"
                    :alt="item.file.name"
                    width="92"
                    height="120"
                    object-fit="cover"
                    :img-props="{ title: '点击放大' }"
                  />
                  <span>截图 {{ index + 1 }}</span>
                  <button type="button" class="qc-link" @click="removeScreenshot(item.id)">移除</button>
                </li>
              </ul>
            </n-image-group>
          </div>

          <div v-if="beforeInput" class="rt-field" data-testid="rule-test-before">
            <span class="qc-label">{{ beforeInput.label }}（1 张）</span>
            <p class="rt-muted rt-hint">{{ beforeInput.hint }}</p>
            <div
              class="rt-drop"
              tabindex="0"
              data-testid="rule-test-before-drop"
              @click="beforeFileInput?.click()"
              @keydown.enter.prevent="beforeFileInput?.click()"
              @dragover.prevent
              @drop.prevent="handleBeforeDrop"
              @paste="handleBeforePaste"
            >
              点这里选操作前的截图，或拖进来；也可以先点一下这里再按 Ctrl+V 粘贴
            </div>
            <input
              ref="beforeFileInput"
              class="rt-hidden-input"
              type="file"
              accept="image/*"
              data-testid="rule-test-before-input"
              @change="handleBeforeInput"
            />
            <n-image-group v-if="beforeShot" :key="beforeShot.id">
              <ul class="rt-before-shots">
                <li>
                  <n-image
                    :src="beforeShot.url"
                    :alt="beforeShot.file.name"
                    width="92"
                    height="120"
                    object-fit="cover"
                    :img-props="{ title: '点击放大' }"
                  />
                  <span>操作前</span>
                  <button type="button" class="qc-link" @click="setBeforeShot(null)">移除</button>
                </li>
              </ul>
            </n-image-group>
          </div>

          <label v-if="showVisibleText" class="rt-field">
            <span class="qc-label">
              {{ secondLocationInput ? '页面文字（第一个位置）' : '页面文字（页面上能看到的文字）' }}
            </span>
            <textarea
              v-model="form.visibleText"
              class="qc-textarea"
              rows="5"
              data-testid="rule-test-visible-text"
            />
          </label>

          <label
            v-if="secondLocationInput"
            class="rt-field"
            data-testid="rule-test-second-location-field"
          >
            <span class="qc-label">{{ secondLocationInput.label }}</span>
            <span class="rt-muted rt-hint">{{ secondLocationInput.hint }}</span>
            <textarea
              v-model="form.secondLocationText"
              class="qc-textarea"
              rows="4"
              data-testid="rule-test-second-location"
            />
          </label>

          <label v-if="showDom" class="rt-field">
            <span class="qc-label">页面源码（HTML）</span>
            <textarea
              v-model="form.domHtml"
              class="qc-textarea rt-mono"
              rows="5"
              data-testid="rule-test-dom"
            />
          </label>

          <label v-if="showUiSnapshot" class="rt-field">
            <span class="qc-label">界面快照（页面元素结构，JSON）</span>
            <textarea
              v-model="form.uiSnapshot"
              class="qc-textarea rt-mono"
              rows="5"
              placeholder='{"elements": [...]}'
              data-testid="rule-test-ui-snapshot"
            />
          </label>

          <label v-if="judgeInputFields.length" class="rt-field">
            <span class="qc-label">
              补充判定输入（JSON；正式任务里由采集程序自动提取：{{ judgeInputFields.join('、') }}）
            </span>
            <textarea
              v-model="form.judgeInput"
              class="qc-textarea rt-mono"
              rows="3"
              :placeholder="judgeInputPlaceholder"
              data-testid="rule-test-judge-input"
            />
          </label>

          <p v-if="unsupportedNeeds.length" class="rt-muted" data-testid="rule-test-unsupported">
            正式任务里这条规则还要：{{ unsupportedNeeds.join('、') }}。这些只能来自真实采集，这里提供不了；不影响测试，结果里会提示。
          </p>
        </template>

        <div class="rt-actions">
          <button
            type="button"
            class="qc-btn qc-btn-primary rt-run"
            :disabled="!canRun"
            data-testid="rule-test-run"
            @click="handleRun"
          >
            {{ running ? '检查中…（一般 10～60 秒）' : '开始检查' }}
          </button>
        </div>
      </section>

      <section class="workstation-glass-panel rt-panel rt-output">
        <h3 class="rt-step">检查结果</h3>
        <p v-if="running" class="rt-empty">正在按正式任务的判定逻辑检查，请稍候…</p>
        <RuleTestResultCard
          v-else-if="activeHistory"
          :result="activeHistory.result"
          :ran-at="activeHistory.ranAt"
        />
        <p v-else class="rt-empty">选好规则、填好证据，点「开始检查」</p>

        <template v-if="history.length > 1">
          <h4 class="rt-history-title">这次打开页面后测过的（全部记录在「测试记录」里）</h4>
          <ul class="rt-history" data-testid="rule-test-history">
            <li v-for="item in history" :key="item.id">
              <button
                type="button"
                :class="{ 'is-active': activeHistory?.id === item.id }"
                @click="activeHistoryId = item.id"
              >
                <span>{{ item.ranAt }}</span>
                <span class="rt-history-rule">{{ item.result.rule_code }}</span>
                <span class="qc-chip" :class="`tone-${ruleTestOutcomeTone(item.result.outcome)}`">
                  {{ ruleTestOutcomeLabel(item.result.outcome) }}
                </span>
              </button>
            </li>
          </ul>
        </template>
      </section>
    </div>
  </div>
</template>

<style scoped>
.rt-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.rt-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}

.rt-panel {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 20px;
  min-width: 0;
}

.rt-output {
  position: sticky;
  top: 16px;
}

.rt-step {
  margin: 4px 0 0;
  color: var(--workstation-text-strong);
  font-size: 15px;
}

.rt-rule-card {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px 16px;
  border: 1px solid var(--workstation-border);
  border-radius: var(--workstation-radius-sm);
  background: var(--workstation-panel-soft);
}

.rt-rule-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.rt-rule-head strong {
  color: var(--workstation-text-strong);
  font-size: 14px;
}

.rt-check-point {
  margin: 0;
  color: var(--workstation-text);
  font-size: 13px;
  line-height: 1.7;
  overflow-wrap: anywhere;
}

.rt-facts {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 6px 12px;
  margin: 0;
  font-size: 13px;
}

.rt-facts dt {
  color: var(--workstation-text-muted);
  white-space: nowrap;
}

.rt-facts dd {
  margin: 0;
  color: var(--workstation-text);
}

.rt-notes {
  margin: 0;
  padding-left: 18px;
  color: var(--workstation-text);
  font-size: 13px;
  line-height: 1.6;
}

.rt-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.rt-field {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.rt-url-row {
  display: flex;
  gap: 8px;
}

.rt-url-row .qc-input {
  flex: 1;
  min-width: 0;
}

.rt-drop {
  padding: 18px 12px;
  border: 1px dashed var(--workstation-border-strong);
  border-radius: var(--workstation-radius-sm);
  color: var(--workstation-text-muted);
  background: var(--workstation-panel-soft);
  font-size: 13px;
  text-align: center;
  cursor: pointer;
  outline: 0;
}

.rt-drop:focus,
.rt-drop.is-dragging {
  border-color: var(--workstation-primary);
  color: var(--workstation-primary);
  background: var(--tint-primary);
}

.rt-hidden-input {
  display: none;
}

.rt-shots,
.rt-before-shots {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin: 10px 0 0;
  padding: 0;
  list-style: none;
}

.rt-shots li,
.rt-before-shots li {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  width: 92px;
  color: var(--workstation-text-muted);
  font-size: 12px;
}

.rt-shots :deep(img),
.rt-before-shots :deep(img) {
  width: 92px;
  height: 120px;
  object-fit: cover;
  border: 1px solid var(--workstation-border);
  border-radius: 6px;
  background: var(--workstation-panel);
  cursor: zoom-in;
}

.rt-shots .qc-link,
.rt-before-shots .qc-link {
  font-size: 12px;
}

.rt-hint {
  margin-bottom: 6px;
}

.rt-mono {
  font-family: 'JetBrains Mono', 'Fira Code', Consolas, monospace;
  font-size: 12px;
}

.rt-actions {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-top: 12px;
  border-top: 1px solid var(--workstation-border);
}

.rt-muted {
  margin: 0;
  color: var(--workstation-text-muted);
  font-size: 12px;
  line-height: 1.6;
}

.rt-run {
  align-self: flex-start;
  padding: 10px 28px;
}

.rt-empty {
  margin: 0;
  padding: 36px 12px;
  color: var(--workstation-text-muted);
  font-size: 13px;
  text-align: center;
}

.rt-history-title {
  margin: 8px 0 0;
  color: var(--workstation-text-muted);
  font-size: 12px;
  font-weight: 600;
}

.rt-history {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.rt-history button {
  display: grid;
  grid-template-columns: 72px minmax(0, 1fr) auto;
  gap: 10px;
  width: 100%;
  padding: 7px 10px;
  border: 1px solid transparent;
  border-radius: 6px;
  color: var(--workstation-text);
  background: transparent;
  font: inherit;
  font-size: 13px;
  text-align: left;
  cursor: pointer;
}

.rt-history button:hover {
  background: var(--workstation-panel-soft);
}

.rt-history button.is-active {
  border-color: var(--workstation-border-strong);
  background: var(--tint-primary);
}

.rt-history-rule {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (max-width: 1280px) {
  .rt-grid {
    grid-template-columns: 1fr;
  }

  .rt-output {
    position: static;
  }
}

</style>
