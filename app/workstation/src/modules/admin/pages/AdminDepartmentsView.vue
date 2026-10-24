<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useMessage } from 'naive-ui'
import SearchSelect from '@/components/pickers/SearchSelect.vue'
import { authStore } from '@/stores/auth'
import { createDepartment, deleteDepartment, listDepartments, updateDepartment, type DepartmentItem } from '../api/departments'
import { listTenants, type TenantItem } from '../api/tenants'

/**
 * 部门管理（对齐 2.1 原型 org.html?tab=department）：筛选（公司 / 部门名称）→ 部门表 → 新增/编辑弹层。
 * 组织层级是 分公司 → 部门 → 产品；停用部门前服务端会校验部门下是否还有启用产品或账号。
 */
const message = useMessage()

const permissions = computed(() => new Set(authStore.user.value?.permissions ?? []))
const canManageAllCompanies = computed(() => permissions.value.has('users:any'))
const ownTenantId = computed(() => authStore.user.value?.tenant_id ?? '')
const ownTenantName = computed(() => authStore.user.value?.tenant_name || '本公司')

const tenants = ref<TenantItem[]>([])
/** 公司选项还在加载：编辑弹窗的「所属公司」下拉先显示「加载中…」 */
const tenantsLoading = ref(false)
const items = ref<DepartmentItem[]>([])
const loading = ref(true)
const loadError = ref('')

const filters = reactive({ tenant_id: '', keyword: '', status: '' as '' | 'enabled' | 'disabled' })

async function load() {
  loading.value = true
  try {
    items.value = await listDepartments({
      tenant_id: canManageAllCompanies.value ? filters.tenant_id || undefined : undefined,
      keyword: filters.keyword,
      status: filters.status,
    })
    loadError.value = ''
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '部门列表加载失败'
  } finally {
    loading.value = false
  }
}

async function loadTenants() {
  if (!canManageAllCompanies.value || !permissions.value.has('tenants')) return
  tenantsLoading.value = true
  try {
    tenants.value = (await listTenants()).filter((item) => item.status === 'enabled')
  } catch {
    tenants.value = []
  } finally {
    tenantsLoading.value = false
  }
}

function resetFilters() {
  filters.tenant_id = ''
  filters.keyword = ''
  filters.status = ''
  void load()
}

function formatMoney(value: number | null) {
  if (value === null || value === undefined) return '未设置'
  return `¥${value.toLocaleString('zh-CN', { minimumFractionDigits: 0, maximumFractionDigits: 2 })}`
}

function formatDate(value: string) {
  return new Date(value).toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' })
}

// ---------- 新增 / 编辑 ----------

const editorOpen = ref(false)
const editingId = ref<string | null>(null)
const saving = ref(false)
const form = reactive({
  tenant_id: '',
  name: '',
  owner_name: '',
  monthly_token_budget: '' as string | number,
  remark: '',
  status: 'enabled' as 'enabled' | 'disabled',
})

function openCreate() {
  editingId.value = null
  form.tenant_id = canManageAllCompanies.value ? filters.tenant_id || ownTenantId.value : ownTenantId.value
  form.name = ''
  form.owner_name = ''
  form.monthly_token_budget = ''
  form.remark = ''
  form.status = 'enabled'
  editorOpen.value = true
}

function openEdit(item: DepartmentItem) {
  editingId.value = item.id
  form.tenant_id = item.tenant_id
  form.name = item.name
  form.owner_name = item.owner_name ?? ''
  form.monthly_token_budget = item.monthly_token_budget ?? ''
  form.remark = item.remark ?? ''
  form.status = item.status
  editorOpen.value = true
}

function budgetValue(): number | null {
  const raw = String(form.monthly_token_budget).trim()
  if (!raw) return null
  const value = Number(raw)
  return Number.isFinite(value) && value >= 0 ? value : null
}

async function submit() {
  if (!form.name.trim()) {
    message.warning('请输入部门名称')
    return
  }
  saving.value = true
  try {
    const payload = {
      name: form.name.trim(),
      owner_name: form.owner_name.trim() || null,
      monthly_token_budget: budgetValue(),
      remark: form.remark.trim() || null,
      status: form.status,
      tenant_id: canManageAllCompanies.value ? form.tenant_id || null : undefined,
    }
    if (editingId.value) {
      await updateDepartment(editingId.value, payload)
      message.success('部门已更新')
    } else {
      await createDepartment(payload)
      message.success('部门已创建')
    }
    editorOpen.value = false
    await load()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '保存部门失败')
  } finally {
    saving.value = false
  }
}

async function toggleStatus(item: DepartmentItem) {
  const next = item.status === 'enabled' ? 'disabled' : 'enabled'
  try {
    await updateDepartment(item.id, {
      name: item.name,
      owner_name: item.owner_name,
      monthly_token_budget: item.monthly_token_budget,
      remark: item.remark,
      status: next,
    })
    message.success(next === 'disabled' ? '部门已停用' : '部门已启用')
    await load()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '更新部门状态失败')
  }
}

const deleteTarget = ref<DepartmentItem | null>(null)
const deleting = ref(false)
const deleteError = ref('')

function openDelete(item: DepartmentItem) {
  deleteTarget.value = item
  deleteError.value = ''
}

async function confirmDelete() {
  const target = deleteTarget.value
  if (!target || deleting.value) return
  deleting.value = true
  deleteError.value = ''
  try {
    await deleteDepartment(target.id)
    deleteTarget.value = null
    message.success('部门已删除')
    await load()
  } catch (error) {
    deleteError.value = error instanceof Error ? error.message : '删除部门失败'
    message.error(deleteError.value)
  } finally {
    deleting.value = false
  }
}

onMounted(() => {
  void loadTenants()
  void load()
})
</script>

<template>
  <div class="ap-page" data-testid="admin-departments">
    <section class="workstation-glass-panel ap-filter">
      <div class="ap-filter-grid">
        <div class="ap-field">
          <label>公司</label>
          <SearchSelect v-if="canManageAllCompanies" v-model="filters.tenant_id" data-testid="department-filter-tenant">
            <option value="">全部公司</option>
            <option v-for="tenant in tenants" :key="tenant.id" :value="tenant.id">{{ tenant.display_name }}</option>
          </SearchSelect>
          <SearchSelect v-else disabled>
            <option>{{ ownTenantName }}</option>
          </SearchSelect>
        </div>
        <div class="ap-field">
          <label>部门名称</label>
          <input v-model="filters.keyword" class="qc-input" type="text" placeholder="部门名称" @keyup.enter="load" />
        </div>
        <div class="ap-field">
          <label>状态</label>
          <SearchSelect v-model="filters.status">
            <option value="">全部</option>
            <option value="enabled">启用</option>
            <option value="disabled">停用</option>
          </SearchSelect>
        </div>
        <div class="ap-filter-actions">
          <button type="button" class="qc-btn qc-btn-primary" @click="load">查询</button>
          <button type="button" class="qc-btn" @click="resetFilters">重置</button>
        </div>
      </div>
    </section>

    <section class="workstation-glass-panel" data-testid="department-table">
      <div class="qc-card-head">
        <h3>部门管理</h3>
        <button type="button" class="qc-btn qc-btn-primary qc-btn-sm" data-testid="department-create" @click="openCreate">新增部门</button>
      </div>
      <p v-if="loadError" class="qc-error head-error">{{ loadError }}</p>
      <div class="ap-table-wrap">
        <table class="qc-table">
          <thead>
            <tr>
              <th>部门名称</th>
              <th>所属公司</th>
              <th>负责人</th>
              <th>启用产品</th>
              <th>账号数</th>
              <th>月度 Token 预算</th>
              <th>状态</th>
              <th>创建时间</th>
              <th class="is-right">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!items.length">
              <td colspan="9" class="qc-empty">{{ loading ? '加载中…' : '还没有部门，点右上角「新增部门」创建' }}</td>
            </tr>
            <tr v-for="item in items" :key="item.id" :data-testid="`department-row-${item.name}`">
              <td><strong>{{ item.name }}</strong></td>
              <td>{{ item.tenant_name }}</td>
              <td>{{ item.owner_name || '—' }}</td>
              <td>{{ item.product_count }}</td>
              <td>{{ item.user_count }}</td>
              <td>{{ formatMoney(item.monthly_token_budget) }}</td>
              <td>
                <span class="qc-chip" :class="item.status === 'enabled' ? 'tone-success' : 'tone-muted'">
                  {{ item.status === 'enabled' ? '启用' : '停用' }}
                </span>
              </td>
              <td class="qc-muted">{{ formatDate(item.created_at) }}</td>
              <td class="is-right">
                <button type="button" class="qc-link" @click="openEdit(item)">编辑</button>
                <span class="action-sep">|</span>
                <button type="button" class="qc-link is-muted" :data-testid="`department-toggle-${item.name}`" @click="toggleStatus(item)">
                  {{ item.status === 'enabled' ? '停用' : '启用' }}
                </button>
                <span class="action-sep">|</span>
                <button type="button" class="qc-link is-danger" :data-testid="`department-delete-${item.name}`" @click="openDelete(item)">删除</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <div v-if="editorOpen" class="ap-modal-backdrop" data-testid="department-editor" @click.self="editorOpen = false">
      <div class="ap-modal">
        <div class="ap-modal-head">
          <h3>{{ editingId ? '编辑部门' : '新增部门' }}</h3>
          <button type="button" class="ap-modal-close" aria-label="关闭" @click="editorOpen = false">×</button>
        </div>
        <div class="ap-modal-body">
          <div class="ap-field">
            <label class="required">所属公司</label>
            <SearchSelect v-if="canManageAllCompanies" v-model="form.tenant_id" :disabled="Boolean(editingId)" data-testid="department-form-tenant">
              <option v-if="tenantsLoading" value="" disabled>加载中…</option>
              <option v-for="tenant in tenants" :key="tenant.id" :value="tenant.id">{{ tenant.display_name }}</option>
            </SearchSelect>
            <SearchSelect v-else disabled>
              <option>{{ ownTenantName }}</option>
            </SearchSelect>
          </div>
          <div class="ap-field">
            <label class="required">部门名称</label>
            <input v-model="form.name" class="qc-input" type="text" placeholder="例如：一部" data-testid="department-form-name" />
          </div>
          <div class="ap-field">
            <label>负责人</label>
            <input v-model="form.owner_name" class="qc-input" type="text" placeholder="选填" />
          </div>
          <div class="ap-field">
            <label>月度 Token 预算（元）</label>
            <input v-model="form.monthly_token_budget" class="qc-input" type="number" min="0" step="100" placeholder="留空表示不设预算；Token 与费用页按 80% / 90% / 100% 预警" />
          </div>
          <div class="ap-field">
            <label>状态</label>
            <SearchSelect v-model="form.status">
              <option value="enabled">启用</option>
              <option value="disabled">停用</option>
            </SearchSelect>
          </div>
          <div class="ap-field">
            <label>备注</label>
            <textarea v-model="form.remark" class="qc-textarea" rows="3" placeholder="选填"></textarea>
          </div>
        </div>
        <div class="ap-modal-foot">
          <button type="button" class="qc-btn" @click="editorOpen = false">取消</button>
          <button type="button" class="qc-btn qc-btn-primary" :disabled="saving" data-testid="department-form-submit" @click="submit">保存</button>
        </div>
      </div>
    </div>
    <div v-if="deleteTarget" class="ap-modal-backdrop" data-testid="department-delete-modal" @click.self="!deleting && (deleteTarget = null)">
      <div class="ap-modal">
        <div class="ap-modal-head"><h3>删除部门</h3></div>
        <div class="ap-modal-body">
          <p>确认删除「{{ deleteTarget.name }}」？仅无关联的部门可删除，关联账号、产品、任务或历史记录时会提示关联情况。</p>
          <p v-if="deleteError" class="qc-error" role="alert">{{ deleteError }}</p>
        </div>
        <div class="ap-modal-foot">
          <button type="button" class="qc-btn" :disabled="deleting" @click="deleteTarget = null">取消</button>
          <button type="button" class="qc-btn qc-btn-danger" :disabled="deleting" data-testid="department-delete-confirm" @click="confirmDelete">{{ deleting ? '删除中…' : '确认删除' }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.qc-link.is-danger {
  color: var(--workstation-danger, #d03050);
}

.head-error {
  margin: 0;
  padding: 12px 20px;
}

.action-sep {
  margin: 0 8px;
  color: var(--workstation-border);
}

.qc-link.is-muted {
  color: var(--workstation-text-muted);
}
</style>
