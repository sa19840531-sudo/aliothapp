<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { NDrawer, NDrawerContent, useMessage } from 'naive-ui'
import {
  createBusinessRuleset,
  listRulesets,
  patchRuleset,
  type RulesetSummary,
} from '../../api/rules'
import SearchSelect from '@/components/pickers/SearchSelect.vue'

/**
 * 规则配置 · 规则集（2.1 原型）：卡片展示名称、编码版本、状态、说明与规则数；
 * 新增 / 编辑只填名称、状态、说明，编码、版本、页面抽样与防死循环策略由系统生成或原样保留。
 */
const props = defineProps<{ canWrite: boolean }>()
const emit = defineEmits<{ viewRules: [rulesetId: string] }>()

const STATUS_LABELS: Record<RulesetSummary['status'], string> = {
  draft: '草稿',
  enabled: '启用',
  disabled: '停用',
  archived: '归档',
}
const STATUS_TONES: Record<RulesetSummary['status'], string> = {
  draft: 'tone-muted',
  enabled: 'tone-success',
  disabled: 'tone-warning',
  archived: 'tone-muted',
}

const message = useMessage()
const rulesets = ref<RulesetSummary[]>([])
const loading = ref(false)
const drawerOpen = ref(false)
const saving = ref(false)
const editing = ref<RulesetSummary | null>(null)
const form = reactive({ name: '', description: '', status: 'draft' as RulesetSummary['status'] })

onMounted(() => void load())

async function load() {
  loading.value = true
  try {
    rulesets.value = (await listRulesets({ page: 1, page_size: 100 })).items
  } catch (error) {
    message.error(error instanceof Error ? error.message : '规则集加载失败')
  } finally {
    loading.value = false
  }
}

function ruleCountText(item: RulesetSummary) {
  const totalCount = item.total_rule_count ?? item.rule_count
  const enabledCount = item.enabled_rule_count
  return enabledCount === null || enabledCount === undefined
    ? `包含 ${totalCount} 条规则`
    : `包含 ${totalCount} 条规则，已开启 ${enabledCount} 条`
}

function openCreate() {
  editing.value = null
  form.name = ''
  form.description = ''
  form.status = 'draft'
  drawerOpen.value = true
}

function openEdit(item: RulesetSummary) {
  editing.value = item
  form.name = item.name
  form.description = item.description ?? ''
  form.status = item.status
  drawerOpen.value = true
}

async function save() {
  if (!form.name.trim() || !form.description.trim()) {
    message.error('规则集名称和说明都要填')
    return
  }
  const payload = { name: form.name.trim(), description: form.description.trim(), status: form.status }
  saving.value = true
  try {
    if (editing.value) {
      await patchRuleset(editing.value.id, payload)
      message.success('规则集已保存')
    } else {
      await createBusinessRuleset(payload)
      message.success('规则集已新增')
    }
    drawerOpen.value = false
    await load()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '保存失败')
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <section class="ruleset-panel" data-testid="ruleset-panel">
    <div class="ruleset-head">
      <p>一个规则集打包多条规则，开启的规则集才会出现在创建任务和快速质检的选择范围里。</p>
      <button
        v-if="props.canWrite"
        type="button"
        class="qc-btn qc-btn-primary"
        data-testid="ruleset-create"
        @click="openCreate"
      >
        新增规则集
      </button>
    </div>

    <p v-if="loading" class="ruleset-empty">规则集加载中…</p>
    <p v-else-if="rulesets.length === 0" class="ruleset-empty">还没有规则集</p>
    <div v-else class="ruleset-grid">
      <article v-for="item in rulesets" :key="item.id" class="workstation-glass-panel ruleset-card">
        <header>
          <div>
            <button type="button" class="ruleset-name qc-link" :data-testid="`ruleset-open-${item.id}`" @click="emit('viewRules', item.id)">{{ item.name }}</button>
            <small>{{ item.ruleset_code }} · v{{ item.version }}</small>
          </div>
          <span class="qc-chip" :class="STATUS_TONES[item.status]">{{ STATUS_LABELS[item.status] }}</span>
        </header>
        <p class="ruleset-desc">{{ item.description || '暂无说明' }}</p>
        <footer>
          <span>{{ ruleCountText(item) }}</span>
          <div class="ruleset-card-actions">
            <button type="button" class="qc-link" @click="emit('viewRules', item.id)">查看规则</button>
            <button v-if="props.canWrite" type="button" class="qc-link" @click="openEdit(item)">编辑</button>
          </div>
        </footer>
      </article>
    </div>

    <n-drawer v-model:show="drawerOpen" width="480" placement="right">
      <n-drawer-content :title="editing ? '编辑规则集' : '新增规则集'" closable>
        <div class="ruleset-form" data-testid="ruleset-form">
          <label class="ruleset-field">
            <span class="qc-label">规则集名称</span>
            <input v-model="form.name" class="qc-input" data-testid="ruleset-form-name" />
          </label>
          <label class="ruleset-field">
            <span class="qc-label">状态</span>
            <SearchSelect v-model="form.status" data-testid="ruleset-form-status">
              <option v-for="(label, value) in STATUS_LABELS" :key="value" :value="value">{{ label }}</option>
            </SearchSelect>
          </label>
          <label class="ruleset-field">
            <span class="qc-label">说明</span>
            <textarea v-model="form.description" class="qc-textarea" rows="4" data-testid="ruleset-form-description" />
          </label>
          <p class="ruleset-note">
            {{
              editing
                ? `编码 ${editing.ruleset_code} · v${editing.version}；页面抽样和防死循环策略保持不变。`
                : '编码、版本和页面抽样、防死循环策略由系统生成（沿用当前启用规则集的策略）。'
            }}
            规则集里有哪些规则，在「规则明细」里按规则集新增；调整已有规则的归属暂不支持。
          </p>
          <div class="ruleset-actions">
            <button type="button" class="qc-btn" @click="drawerOpen = false">取消</button>
            <button
              type="button"
              class="qc-btn qc-btn-primary"
              :disabled="saving"
              data-testid="ruleset-form-save"
              @click="save"
            >
              {{ saving ? '保存中…' : '保存' }}
            </button>
          </div>
        </div>
      </n-drawer-content>
    </n-drawer>
  </section>
</template>

<style scoped>
.ruleset-panel {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.ruleset-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.ruleset-head p {
  margin: 0;
  color: var(--workstation-text-muted);
  font-size: 13px;
}

.ruleset-empty {
  margin: 0;
  padding: 32px 0;
  color: var(--workstation-text-muted);
  text-align: center;
}

.ruleset-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}

.ruleset-card {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 16px 18px;
}

.ruleset-card header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.ruleset-name {
  display: block;
  color: var(--workstation-text-strong);
  font-size: 15px;
}

.ruleset-card-actions {
  display: flex;
  gap: 14px;
}

.ruleset-card header small {
  color: var(--workstation-text-faint);
  font-size: 12px;
}

.ruleset-desc {
  margin: 0;
  color: var(--workstation-text);
  font-size: 13px;
  line-height: 1.7;
}

.ruleset-card footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: var(--workstation-text-muted);
  font-size: 13px;
}

.ruleset-form {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.ruleset-field {
  display: flex;
  flex-direction: column;
}

.ruleset-note {
  margin: 0;
  color: var(--workstation-text-muted);
  font-size: 12px;
  line-height: 1.7;
}

.ruleset-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}

@media (max-width: 1000px) {
  .ruleset-grid {
    grid-template-columns: 1fr;
  }
}
</style>
