<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { usePortalNav } from '@/composables/usePortalNav'
import { NIcon, useMessage } from 'naive-ui'
import {
  CloudUploadOutline,
  PhonePortraitOutline,
  PlayCircleOutline,
  ShieldCheckmarkOutline,
} from '@vicons/ionicons5'
import { uploadAppBuild, type AppBuildUploadResponse } from '@/services/app-builds.api'
import { submitAppTask } from '@/services/tasks.api'
import { AliothHttpError } from '@/services/http'

const nav = usePortalNav()
const message = useMessage()

const formValue = reactive({
  task_name: '',
  package_name: '',
  device_backend: 'cloud_phone',
  device_profile_id: 'default',
  remark: '',
})

const selectedFile = ref<File | null>(null)
const build = ref<AppBuildUploadResponse | null>(null)
const uploading = ref(false)
const submitting = ref(false)
const fileInputRef = ref<HTMLInputElement | null>(null)

const canSubmit = computed(
  () => Boolean(formValue.task_name.trim() && build.value?.platform_build_ref && !uploading.value),
)

const buildSummary = computed(() => {
  const item = build.value
  if (!item) {
    return null
  }
  const sizeMb = (item.size_bytes / (1024 * 1024)).toFixed(2)
  return {
    filename: item.original_filename,
    ref: item.platform_build_ref,
    sha256: item.sha256,
    sizeLabel: `${sizeMb} MB`,
    packageName: item.package_name || formValue.package_name || '未解析',
    versionLabel: [item.version_name, item.version_code != null ? `(${item.version_code})` : '']
      .filter(Boolean)
      .join(' ') || '—',
  }
})

function openFilePicker() {
  fileInputRef.value?.click()
}

function onFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0] ?? null
  selectedFile.value = file
  build.value = null
  if (file) {
    void handleUpload(file)
  }
}

async function handleUpload(file: File) {
  if (!file.name.toLowerCase().endsWith('.apk')) {
    message.error('请上传 .apk 文件')
    selectedFile.value = null
    return
  }
  uploading.value = true
  try {
    const result = await uploadAppBuild(file)
    build.value = result
    if (result.package_name && !formValue.package_name.trim()) {
      formValue.package_name = result.package_name
    }
    message.success('APK 上传并校验成功')
  } catch (error) {
    build.value = null
    message.error(error instanceof Error ? error.message : 'APK 上传失败')
  } finally {
    uploading.value = false
  }
}

function cancelCreate() {
  void nav.push('/tasks')
}

async function handleSubmit() {
  if (!formValue.task_name.trim()) {
    message.error('请填写任务名称')
    return
  }
  if (!build.value?.platform_build_ref) {
    message.error('请先上传并通过校验的 APK')
    return
  }

  submitting.value = true
  try {
    const result = await submitAppTask({
      task_name: formValue.task_name.trim(),
      platform_build_ref: build.value.platform_build_ref,
      package_name: formValue.package_name.trim() || null,
      device_backend: formValue.device_backend.trim() || 'cloud_phone',
      device_profile_id: formValue.device_profile_id.trim() || 'default',
      remark: formValue.remark.trim() || null,
    })
    message.success(result.message || 'App 质检任务已提交')
    await nav.push(`/tasks/${result.task_id}`)
  } catch (error) {
    if (error instanceof AliothHttpError && error.status === 409) {
      message.error(error.message || 'App 质检未启用或工作流版本不正确')
    } else {
      message.error(error instanceof Error ? error.message : '创建 App 质检任务失败')
    }
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <section class="create-task-shell">
    <div class="create-title-block">
      <div class="title-row">
        <h1>正式 App 质检</h1>
        <span>新建</span>
      </div>
      <p>上传 Android APK 并提交 App 质检任务。任务将写入质检事实链，经 Capture、Inspection 与 Completion Gate 收口。</p>
    </div>

    <div class="create-layout">
      <section class="config-panel">
        <div class="panel-title">
          <i></i>
          <h2>任务配置</h2>
        </div>

        <div class="form-stack">
          <label class="field-block">
            <span>任务名称 <b>*</b></span>
            <input
              v-model="formValue.task_name"
              class="form-input"
              type="text"
              placeholder="例如：示例 App 首屏质检"
            />
          </label>

          <div class="field-block">
            <span>APK 构建制品 <b>*</b></span>
            <input
              ref="fileInputRef"
              class="file-input-hidden"
              type="file"
              accept=".apk,application/vnd.android.package-archive"
              @change="onFileChange"
            />
            <button class="upload-button" type="button" :disabled="uploading" @click="openFilePicker">
              <n-icon size="18"><CloudUploadOutline /></n-icon>
              {{ uploading ? '上传校验中…' : selectedFile ? '重新选择 APK' : '选择并上传 APK' }}
            </button>
            <small class="field-hint">
              {{ selectedFile ? `已选：${selectedFile.name}` : '仅支持 Android APK；上传后由服务端校验 SHA-256' }}
            </small>
          </div>

          <label class="field-block">
            <span>包名（可选）</span>
            <input
              v-model="formValue.package_name"
              class="form-input"
              type="text"
              placeholder="可留空；上传成功后可能自动填入"
            />
          </label>

          <div class="field-grid">
            <label class="field-block">
              <span>设备后端</span>
              <input v-model="formValue.device_backend" class="form-input" type="text" />
            </label>
            <label class="field-block">
              <span>设备 Profile</span>
              <input v-model="formValue.device_profile_id" class="form-input" type="text" />
            </label>
          </div>

          <label class="field-block">
            <span>备注</span>
            <textarea
              v-model="formValue.remark"
              class="form-input"
              maxlength="500"
              rows="4"
              placeholder="补充 App 版本、关注场景；若需跳过同名降版扫描，注明 SAME_LABEL_SCAN_SKIP_AUTHORIZED 与授权范围"
            />
            <small>{{ formValue.remark.length }}/500</small>
          </label>
        </div>

        <div class="form-actions form-actions--end">
          <div>
            <button class="cancel-button" type="button" @click="cancelCreate">取消</button>
            <button
              class="submit-button"
              type="button"
              :disabled="submitting || !canSubmit"
              @click="handleSubmit"
            >
              <n-icon size="17"><PlayCircleOutline /></n-icon>
              {{ submitting ? '提交中…' : '创建 App 质检任务' }}
            </button>
          </div>
        </div>
      </section>

      <aside class="preview-column">
        <div class="sticky-stack">
          <section class="preview-panel">
            <div class="preview-heading">
              <div>
                <i></i>
                <h2>制品与目标预览</h2>
              </div>
              <span>{{ build ? '已就绪' : '待上传' }}</span>
            </div>

            <div class="preview-card hero-preview">
              <div class="preview-icon">
                <n-icon size="24"><PhonePortraitOutline /></n-icon>
              </div>
              <div>
                <h3>{{ formValue.task_name || '未命名 App 任务' }}</h3>
                <p>正式 App Workflow · workflow_version=v2</p>
              </div>
            </div>

            <div v-if="buildSummary" class="build-facts">
              <div class="fact-row">
                <span>文件</span>
                <strong>{{ buildSummary.filename }}</strong>
              </div>
              <div class="fact-row">
                <span>制品引用</span>
                <strong class="mono">{{ buildSummary.ref }}</strong>
              </div>
              <div class="fact-row">
                <span>包名</span>
                <strong>{{ buildSummary.packageName }}</strong>
              </div>
              <div class="fact-row">
                <span>版本</span>
                <strong>{{ buildSummary.versionLabel }}</strong>
              </div>
              <div class="fact-row">
                <span>大小</span>
                <strong>{{ buildSummary.sizeLabel }}</strong>
              </div>
              <div class="fact-row">
                <span>SHA-256</span>
                <strong class="mono sha">{{ buildSummary.sha256 }}</strong>
              </div>
            </div>
            <p v-else class="scope-empty">上传 APK 后将展示校验结果</p>
          </section>

          <section class="notice-card muted">
            <n-icon size="20"><ShieldCheckmarkOutline /></n-icon>
            <p>创建后进入既有任务详情查看进度；H5「创建质检任务」入口保持不变。</p>
          </section>
        </div>
      </aside>
    </div>
  </section>
</template>

<style scoped>
.create-task-shell {
  color: #e5e7eb;
}

.create-title-block {
  margin-bottom: 24px;
}

.title-row {
  display: flex;
  align-items: center;
  gap: 12px;
}

.title-row h1 {
  margin: 0;
  font-size: 28px;
  font-weight: 700;
  color: #f8fafc;
}

.title-row span {
  padding: 2px 10px;
  border-radius: 999px;
  background: rgba(10, 132, 255, 0.16);
  color: #7dd3fc;
  font-size: 12px;
}

.create-title-block p {
  margin: 10px 0 0;
  max-width: 760px;
  color: #94a3b8;
  line-height: 1.6;
}

.create-layout {
  display: grid;
  grid-template-columns: minmax(0, 1.35fr) minmax(280px, 0.9fr);
  gap: 20px;
  align-items: start;
}

.config-panel,
.preview-panel,
.notice-card {
  border: 1px solid rgba(148, 163, 184, 0.14);
  border-radius: 16px;
  background: rgba(15, 23, 42, 0.72);
  backdrop-filter: blur(10px);
}

.config-panel,
.preview-panel {
  padding: 22px;
}

.panel-title,
.preview-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 18px;
}

.panel-title,
.preview-heading > div {
  display: flex;
  align-items: center;
  gap: 10px;
}

.panel-title i,
.preview-heading i {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #0a84ff;
  box-shadow: 0 0 0 4px rgba(10, 132, 255, 0.18);
}

.panel-title h2,
.preview-heading h2 {
  margin: 0;
  font-size: 16px;
}

.form-stack {
  display: grid;
  gap: 18px;
}

.field-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
}

.field-block {
  display: grid;
  gap: 8px;
}

.field-block > span {
  color: #cbd5e1;
  font-size: 13px;
}

.field-block b {
  color: #f87171;
  font-weight: 600;
}

.form-input,
.upload-button {
  width: 100%;
  border-radius: 10px;
  border: 1px solid #2e3e5c;
  background: #1a2235;
  color: #fff;
}

.form-input {
  padding: 12px 14px;
  outline: none;
}

.form-input:focus {
  border-color: #0a84ff;
  box-shadow: 0 0 0 3px rgba(10, 132, 255, 0.15);
}

textarea.form-input {
  resize: vertical;
  min-height: 96px;
}

.field-hint,
.field-block small {
  color: #64748b;
  font-size: 12px;
}

.file-input-hidden {
  display: none;
}

.upload-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 12px 14px;
  cursor: pointer;
}

.upload-button:disabled {
  opacity: 0.65;
  cursor: not-allowed;
}

.form-actions {
  margin-top: 24px;
  padding-top: 18px;
  border-top: 1px solid rgba(148, 163, 184, 0.12);
}

.form-actions--end > div {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}

.cancel-button,
.submit-button {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 42px;
  padding: 0 16px;
  border-radius: 10px;
  border: 1px solid transparent;
  cursor: pointer;
}

.cancel-button {
  background: transparent;
  border-color: #334155;
  color: #cbd5e1;
}

.submit-button {
  background: linear-gradient(135deg, #0a84ff, #0066cc);
  color: #fff;
}

.submit-button:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.sticky-stack {
  display: grid;
  gap: 14px;
  position: sticky;
  top: 96px;
}

.preview-card {
  display: flex;
  gap: 14px;
  align-items: center;
  padding: 14px;
  border-radius: 12px;
  background: rgba(30, 41, 59, 0.7);
}

.preview-icon {
  display: grid;
  place-items: center;
  width: 48px;
  height: 48px;
  border-radius: 12px;
  background: rgba(10, 132, 255, 0.16);
  color: #7dd3fc;
}

.preview-card h3 {
  margin: 0 0 4px;
  font-size: 15px;
}

.preview-card p,
.scope-empty {
  margin: 0;
  color: #94a3b8;
  font-size: 13px;
}

.build-facts {
  display: grid;
  gap: 10px;
  margin-top: 14px;
}

.fact-row {
  display: grid;
  grid-template-columns: 72px minmax(0, 1fr);
  gap: 10px;
  font-size: 13px;
}

.fact-row span {
  color: #64748b;
}

.fact-row strong {
  color: #e2e8f0;
  font-weight: 500;
  overflow-wrap: anywhere;
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
}

.sha {
  word-break: break-all;
}

.notice-card {
  display: flex;
  gap: 12px;
  padding: 14px 16px;
  color: #cbd5e1;
  line-height: 1.55;
  font-size: 13px;
}

.notice-card.muted {
  color: #94a3b8;
}

.notice-card p {
  margin: 0;
}

.notice-card code {
  padding: 1px 5px;
  border-radius: 4px;
  background: rgba(148, 163, 184, 0.12);
  font-size: 12px;
}

@media (max-width: 980px) {
  .create-layout,
  .field-grid {
    grid-template-columns: 1fr;
  }

  .sticky-stack {
    position: static;
  }
}
</style>
