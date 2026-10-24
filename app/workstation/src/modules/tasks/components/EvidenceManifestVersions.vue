<script setup lang="ts">
import type { EvidenceManifestReview, H5ShortflowArtifact } from '@/services/h5-shortflows.api'
defineProps<{ versions: EvidenceManifestReview[] }>()
const emit = defineEmits<{ view: [version: EvidenceManifestReview, artifact: H5ShortflowArtifact] }>()
const quality: Record<string, string> = { pass: '材料质量合格', pending: '材料质量待定', fail: '材料质量不合格' }
</script>

<template>
  <details v-if="versions.length" data-testid="manifest-history">
    <summary>Manifest 版本审阅（{{ versions.length }} 版）</summary>
    <p>全部版本仅供审阅。选择历史版本不会改变当前材料、判定输入或导出快照。</p>
    <article v-for="version in versions" :key="version.manifest_id">
      <p>v{{ version.manifest_version }} · {{ version.is_latest ? '最新版本' : '历史版本' }}{{ version.is_bound ? ' · 动作绑定版本' : '' }} · {{ quality[version.quality_status] || '质量未知' }}</p>
      <p>{{ version.created_at }} · {{ version.manifest_id }} · 哈希 {{ version.manifest_hash }}</p>
      <p v-if="version.quality_reason">{{ version.quality_reason }}</p>
      <p v-if="version.integrity !== 'verified'">原件与此版本清单不一致，无法开放材料审阅。</p>
      <button v-for="artifact in version.artifacts" :key="artifact.artifact_id" type="button"
        class="qc-btn qc-btn--secondary" :disabled="version.integrity !== 'verified'"
        @click="emit('view', version, artifact)">查看{{ version.is_latest ? '此版' : '历史' }}{{ artifact.artifact_type === 'screenshot' ? '截图' : '材料' }} {{ artifact.artifact_id }}</button>
    </article>
  </details>
</template>

<style scoped>
details { margin: 12px 0; overflow-wrap: anywhere; }
article { border-left: 2px solid var(--qc-border, #d9e1e8); padding-left: 10px; }
button { max-width: 100%; white-space: normal; overflow-wrap: anywhere; }
</style>
