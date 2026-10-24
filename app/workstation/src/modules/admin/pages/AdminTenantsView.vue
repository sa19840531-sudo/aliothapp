<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useMessage } from 'naive-ui'
import { createTenant, deleteTenant, listTenants, updateTenant, type TenantItem } from '../api/tenants'
import SearchSelect from '@/components/pickers/SearchSelect.vue'

/**
 * 分公司管理：页头 → 筛选卡 → 分公司列表（名称/总管理员/部门数/产品数/状态/创建时间）
 * → 新增分公司弹层（顺手建总管理员账号）。只有运营中心能进这一页。
 */
const message = useMessage()

const loading = ref(true)
const loadError = ref('')
const tenants = ref<TenantItem[]>([])

const filters = reactive({ name: '', status: '' as '' | 'enabled' | 'disabled' })
const applied = reactive({ name: '', status: '' as '' | 'enabled' | 'disabled' })
const page = ref(1)
const PAGE_SIZE = 10

const filtered = computed(() =>
  tenants.value.filter((item) => {
    if (applied.status && item.status !== applied.status) return false
    if (applied.name && !item.display_name.toLowerCase().includes(applied.name.toLowerCase())) return false
    return true
  }),
)
const pageCount = computed(() => Math.max(1, Math.ceil(filtered.value.length / PAGE_SIZE)))
const pageItems = computed(() => filtered.value.slice((page.value - 1) * PAGE_SIZE, page.value * PAGE_SIZE))
const pageNumbers = computed(() => Array.from({ length: pageCount.value }, (_, index) => index + 1))
const rangeText = computed(() => {
  if (!filtered.value.length) return loading.value ? '加载中…' : '暂无数据'
  const start = (page.value - 1) * PAGE_SIZE + 1
  const end = Math.min(page.value * PAGE_SIZE, filtered.value.length)
  return `显示 ${start}-${end} 条，共 ${filtered.value.length} 条`
})

async function load() {
  loading.value = true
  try {
    tenants.value = await listTenants()
    loadError.value = ''
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '分公司列表加载失败'
  } finally {
    loading.value = false
  }
}

function search() {
  Object.assign(applied, filters)
  page.value = 1
}

function resetFilters() {
  filters.name = ''
  filters.status = ''
  search()
}

function goPage(next: number) {
  page.value = Math.min(Math.max(1, next), pageCount.value)
}

function formatDate(value: string) {
  return new Date(value).toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' })
}

// ---------- 新增 / 编辑 / 启停 ----------

const createOpen = ref(false)
const creating = ref(false)
const createForm = reactive({
  display_name: '',
  admin_username: '',
  admin_full_name: '',
  admin_password: '',
  contact_name: '',
  remark: '',
})

function openCreate() {
  createForm.display_name = ''
  createForm.admin_username = ''
  createForm.admin_full_name = ''
  createForm.admin_password = ''
  createForm.contact_name = ''
  createForm.remark = ''
  createOpen.value = true
}

async function submitCreate() {
  if (!createForm.display_name.trim()) {
    message.warning('请输入公司名称')
    return
  }
  if (!createForm.admin_username.trim()) {
    message.warning('请输入总管理员账号')
    return
  }
  if (createForm.admin_password.length < 6) {
    message.warning('初始密码至少 6 位')
    return
  }
  creating.value = true
  try {
    await createTenant({
      display_name: createForm.display_name.trim(),
      admin_username: createForm.admin_username.trim(),
      admin_full_name: createForm.admin_full_name.trim() || undefined,
      admin_password: createForm.admin_password,
      contact_name: createForm.contact_name.trim() || null,
      remark: createForm.remark.trim() || null,
    })
    createOpen.value = false
    message.success('分公司已创建，总管理员账号可直接登录')
    await load()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '创建分公司失败')
  } finally {
    creating.value = false
  }
}

const editTarget = ref<TenantItem | null>(null)
const saving = ref(false)
const editForm = reactive({ display_name: '', contact_name: '', remark: '' })

function openEdit(item: TenantItem) {
  editTarget.value = item
  editForm.display_name = item.display_name
  editForm.contact_name = item.contact_name ?? ''
  editForm.remark = item.remark ?? ''
}

async function submitEdit() {
  const target = editTarget.value
  if (!target) return
  if (!editForm.display_name.trim()) {
    message.warning('请输入公司名称')
    return
  }
  saving.value = true
  try {
    await updateTenant(target.id, {
      display_name: editForm.display_name.trim(),
      status: target.status,
      contact_name: editForm.contact_name.trim() || null,
      remark: editForm.remark.trim() || null,
    })
    editTarget.value = null
    message.success('分公司信息已更新')
    await load()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '更新分公司失败')
  } finally {
    saving.value = false
  }
}

const toggleTarget = ref<TenantItem | null>(null)

async function confirmToggle() {
  const target = toggleTarget.value
  if (!target) return
  const next = target.status === 'enabled' ? 'disabled' : 'enabled'
  try {
    await updateTenant(target.id, {
      display_name: target.display_name,
      status: next,
      contact_name: target.contact_name,
      remark: target.remark,
    })
    message.success(next === 'disabled' ? '分公司已停用，该公司账号将无法登录' : '分公司已启用')
    toggleTarget.value = null
    await load()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '更新状态失败')
  }
}

const deleteTarget = ref<TenantItem | null>(null)
const deleting = ref(false)
const deleteError = ref('')

function openDelete(item: TenantItem) {
  deleteTarget.value = item
  deleteError.value = ''
}

async function confirmDelete() {
  const target = deleteTarget.value
  if (!target || deleting.value) return
  deleting.value = true
  deleteError.value = ''
  try {
    await deleteTenant(target.id)
    deleteTarget.value = null
    message.success('分公司已删除')
    await load()
    page.value = Math.min(page.value, pageCount.value)
  } catch (error) {
    deleteError.value = error instanceof Error ? error.message : '删除分公司失败'
    message.error(deleteError.value)
  } finally {
    deleting.value = false
  }
}

onMounted(() => void load())
</script>

<template>
  <div class="ap-page" data-testid="admin-tenants">
    <div class="ap-head">
      <div>
        <h1>分公司管理</h1>
        <p>管理集团旗下分公司、总管理员账号及数据隔离范围</p>
      </div>
      <button type="button" class="qc-btn qc-btn-primary" data-testid="tenant-create" @click="openCreate">新增分公司</button>
    </div>

    <section class="workstation-glass-panel ap-filter">
      <div class="ap-filter-grid">
        <div class="ap-field">
          <label>公司名称</label>
          <input v-model="filters.name" class="qc-input" type="text" placeholder="请输入公司名称" @keyup.enter="search" />
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
          <button type="button" class="qc-btn qc-btn-primary" @click="search">查询</button>
          <button type="button" class="qc-btn" @click="resetFilters">重置</button>
        </div>
      </div>
    </section>

    <section class="workstation-glass-panel" data-testid="tenant-table">
      <div class="qc-card-head">
        <h3>分公司列表</h3>
        <span class="qc-muted">{{ loading && !filtered.length ? '加载中…' : `共 ${filtered.length} 条` }}</span>
      </div>
      <p v-if="loadError" class="qc-error head-error">{{ loadError }}</p>
      <div class="ap-table-wrap">
        <table class="qc-table">
          <thead>
            <tr>
              <th>公司名称</th>
              <th>总管理员账号</th>
              <th>部门数</th>
              <th>产品数</th>
              <th>状态</th>
              <th>创建时间</th>
              <th class="is-right">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!pageItems.length">
              <td colspan="7" class="qc-empty">{{ loading ? '加载中…' : '没有符合条件的分公司' }}</td>
            </tr>
            <tr v-for="item in pageItems" :key="item.id" :data-testid="`tenant-row-${item.tenant_key}`">
              <td>
                {{ item.display_name }}
                <span v-if="item.is_headquarters" class="qc-chip tone-danger hq-chip">总部</span>
              </td>
              <td>{{ item.admin_username || '—' }}</td>
              <td>{{ item.department_count }}</td>
              <td>{{ item.product_count }}</td>
              <td>
                <span class="qc-chip" :class="item.status === 'enabled' ? 'tone-success' : 'tone-muted'">
                  {{ item.status === 'enabled' ? '启用' : '停用' }}
                </span>
              </td>
              <td class="qc-muted">{{ formatDate(item.created_at) }}</td>
              <td class="is-right">
                <button type="button" class="qc-link" @click="openEdit(item)">编辑</button>
                <template v-if="!item.is_headquarters">
                  <span class="action-sep">|</span>
                  <button type="button" class="qc-link is-muted" :data-testid="`tenant-toggle-${item.tenant_key}`" @click="toggleTarget = item">
                    {{ item.status === 'enabled' ? '停用' : '启用' }}
                  </button>
                  <span class="action-sep">|</span>
                  <button type="button" class="qc-link is-danger" :data-testid="`tenant-delete-${item.tenant_key}`" @click="openDelete(item)">删除</button>
                </template>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="qc-pager">
        <span>{{ rangeText }}</span>
        <div class="qc-pager-pages">
          <button type="button" :disabled="page <= 1" @click="goPage(page - 1)">上一页</button>
          <button v-for="number in pageNumbers" :key="number" type="button" :class="{ 'is-active': number === page }" @click="goPage(number)">
            {{ number }}
          </button>
          <button type="button" :disabled="page >= pageCount" @click="goPage(page + 1)">下一页</button>
        </div>
      </div>
    </section>

    <div v-if="createOpen" class="ap-modal-backdrop" data-testid="tenant-create-modal" @click.self="createOpen = false">
      <div class="ap-modal">
        <div class="ap-modal-head">
          <h3>新增分公司</h3>
          <button type="button" class="ap-modal-close" aria-label="关闭" @click="createOpen = false">×</button>
        </div>
        <div class="ap-modal-body">
          <div class="ap-field">
            <label class="required">公司名称</label>
            <input v-model="createForm.display_name" class="qc-input" type="text" placeholder="例如：华东分公司" data-testid="tenant-form-name" />
          </div>
          <div class="ap-field">
            <label class="required">总管理员账号</label>
            <input v-model="createForm.admin_username" class="qc-input" type="text" placeholder="登录账号，例如 admin@hd" data-testid="tenant-form-admin" />
          </div>
          <div class="form-row">
            <div class="ap-field">
              <label>管理员姓名</label>
              <input v-model="createForm.admin_full_name" class="qc-input" type="text" placeholder="选填" />
            </div>
            <div class="ap-field">
              <label class="required">初始密码</label>
              <input v-model="createForm.admin_password" class="qc-input" type="password" placeholder="至少 6 位" data-testid="tenant-form-password" />
            </div>
          </div>
          <div class="ap-field">
            <label>联系人</label>
            <input v-model="createForm.contact_name" class="qc-input" type="text" placeholder="选填" />
          </div>
          <div class="ap-field">
            <label>备注</label>
            <textarea v-model="createForm.remark" class="qc-textarea" rows="3" placeholder="选填"></textarea>
          </div>
        </div>
        <div class="ap-modal-foot">
          <button type="button" class="qc-btn" @click="createOpen = false">取消</button>
          <button type="button" class="qc-btn qc-btn-primary" :disabled="creating" data-testid="tenant-form-submit" @click="submitCreate">确认创建</button>
        </div>
      </div>
    </div>

    <div v-if="editTarget" class="ap-modal-backdrop" data-testid="tenant-edit-modal" @click.self="editTarget = null">
      <div class="ap-modal">
        <div class="ap-modal-head">
          <h3>编辑分公司 · {{ editTarget.display_name }}</h3>
          <button type="button" class="ap-modal-close" aria-label="关闭" @click="editTarget = null">×</button>
        </div>
        <div class="ap-modal-body">
          <div class="ap-field">
            <label class="required">公司名称</label>
            <input v-model="editForm.display_name" class="qc-input" type="text" />
          </div>
          <div class="ap-field">
            <label>联系人</label>
            <input v-model="editForm.contact_name" class="qc-input" type="text" placeholder="选填" />
          </div>
          <div class="ap-field">
            <label>备注</label>
            <textarea v-model="editForm.remark" class="qc-textarea" rows="3" placeholder="选填"></textarea>
          </div>
          <p class="qc-muted form-hint">总管理员账号：{{ editTarget.admin_username || '尚未创建' }}（在「账号与权限」维护）</p>
        </div>
        <div class="ap-modal-foot">
          <button type="button" class="qc-btn" @click="editTarget = null">取消</button>
          <button type="button" class="qc-btn qc-btn-primary" :disabled="saving" @click="submitEdit">保存</button>
        </div>
      </div>
    </div>

    <div v-if="toggleTarget" class="ap-modal-backdrop" data-testid="tenant-toggle-modal" @click.self="toggleTarget = null">
      <div class="ap-modal">
        <div class="ap-modal-head">
          <h3>{{ toggleTarget.status === 'enabled' ? '停用分公司' : '启用分公司' }}</h3>
          <button type="button" class="ap-modal-close" aria-label="关闭" @click="toggleTarget = null">×</button>
        </div>
        <div class="ap-modal-body">
          <p class="confirm-text">
            <template v-if="toggleTarget.status === 'enabled'">
              停用「{{ toggleTarget.display_name }}」后，该公司所有账号立即无法登录，也不能再发起新任务；历史任务和报告保留。确认停用？
            </template>
            <template v-else>启用「{{ toggleTarget.display_name }}」后，该公司账号恢复登录。确认启用？</template>
          </p>
        </div>
        <div class="ap-modal-foot">
          <button type="button" class="qc-btn" @click="toggleTarget = null">取消</button>
          <button type="button" class="qc-btn qc-btn-primary" data-testid="tenant-toggle-confirm" @click="confirmToggle">确认</button>
        </div>
      </div>
    </div>
    <div v-if="deleteTarget" class="ap-modal-backdrop" data-testid="tenant-delete-modal" @click.self="!deleting && (deleteTarget = null)">
      <div class="ap-modal">
        <div class="ap-modal-head"><h3>删除分公司</h3></div>
        <div class="ap-modal-body">
          <p>确认删除「{{ deleteTarget.display_name }}」？仅无关联的分公司可删除，关联账号、产品、任务或历史记录时会提示关联情况。</p>
          <p v-if="deleteError" class="qc-error" role="alert">{{ deleteError }}</p>
        </div>
        <div class="ap-modal-foot">
          <button type="button" class="qc-btn" :disabled="deleting" @click="deleteTarget = null">取消</button>
          <button type="button" class="qc-btn qc-btn-danger" :disabled="deleting" data-testid="tenant-delete-confirm" @click="confirmDelete">{{ deleting ? '删除中…' : '确认删除' }}</button>
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

.hq-chip {
  margin-left: 6px;
}

.action-sep {
  margin: 0 8px;
  color: var(--workstation-border);
}

.qc-link.is-muted {
  color: var(--workstation-text-muted);
}

.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

.form-hint {
  margin: 0;
  font-size: 12px;
}

.confirm-text {
  margin: 0;
  color: var(--workstation-text);
  font-size: 14px;
  line-height: 1.7;
}
</style>
