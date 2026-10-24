<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useMessage } from 'naive-ui'
import SearchSelect from '@/components/pickers/SearchSelect.vue'
import { authStore } from '@/stores/auth'
import { listDepartments, type DepartmentItem } from '../api/departments'
import { listTenants, type TenantItem } from '../api/tenants'
import {
  createUser,
  deleteUser,
  listUsers,
  updateUser,
  type UserItem,
  type UserRole,
  type UserStatus,
} from '../api/users'

/**
 * 账号与权限（对齐 2.1 原型 system-account.html）：筛选（公司/部门/角色/关键词）→ 账号列表
 * （账号/姓名/角色/所属公司/部门/数据权限范围/最近登录时间/状态）→ 创建/编辑账号弹层。
 * 运营中心管所有公司；分公司管理员只能在本公司里建人、改人，不能创建运营中心账号。
 */
const message = useMessage()

const permissions = computed(() => new Set(authStore.user.value?.permissions ?? []))
const canManageAllCompanies = computed(() => permissions.value.has('users:any'))
const ownTenantId = computed(() => authStore.user.value?.tenant_id ?? '')
const ownTenantName = computed(() => authStore.user.value?.tenant_name || '本公司')

/** 角色名沿用后端角色；括号里是原型里对应的叫法。 */
const ROLE_OPTIONS: Array<{ value: UserRole; label: string; hqOnly?: boolean }> = [
  { value: '平台管理员', label: '运营中心账号（平台管理员）', hqOnly: true },
  { value: '公司管理员', label: '分公司总管理员（公司管理员）' },
  { value: '审计员', label: '审计员（只读）' },
  { value: '规则配置员', label: '部门账号（规则配置员）' },
]

function roleLabel(role: string) {
  return ROLE_OPTIONS.find((item) => item.value === role)?.label ?? role
}

const roleOptions = computed(() =>
  ROLE_OPTIONS.filter((item) => !item.hqOnly || canManageAllCompanies.value),
)

const tenants = ref<TenantItem[]>([])
const departments = ref<DepartmentItem[]>([])
const users = ref<UserItem[]>([])
const total = ref(0)
const loading = ref(true)
/** 公司 / 部门选项还在加载：弹窗里先说「加载中…」，不说「该公司还没有部门」 */
const optionsLoading = ref(true)
const loadError = ref('')

const query = reactive({
  tenant_id: '',
  department_name: '',
  role: '' as UserRole | '',
  status: '' as UserStatus | '',
  keyword: '',
  page: 1,
})
const PAGE_SIZE = 10
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))
const pageNumbers = computed(() => Array.from({ length: pageCount.value }, (_, index) => index + 1))

/** 部门下拉跟着公司走：运营中心看选中公司的部门，分公司只看本公司的。 */
const departmentOptions = computed(() => {
  const tenantId = canManageAllCompanies.value ? query.tenant_id : ownTenantId.value
  const names = departments.value
    .filter((item) => !tenantId || item.tenant_id === tenantId)
    .map((item) => item.name)
  return Array.from(new Set(names))
})

async function load() {
  loading.value = true
  try {
    const result = await listUsers({
      keyword: query.keyword.trim() || undefined,
      status: query.status || 'all',
      tenant_id: canManageAllCompanies.value ? query.tenant_id || 'all' : 'all',
      role: query.role || 'all',
      department_name: query.department_name || undefined,
      page: query.page,
      page_size: PAGE_SIZE,
    })
    users.value = result.items
    total.value = result.total
    loadError.value = ''
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '账号列表加载失败'
  } finally {
    loading.value = false
  }
}

async function loadOptions() {
  if (canManageAllCompanies.value && permissions.value.has('tenants')) {
    try {
      tenants.value = await listTenants()
    } catch {
      tenants.value = []
    }
  }
  if (permissions.value.has('departments')) {
    try {
      departments.value = await listDepartments()
    } catch {
      departments.value = []
    }
  }
  optionsLoading.value = false
}

function search() {
  query.page = 1
  void load()
}

function resetFilters() {
  query.tenant_id = ''
  query.department_name = ''
  query.role = ''
  query.status = ''
  query.keyword = ''
  search()
}

function goPage(next: number) {
  query.page = Math.min(Math.max(1, next), pageCount.value)
  void load()
}

watch(
  () => query.tenant_id,
  () => {
    query.department_name = ''
  },
)

function formatDate(value: string | null | undefined) {
  if (!value) return '从未登录'
  return new Date(value).toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

// ---------- 创建 / 编辑 / 启停 / 删除 ----------

const editorOpen = ref(false)
const editingId = ref<string | null>(null)
const saving = ref(false)
const form = reactive({
  username: '',
  full_name: '',
  password: '',
  role: '规则配置员' as UserRole,
  status: 'enabled' as UserStatus,
  tenant_id: '',
  department_name: '',
})

/** 部门账号按所属部门划分数据范围，必须选部门；其他角色看整个公司或全部公司，不带部门。 */
const DEPARTMENT_ROLE: UserRole = '规则配置员'
const needsDepartment = computed(() => form.role === DEPARTMENT_ROLE)

/** 所属部门只能选所选公司在「部门管理」里的启用部门；编辑时原部门已停用或不在部门管理里，也照常显示出来。 */
const formDepartmentOptions = computed(() => {
  const tenantId = canManageAllCompanies.value ? form.tenant_id : ownTenantId.value
  const options = departments.value
    .filter((item) => item.tenant_id === tenantId && item.status === 'enabled')
    .map((item) => ({ value: item.name, label: item.name }))
  const current = form.department_name
  if (current && !options.some((item) => item.value === current)) {
    options.unshift({ value: current, label: `${current}（已停用或不在部门管理里）` })
  }
  return options
})
/** 弹窗里当前选中的公司名，用于「这个公司还没有部门」的提示。 */
const formTenantName = computed(() =>
  canManageAllCompanies.value
    ? (tenants.value.find((item) => item.id === form.tenant_id)?.display_name ?? '所选公司')
    : ownTenantName.value,
)

function openCreate() {
  editingId.value = null
  form.username = ''
  form.full_name = ''
  form.password = ''
  form.role = '规则配置员'
  form.status = 'enabled'
  form.tenant_id = canManageAllCompanies.value ? query.tenant_id || ownTenantId.value : ownTenantId.value
  // 筛选里选了本公司的启用部门就顺手带上，否则留空让人选
  const filtered = query.department_name
  form.department_name = departments.value.some(
    (item) => item.name === filtered && item.tenant_id === form.tenant_id && item.status === 'enabled',
  )
    ? filtered
    : ''
  editorOpen.value = true
}

function openEdit(item: UserItem) {
  editingId.value = item.id
  form.username = item.username
  form.full_name = item.full_name
  form.password = ''
  form.role = item.role
  form.status = item.status
  form.tenant_id = item.tenant_id
  form.department_name = item.department_name ?? ''
  editorOpen.value = true
}

async function submit() {
  if (!form.username.trim() || !form.full_name.trim()) {
    message.warning('请填写账号和姓名')
    return
  }
  if (!editingId.value && form.password.length < 6) {
    message.warning('初始密码至少 6 位')
    return
  }
  if (editingId.value && form.password && form.password.length < 6) {
    message.warning('新密码至少 6 位')
    return
  }
  if (needsDepartment.value && !form.department_name) {
    message.warning('部门账号要选择所属部门')
    return
  }
  saving.value = true
  try {
    const tenantId = canManageAllCompanies.value ? form.tenant_id || undefined : undefined
    const departmentName = needsDepartment.value ? form.department_name : null
    if (editingId.value) {
      await updateUser(editingId.value, {
        full_name: form.full_name.trim(),
        role: form.role,
        status: form.status,
        password: form.password || undefined,
        tenant_id: tenantId,
        department_name: departmentName,
      })
      message.success('账号已更新')
    } else {
      await createUser({
        username: form.username.trim(),
        full_name: form.full_name.trim(),
        password: form.password,
        role: form.role,
        status: form.status,
        tenant_id: tenantId,
        department_name: departmentName,
      })
      message.success('账号已创建')
    }
    editorOpen.value = false
    await load()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '保存账号失败')
  } finally {
    saving.value = false
  }
}

async function toggleStatus(item: UserItem) {
  const next: UserStatus = item.status === 'enabled' ? 'disabled' : 'enabled'
  try {
    await updateUser(item.id, {
      full_name: item.full_name,
      role: item.role,
      status: next,
      department_name: item.department_name,
    })
    message.success(next === 'disabled' ? '账号已停用，立即失去菜单和数据访问' : '账号已启用')
    await load()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '更新账号状态失败')
  }
}

const deleteTarget = ref<UserItem | null>(null)

async function confirmDelete() {
  const target = deleteTarget.value
  if (!target) return
  try {
    await deleteUser(target.id)
    message.success('账号已删除')
    deleteTarget.value = null
    await load()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '删除账号失败')
  }
}

onMounted(() => {
  void loadOptions()
  void load()
})
</script>

<template>
  <div class="ap-page" data-testid="admin-users">
    <section class="workstation-glass-panel ap-filter">
      <div class="ap-filter-grid">
        <div class="ap-field">
          <label>公司</label>
          <SearchSelect v-if="canManageAllCompanies" v-model="query.tenant_id" data-testid="user-filter-tenant">
            <option value="">全部公司</option>
            <option v-for="tenant in tenants" :key="tenant.id" :value="tenant.id">{{ tenant.display_name }}</option>
          </SearchSelect>
          <SearchSelect v-else disabled>
            <option>{{ ownTenantName }}</option>
          </SearchSelect>
        </div>
        <div class="ap-field">
          <label>部门</label>
          <SearchSelect v-model="query.department_name">
            <option value="">全部部门</option>
            <option v-for="name in departmentOptions" :key="name" :value="name">{{ name }}</option>
          </SearchSelect>
        </div>
        <div class="ap-field">
          <label>角色</label>
          <SearchSelect v-model="query.role" data-testid="user-filter-role">
            <option value="">全部角色</option>
            <option v-for="item in ROLE_OPTIONS" :key="item.value" :value="item.value">{{ item.label }}</option>
          </SearchSelect>
        </div>
        <div class="ap-field">
          <label>状态</label>
          <SearchSelect v-model="query.status">
            <option value="">全部状态</option>
            <option value="enabled">启用</option>
            <option value="disabled">停用</option>
          </SearchSelect>
        </div>
      </div>
      <div class="filter-row">
        <input v-model="query.keyword" class="qc-input keyword" type="search" placeholder="账号 / 姓名" data-testid="user-filter-keyword" @keyup.enter="search" />
        <button type="button" class="qc-btn qc-btn-primary" data-testid="user-search" @click="search">查询</button>
        <button type="button" class="qc-btn" @click="resetFilters">重置</button>
      </div>
    </section>

    <section class="workstation-glass-panel" data-testid="user-table">
      <div class="qc-card-head">
        <h3>账号列表</h3>
        <button type="button" class="qc-btn qc-btn-primary qc-btn-sm" data-testid="user-create" @click="openCreate">创建账号</button>
      </div>
      <p v-if="loadError" class="qc-error head-error">{{ loadError }}</p>
      <div class="ap-table-wrap">
        <table class="qc-table">
          <thead>
            <tr>
              <th>账号</th>
              <th>姓名</th>
              <th>角色</th>
              <th>所属公司</th>
              <th>部门</th>
              <th>数据权限范围</th>
              <th>最近登录时间</th>
              <th>状态</th>
              <th class="is-right">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!users.length">
              <td colspan="9" class="qc-empty">{{ loading ? '加载中…' : '没有符合条件的账号' }}</td>
            </tr>
            <tr v-for="item in users" :key="item.id" :data-testid="`user-row-${item.username}`">
              <td><strong>{{ item.username }}</strong></td>
              <td>{{ item.full_name }}</td>
              <td><span class="qc-chip tone-primary">{{ roleLabel(item.role) }}</span></td>
              <td>{{ item.tenant_name || '—' }}</td>
              <td>{{ item.department_name || '—' }}</td>
              <td class="qc-muted">{{ item.data_scope }}</td>
              <td class="qc-muted">{{ formatDate(item.last_login_at) }}</td>
              <td>
                <span class="qc-chip" :class="item.status === 'enabled' ? 'tone-success' : 'tone-muted'">
                  {{ item.status === 'enabled' ? '启用' : '停用' }}
                </span>
              </td>
              <td class="is-right actions">
                <button type="button" class="qc-link" @click="openEdit(item)">编辑</button>
                <span class="action-sep">|</span>
                <button type="button" class="qc-link is-muted" @click="toggleStatus(item)">
                  {{ item.status === 'enabled' ? '停用' : '启用' }}
                </button>
                <span class="action-sep">|</span>
                <button type="button" class="qc-link is-danger" @click="deleteTarget = item">删除</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="qc-pager">
        <span>{{ loading && !users.length ? '加载中…' : `共 ${total} 个账号` }}</span>
        <div class="qc-pager-pages">
          <button type="button" :disabled="query.page <= 1" @click="goPage(query.page - 1)">上一页</button>
          <button v-for="number in pageNumbers" :key="number" type="button" :class="{ 'is-active': number === query.page }" @click="goPage(number)">
            {{ number }}
          </button>
          <button type="button" :disabled="query.page >= pageCount" @click="goPage(query.page + 1)">下一页</button>
        </div>
      </div>
    </section>

    <div v-if="editorOpen" class="ap-modal-backdrop" data-testid="user-editor" @click.self="editorOpen = false">
      <div class="ap-modal">
        <div class="ap-modal-head">
          <h3>{{ editingId ? '编辑账号' : '创建账号' }}</h3>
          <button type="button" class="ap-modal-close" aria-label="关闭" @click="editorOpen = false">×</button>
        </div>
        <div class="ap-modal-body">
          <div class="form-row">
            <div class="ap-field">
              <label class="required">账号</label>
              <input v-model="form.username" class="qc-input" type="text" :disabled="Boolean(editingId)" placeholder="登录用" data-testid="user-form-username" />
            </div>
            <div class="ap-field">
              <label class="required">姓名</label>
              <input v-model="form.full_name" class="qc-input" type="text" data-testid="user-form-name" />
            </div>
          </div>
          <div class="ap-field">
            <label :class="{ required: !editingId }">{{ editingId ? '重置密码' : '初始密码' }}</label>
            <input v-model="form.password" class="qc-input" type="password" :placeholder="editingId ? '留空则不改' : '至少 6 位'" data-testid="user-form-password" />
          </div>
          <div class="form-row">
            <div class="ap-field">
              <label class="required">角色</label>
              <SearchSelect v-model="form.role" data-testid="user-form-role">
                <option v-for="item in roleOptions" :key="item.value" :value="item.value">{{ item.label }}</option>
              </SearchSelect>
            </div>
            <div class="ap-field">
              <label>状态</label>
              <SearchSelect v-model="form.status">
                <option value="enabled">启用</option>
                <option value="disabled">停用</option>
              </SearchSelect>
            </div>
          </div>
          <div class="form-row">
            <div class="ap-field">
              <label class="required">所属公司</label>
              <!-- 换公司后原来选的部门不再适用，清空重选 -->
              <SearchSelect
                v-if="canManageAllCompanies"
                v-model="form.tenant_id"
                data-testid="user-form-tenant"
                @change="form.department_name = ''"
              >
                <option v-if="optionsLoading" value="" disabled>加载中…</option>
                <option v-for="tenant in tenants.filter((item) => item.status === 'enabled')" :key="tenant.id" :value="tenant.id">
                  {{ tenant.display_name }}
                </option>
              </SearchSelect>
              <SearchSelect v-else disabled>
                <option>{{ ownTenantName }}</option>
              </SearchSelect>
            </div>
            <div v-if="needsDepartment" class="ap-field">
              <label class="required">所属部门</label>
              <SearchSelect v-model="form.department_name" data-testid="user-form-department">
                <option value="">{{ optionsLoading ? '加载中…' : formDepartmentOptions.length ? '请选择部门' : '该公司还没有部门' }}</option>
                <option v-for="item in formDepartmentOptions" :key="item.value" :value="item.value">{{ item.label }}</option>
              </SearchSelect>
              <small v-if="!optionsLoading && !formDepartmentOptions.length" class="qc-muted form-hint" data-testid="user-form-department-empty">
                「{{ formTenantName }}」下还没有部门：请把所属公司换成部门所在的公司，或先到「部门管理」新增
              </small>
            </div>
          </div>
          <p class="qc-muted form-hint">
            数据权限跟着角色走：运营中心账号看全部公司；分公司总管理员看本公司全部部门；审计员本公司只读；部门账号只看所属部门的数据。
          </p>
        </div>
        <div class="ap-modal-foot">
          <button type="button" class="qc-btn" @click="editorOpen = false">取消</button>
          <button type="button" class="qc-btn qc-btn-primary" :disabled="saving" data-testid="user-form-submit" @click="submit">保存</button>
        </div>
      </div>
    </div>

    <div v-if="deleteTarget" class="ap-modal-backdrop" data-testid="user-delete-modal" @click.self="deleteTarget = null">
      <div class="ap-modal">
        <div class="ap-modal-head">
          <h3>删除账号</h3>
          <button type="button" class="ap-modal-close" aria-label="关闭" @click="deleteTarget = null">×</button>
        </div>
        <div class="ap-modal-body">
          <p class="confirm-text">确认删除账号「{{ deleteTarget.username }}」？删除后不可恢复；只是暂时不用请改为「停用」。</p>
        </div>
        <div class="ap-modal-foot">
          <button type="button" class="qc-btn" @click="deleteTarget = null">取消</button>
          <button type="button" class="qc-btn qc-btn-primary" data-testid="user-delete-confirm" @click="confirmDelete">确认删除</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.filter-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}

.filter-row .keyword {
  width: 260px;
}

.head-error {
  margin: 0;
  padding: 12px 20px;
}

.actions {
  white-space: nowrap;
}

.action-sep {
  margin: 0 8px;
  color: var(--workstation-border);
}

.qc-link.is-muted {
  color: var(--workstation-text-muted);
}

.qc-link.is-danger {
  color: var(--workstation-danger);
}

.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

.form-hint {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
}

.confirm-text {
  margin: 0;
  color: var(--workstation-text);
  font-size: 14px;
  line-height: 1.7;
}
</style>
