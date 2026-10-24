<script setup lang="ts">
import type { WebReportExplanation } from '@/types/web-report-explanation'

defineProps<{
  explanation?: WebReportExplanation | null
  identityDiagnostics?: Array<{ capture_id: string; reason_code: string }>
  /** 说明所在的数据还没回来：显示「加载中…」，不说「未提供」 */
  loading?: boolean
}>()
</script>

<template>
  <section class="web-explanation" aria-label="Web 证据与判定说明">
    <h3>Web 证据与判定说明</h3>
    <p v-if="!explanation && loading">加载中…</p>
    <p v-else-if="!explanation">历史报告或当前材料未提供此版本的解释，来源不可追溯。</p>
    <template v-else>
      <p>Run {{ explanation.agent_run_id }} · Epoch {{ explanation.finalization_epoch }}</p>
      <div v-if="explanation.page_type_summary" class="evidence-group">
        <h4>页面类型与覆盖口径</h4>
        <p>业务页：{{ explanation.page_type_summary.business_page_types.join('、') || '未观察到' }}</p>
        <p>生命周期页：{{ explanation.page_type_summary.lifecycle_page_types.join('、') || '未观察到' }}</p>
        <p>Unknown：{{ explanation.page_type_summary.unknown_pages.length }} · 广告页：{{ explanation.page_type_summary.ad_page_count }}</p>
      </div>
      <div v-if="explanation.web_logical_pages?.length" class="evidence-group">
        <h4>业务与生命周期页面证据</h4>
        <article v-for="page in explanation.web_logical_pages" :key="page.logical_page_id">
          <p>{{ page.page_type || 'unknown' }} · 主 Capture {{ page.primary_capture_id }} · {{ page.classification_status || '待识别' }}</p>
          <p v-if="page.supplemental_full_page">
            全页长图 {{ page.supplemental_full_page.status }}
            <template v-if="page.supplemental_full_page.capture_id"> · Capture {{ page.supplemental_full_page.capture_id }}</template>
            <template v-if="page.supplemental_full_page.screenshot_artifact_id"> · Screenshot {{ page.supplemental_full_page.screenshot_artifact_id }}</template>
            <template v-if="page.supplemental_full_page.reason_code"> · {{ page.supplemental_full_page.reason_code }}</template>
          </p>
        </article>
      </div>
      <div v-if="explanation.web_ad_pages?.length" class="evidence-group">
        <h4>广告页面证据</h4>
        <article v-for="page in explanation.web_ad_pages" :key="page.capture_id">
          <p>ad_page · Capture {{ page.capture_id }} · 父 Capture {{ page.parent_capture_id || '无' }}</p>
          <p>请求 {{ page.requested_url || '未记录' }} · 最终 {{ page.final_url || '未记录' }}</p>
          <p v-if="page.screenshot_artifact_id">Screenshot {{ page.screenshot_artifact_id }}</p>
        </article>
      </div>
      <div v-for="item in explanation.notifications" :key="item.capture_id">
        <p v-if="item.notification">
          {{ item.capture_id }} ·
          {{ item.classification_status === 'recognized' && item.notification.status === 'blocked' ? '已分类但未发布质检' : item.notification.status }}
          · {{ item.notification.reason_code }}
        </p>
      </div>
      <div v-if="explanation.primary_content?.length">
        <h4>主体补图与回退</h4>
        <p>失败保留原始基线；动作已接受不等于补图成功，不把失败帧作为稳定主截图。</p>
        <article v-for="(item, index) in explanation.primary_content" :key="`primary-${index}`">
          <p>{{ item.menu_entry_id }} · {{ item.status }} · {{ item.reason_code }} · 原始基线 {{ item.parent_capture_id }}</p>
          <p v-for="(attempt, attemptIndex) in item.attempts" :key="attemptIndex">阶段 {{ attempt.stage }} · 直接来源 {{ attempt.source_capture_id }} · 动作前 {{ attempt.before_capture_id }} · 动作后 {{ attempt.after_capture_id }} · Action {{ attempt.action_execution_id }} · {{ attempt.error_code }}</p>
        </article>
      </div>
      <div v-if="explanation.sequences?.length">
        <h4>失败序列材料</h4>
        <p>有可判定序列不代表页面稳定或覆盖完成。</p>
        <article v-for="item in explanation.sequences" :key="item.sequence_id">
          <p>{{ item.sequence_id }} · {{ item.purpose }} · {{ item.reason_code }}</p>
          <p v-if="item.readiness_status || item.readiness_exit_reason">稳定性 {{ item.readiness_status || 'unknown' }} · 出口 {{ item.readiness_exit_reason || 'unknown' }}</p>
          <p v-for="frame in item.frames" :key="frame.capture_id">{{ frame.capture_role }} · {{ frame.capture_id }} · {{ frame.captured_at }}<span v-if="frame.page_state"> · 帧状态 {{ frame.page_state }}</span></p>
          <p v-if="item.capability_revision">
            局部结构 {{ item.capability_revision.scope }} · {{ item.capability_revision.predicate }} · {{ item.capability_revision.status }}；
            账号 {{ item.capability_revision.account_state || 'unknown' }}。仅当前文档可枚举结构，产品级能力仍为未知。
          </p>
        </article>
      </div>
      <p v-for="(item, index) in explanation.publication_rules" :key="`selection-${index}`">
        {{ item.rule_code }} · 规则选择 {{ item.selection?.status || '未知' }} · {{ item.selection?.reason_code }}
      </p>
      <article v-for="(item, index) in explanation.rule_materials" :key="`material-${index}`">
        <p>{{ item.rule_code }} · {{ item.material_role === 'sequence' ? '序列材料' : '稳定材料' }} · 逐条写入 {{ item.status }} · 合并结论 {{ item.canonical_status || '未判定' }}</p>
        <p>Work {{ item.work_item_id }} · 批次 {{ item.work_status }} · {{ item.error_code }} · InputSet {{ item.evidence_input_set_id || '历史无引用' }} · 合同 {{ item.contract_hash }}</p>
        <p v-for="attempt in item.attempts" :key="attempt.attempt_id">原判定 {{ attempt.execution_status }} · Attempt {{ attempt.attempt_id }} · 技术状态 {{ attempt.status }} · Packet {{ attempt.observation_packet_id }}</p>
        <p v-for="source in item.canonical?.sources" :key="`${source.work_item_id}-${source.attempt_id}`">合并来源 {{ source.material_role }} · {{ source.execution_status }} · Work {{ source.work_item_id }} · Attempt {{ source.attempt_id }} · InputSet {{ source.evidence_input_set_id }} · 合同 {{ source.contract_hash }}</p>
      </article>
      <p>技术错误不等于产品不通过；正式绑定和系统待确认清零不表示本轮人工逐页复核完成。跨页面静态素材复用尚未启用。</p>
    </template>
    <p v-for="item in identityDiagnostics" :key="item.capture_id">未形成可信逻辑页：{{ item.capture_id }} · {{ item.reason_code }}</p>
  </section>
</template>

<style scoped>
.web-explanation { margin: 16px 0; padding: 16px; border: 1px solid var(--border-color, #d8dee6); border-radius: 8px; overflow-wrap: anywhere; }
.web-explanation p { margin: 8px 0; line-height: 1.6; }
.web-explanation article { padding: 8px 0; border-top: 1px solid var(--border-color, #d8dee6); }
.evidence-group { margin-top: 14px; }
.evidence-group h4 { margin: 0 0 8px; }
</style>
