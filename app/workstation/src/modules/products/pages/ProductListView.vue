<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useMessage } from 'naive-ui'
import ProductImportPanel from '../components/ProductImportPanel.vue'
import SearchSelect from '@/components/pickers/SearchSelect.vue'
import { usePortalNav } from '@/composables/usePortalNav'
import { listDepartments } from '@/modules/admin/api/departments'
import { listTenants, type TenantItem } from '@/modules/admin/api/tenants'
import {
  createProduct,
  listProducts,
  PRODUCT_PLATFORM_LABELS,
  updateProduct,
  type ProductPayload,
  type ProductPlatform,
  type ProductRecord,
  type ProductStatus,
} from '@/services/products.api'
import { authStore } from '@/stores/auth'

/**
 * 产品管理（对齐 2.1 原型 product.html）：产品列表 / 批量导入 / 导入记录。
 * 列表走服务端分页与筛选（关键词、公司、部门、平台、状态），停用产品也能看到并可重新启用；
 * 运营中心能看所有公司的产品并指定归属，分公司账号只看本公司的 + 运营中心共享的。
 * 管理后台下 Excel 导入是独立页面，这里只显示列表。
 */
const nav = usePortalNav()
const message = useMessage()

const permissions = computed(() => new Set(authStore.user.value?.permissions ?? []))
const canReadAny = computed(() => permissions.value.has('taskObjects:read:any'))
const workspaceName = computed(() => authStore.user.value?.tenant_name || '本公司')
const showTabs = computed(() => !nav.isAdmin.value)
/** 部门平台不展示公司 / 部门（2.1 原型）：组织范围由登录身份确定，后端固定归本部门。 */
const showOrg = computed(() => nav.isAdmin.value)
/** 共享产品由运营中心维护，其他账号不显示编辑 / 停用（后端也会拒绝）。 */
function canMaintain(item: ProductRecord) {
  return canReadAny.value || Boolean(item.tenant_id)
}

type TabKey = 'list' | 'import' | 'records'
const activeTab = ref<TabKey>('list')
const TABS: Array<{ key: TabKey; label: string }> = [
  { key: 'list', label: '产品列表' },
  { key: 'import', label: '批量导入' },
  { key: 'records', label: '导入记录' },
]
const importTab = computed<'import' | 'record'>(() => (activeTab.value === 'records' ? 'record' : 'import'))

function switchTab(key: TabKey) {
  activeTab.value = key
}

function onImportTabChange(tab: 'import' | 'record') {
  activeTab.value = tab === 'record' ? 'records' : 'import'
}

const PLATFORM_OPTIONS = (Object.keys(PRODUCT_PLATFORM_LABELS) as ProductPlatform[]).map((value) => ({
  value,
  label: PRODUCT_PLATFORM_LABELS[value],
}))

function platformLabel(code: string) {
  return PRODUCT_PLATFORM_LABELS[code as ProductPlatform] ?? code
}

function platformsOf(item: ProductRecord): ProductPlatform[] {
  const codes = (item.platforms ?? []).map((code) => String(code)) as ProductPlatform[]
  if (codes.length) return codes
  return item.h5_url ? ['h5'] : []
}

// ---------- 列表 ----------

const items = ref<ProductRecord[]>([])
const total = ref(0)
const page = ref(1)
const PAGE_SIZE = 10
const loading = ref(true)
const loadError = ref('')

const filters = reactive({
  keyword: '',
  tenant_id: '',
  department: '',
  platform: '' as ProductPlatform | '',
  status: '' as ProductStatus | '',
})

const tenants = ref<TenantItem[]>([])
const departmentNames = ref<string[]>([])

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))
const pageNumbers = computed(() => Array.from({ length: pageCount.value }, (_, index) => index + 1))

async function load() {
  loading.value = true
  try {
    const result = await listProducts({
      keyword: filters.keyword,
      status: filters.status,
      tenant_id: canReadAny.value ? filters.tenant_id || undefined : undefined,
      department_name: filters.department || undefined,
      platform: filters.platform,
      page: page.value,
      page_size: PAGE_SIZE,
    })
    items.value = result.items
    total.value = result.total
    loadError.value = ''
    if (!departmentNames.value.length) {
      departmentNames.value = Array.from(
        new Set(result.items.map((item) => item.department_name).filter((name): name is string => !!name)),
      )
    }
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '产品列表加载失败'
  } finally {
    loading.value = false
  }
}

async function loadOptions() {
  if (canReadAny.value && permissions.value.has('tenants')) {
    try {
      tenants.value = await listTenants()
    } catch {
      tenants.value = []
    }
  }
  if (permissions.value.has('departments')) {
    try {
      const departments = await listDepartments()
      departmentNames.value = Array.from(new Set(departments.map((item) => item.name)))
    } catch {
      // 没有部门权限就用列表里出现过的部门名
    }
  }
}

function search() {
  page.value = 1
  void load()
}

function resetFilters() {
  filters.keyword = ''
  filters.tenant_id = ''
  filters.department = ''
  filters.platform = ''
  filters.status = ''
  search()
}

function goPage(next: number) {
  page.value = Math.min(Math.max(1, next), pageCount.value)
  void load()
}

function startInspect(item: ProductRecord) {
  void nav.push({ path: '/quick', query: { product: item.project_code } })
}

// ---------- 详情 / 新增 / 编辑 / 停用 ----------

const detailTarget = ref<ProductRecord | null>(null)

const editorOpen = ref(false)
const editorMode = ref<'create' | 'edit'>('create')
const editingId = ref<string | null>(null)
const saving = ref(false)
const form = reactive({
  project_code: '',
  product_name: '',
  tenant_id: '',
  department_name: '',
  owner_name: '',
  platforms: [] as ProductPlatform[],
  h5_url: '',
  status: 'enabled' as ProductStatus,
})

function openCreate() {
  editorMode.value = 'create'
  editingId.value = null
  form.project_code = ''
  form.product_name = ''
  form.tenant_id = canReadAny.value && filters.tenant_id !== 'shared' ? filters.tenant_id : ''
  form.department_name = filters.department || ''
  form.owner_name = ''
  form.platforms = []
  form.h5_url = ''
  form.status = 'enabled'
  editorOpen.value = true
}

function openEdit(item: ProductRecord) {
  editorMode.value = 'edit'
  editingId.value = item.id
  form.project_code = item.project_code
  form.product_name = item.product_name
  form.tenant_id = item.tenant_id ?? ''
  form.department_name = item.department_name ?? ''
  form.owner_name = item.owner_name ?? ''
  form.platforms = platformsOf(item)
  form.h5_url = item.h5_url ?? ''
  form.status = item.status
  editorOpen.value = true
}

function togglePlatform(code: ProductPlatform) {
  form.platforms = form.platforms.includes(code)
    ? form.platforms.filter((item) => item !== code)
    : [...form.platforms, code]
}

function buildPayload(): ProductPayload {
  return {
    project_code: form.project_code.trim(),
    product_name: form.product_name.trim(),
    h5_url: form.h5_url.trim() || null,
    department_name: form.department_name.trim() || null,
    owner_name: form.owner_name.trim() || null,
    platforms: [...form.platforms],
    status: form.status,
    tenant_id: canReadAny.value ? form.tenant_id || null : undefined,
  }
}

async function saveProduct() {
  if (!form.project_code.trim() || !form.product_name.trim()) {
    message.warning('请填写产品编码和产品名称')
    return
  }
  saving.value = true
  try {
    if (editorMode.value === 'create') {
      await createProduct(buildPayload())
      message.success('产品已登记')
    } else if (editingId.value) {
      await updateProduct(editingId.value, buildPayload())
      message.success('产品已更新')
    }
    editorOpen.value = false
    await load()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '保存失败')
  } finally {
    saving.value = false
  }
}

async function toggleStatus(item: ProductRecord) {
  const next: ProductStatus = item.status === 'disabled' ? 'enabled' : 'disabled'
  try {
    await updateProduct(item.id, {
      project_code: item.project_code,
      product_name: item.product_name,
      h5_url: item.h5_url,
      department_name: item.department_name ?? null,
      owner_name: item.owner_name ?? null,
      platforms: platformsOf(item),
      status: next,
      tenant_id: canReadAny.value ? item.tenant_id ?? null : undefined,
    })
    message.success(next === 'disabled' ? '产品已停用' : '产品已启用')
    await load()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '状态更新失败')
  }
}

function tenantLabel(item: ProductRecord) {
  return item.tenant_name || (item.tenant_id ? '—' : '运营中心（共享）')
}

function formatDate(value: string | null | undefined) {
  if (!value) return '—'
  return new Date(value).toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

watch(
  () => filters.tenant_id,
  () => {
    filters.department = ''
  },
)

onMounted(() => {
  void loadOptions()
  void load()
})
</script>

<template>
  <div class="workstation-page product-page">
    <div v-if="showTabs" class="workstation-glass-panel tab-card" data-testid="product-tabs">
      <button
        v-for="tab in TABS"
        :key="tab.key"
        type="button"
        class="tab-btn"
        :class="{ active: activeTab === tab.key }"
        :data-testid="`product-tab-${tab.key}`"
        @click="switchTab(tab.key)"
      >
        {{ tab.label }}
      </button>
    </div>

    <template v-if="activeTab === 'list'">
      <section class="workstation-glass-panel filter-card">
        <div class="filter-grid">
          <template v-if="showOrg">
          <SearchSelect v-if="canReadAny" v-model="filters.tenant_id" data-testid="product-filter-tenant">
            <option value="">全部公司</option>
            <option value="shared">运营中心（共享产品）</option>
            <option v-for="tenant in tenants" :key="tenant.id" :value="tenant.id">{{ tenant.display_name }}</option>
          </SearchSelect>
          <SearchSelect v-else disabled :title="workspaceName">
            <option>{{ workspaceName }}</option>
          </SearchSelect>
          <SearchSelect v-model="filters.department">
            <option value="">全部部门</option>
            <option v-for="name in departmentNames" :key="name" :value="name">{{ name }}</option>
          </SearchSelect>
          </template>
          <SearchSelect v-model="filters.platform">
            <option value="">全部平台</option>
            <option v-for="option in PLATFORM_OPTIONS" :key="option.value" :value="option.value">{{ option.label }}</option>
          </SearchSelect>
          <SearchSelect v-model="filters.status" data-testid="product-filter-status">
            <option value="">全部状态</option>
            <option value="enabled">启用</option>
            <option value="disabled">停用</option>
          </SearchSelect>
        </div>
        <div class="filter-row">
          <input
            v-model="filters.keyword"
            class="qc-input keyword"
            type="search"
            placeholder="产品名称 / 编码"
            data-testid="product-search"
            @keyup.enter="search"
          />
          <button type="button" class="qc-btn qc-btn-primary" @click="search">查询</button>
          <button type="button" class="qc-btn" @click="resetFilters">重置</button>
        </div>
      </section>

      <section class="workstation-glass-panel table-card" data-testid="product-table">
        <div class="qc-card-head">
          <h3>产品列表 <span class="qc-muted">{{ loading && !items.length ? '加载中…' : `共 ${total} 个` }}</span></h3>
          <button type="button" class="qc-btn qc-btn-primary qc-btn-sm" data-testid="product-create" @click="openCreate">新增产品</button>
        </div>
        <p v-if="loadError" class="qc-error head-error">{{ loadError }}</p>
        <div class="table-wrap">
          <table class="qc-table">
            <thead>
              <tr>
                <th>产品名称</th>
                <th>产品编码</th>
                <th v-if="canReadAny && showOrg">所属公司</th>
                <th v-if="showOrg">所属部门</th>
                <th>支持平台</th>
                <th>负责人</th>
                <th>状态</th>
                <th class="is-right">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="!items.length">
                <td :colspan="6 + (canReadAny && showOrg ? 1 : 0) + (showOrg ? 1 : 0)" class="qc-empty">{{ loading ? '加载中…' : loadError ? '产品列表加载失败，请稍后重试' : '没有符合条件的产品' }}</td>
              </tr>
              <tr v-for="item in items" :key="item.id" :data-testid="`product-row-${item.project_code}`">
                <td><strong>{{ item.product_name }}</strong></td>
                <td class="qc-mono">{{ item.project_code }}</td>
                <td v-if="canReadAny && showOrg">{{ tenantLabel(item) }}</td>
                <td v-if="showOrg">{{ item.department_name || '—' }}</td>
                <td>
                  <span v-for="code in platformsOf(item)" :key="code" class="qc-chip platform-chip">{{ platformLabel(code) }}</span>
                  <span v-if="!platformsOf(item).length" class="qc-muted">—</span>
                </td>
                <td>{{ item.owner_name || '—' }}</td>
                <td>
                  <span class="qc-chip" :class="item.status === 'disabled' ? 'tone-muted' : 'tone-success'">
                    {{ item.status === 'disabled' ? '停用' : '启用' }}
                  </span>
                </td>
                <td class="is-right actions">
                  <button type="button" class="qc-link" @click="detailTarget = item">详情</button>
                  <template v-if="canMaintain(item)">
                    <button type="button" class="qc-link" @click="openEdit(item)">编辑</button>
                    <button type="button" class="qc-link is-muted" @click="toggleStatus(item)">
                      {{ item.status === 'disabled' ? '启用' : '停用' }}
                    </button>
                  </template>
                  <button type="button" class="qc-link" :disabled="item.status === 'disabled'" @click="startInspect(item)">发起质检</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-if="!loading || items.length" class="qc-pager">
          <span>第 {{ page }} / {{ pageCount }} 页</span>
          <div class="qc-pager-pages">
            <button type="button" :disabled="page <= 1" @click="goPage(page - 1)">上一页</button>
            <button v-for="number in pageNumbers" :key="number" type="button" :class="{ 'is-active': number === page }" @click="goPage(number)">
              {{ number }}
            </button>
            <button type="button" :disabled="page >= pageCount" @click="goPage(page + 1)">下一页</button>
          </div>
        </div>
      </section>
    </template>

    <section v-else class="workstation-glass-panel" data-testid="product-import">
      <ProductImportPanel :tab="importTab" @update:tab="onImportTabChange" @imported="load" />
    </section>

    <div v-if="detailTarget" class="ap-modal-backdrop" data-testid="product-detail" @click.self="detailTarget = null">
      <div class="ap-modal">
        <div class="ap-modal-head">
          <h3>{{ detailTarget.product_name }}</h3>
          <button type="button" class="ap-modal-close" aria-label="关闭" @click="detailTarget = null">×</button>
        </div>
        <div class="ap-modal-body">
          <dl class="detail-list">
            <dt>产品编码</dt><dd class="qc-mono">{{ detailTarget.project_code }}</dd>
            <template v-if="showOrg">
              <dt>所属公司</dt><dd>{{ tenantLabel(detailTarget) }}</dd>
              <dt>所属部门</dt><dd>{{ detailTarget.department_name || '—' }}</dd>
            </template>
            <dt>负责人</dt><dd>{{ detailTarget.owner_name || '—' }}</dd>
            <dt>支持平台</dt><dd>{{ platformsOf(detailTarget).map(platformLabel).join(' / ') || '—' }}</dd>
            <dt>H5 地址</dt><dd class="break">{{ detailTarget.h5_url || '—' }}</dd>
            <dt>状态</dt><dd>{{ detailTarget.status === 'disabled' ? '停用' : '启用' }}</dd>
            <dt>登记时间</dt><dd>{{ formatDate(detailTarget.created_at) }}</dd>
            <dt>最近更新</dt><dd>{{ formatDate(detailTarget.updated_at) }}</dd>
          </dl>
          <p v-if="platformsOf(detailTarget).includes('ios')" class="qc-muted ios-note">iOS 目前只做产品资料登记，自动质检能力待接入。</p>
        </div>
        <div class="ap-modal-foot">
          <button type="button" class="qc-btn" @click="detailTarget = null">关闭</button>
          <button
            v-if="canMaintain(detailTarget)"
            type="button"
            class="qc-btn qc-btn-primary"
            @click="openEdit(detailTarget); detailTarget = null"
          >
            编辑
          </button>
        </div>
      </div>
    </div>

    <div v-if="editorOpen" class="ap-modal-backdrop" data-testid="product-editor" @click.self="editorOpen = false">
      <div class="ap-modal">
        <div class="ap-modal-head">
          <h3>{{ editorMode === 'create' ? '新增产品' : '编辑产品' }}</h3>
          <button type="button" class="ap-modal-close" aria-label="关闭" @click="editorOpen = false">×</button>
        </div>
        <div class="ap-modal-body">
          <div class="form-row">
            <div class="ap-field">
              <label class="required">产品编码</label>
              <input v-model="form.project_code" class="qc-input" type="text" placeholder="不能和已有产品重复，例如 MB-001" data-testid="product-code" />
            </div>
            <div class="ap-field">
              <label class="required">产品名称</label>
              <input v-model="form.product_name" class="qc-input" type="text" data-testid="product-name" />
            </div>
          </div>
          <div v-if="showOrg" class="form-row">
            <div class="ap-field">
              <label>所属公司</label>
              <SearchSelect v-if="canReadAny" v-model="form.tenant_id" data-testid="product-tenant">
                <option value="">运营中心（共享，全公司可见）</option>
                <option v-for="tenant in tenants" :key="tenant.id" :value="tenant.id">{{ tenant.display_name }}</option>
              </SearchSelect>
              <SearchSelect v-else disabled>
                <option>{{ workspaceName }}</option>
              </SearchSelect>
            </div>
            <div class="ap-field">
              <label>所属部门</label>
              <input v-model="form.department_name" class="qc-input" type="text" list="product-department-options" placeholder="例如：一部" />
              <datalist id="product-department-options">
                <option v-for="name in departmentNames" :key="name" :value="name"></option>
              </datalist>
            </div>
          </div>
          <div class="form-row">
            <div class="ap-field">
              <label>负责人</label>
              <input v-model="form.owner_name" class="qc-input" type="text" placeholder="选填" />
            </div>
            <div class="ap-field">
              <label>状态</label>
              <SearchSelect v-model="form.status">
                <option value="enabled">启用</option>
                <option value="disabled">停用</option>
              </SearchSelect>
            </div>
          </div>
          <div class="ap-field">
            <label>支持平台</label>
            <div class="platform-picks">
              <label v-for="option in PLATFORM_OPTIONS" :key="option.value" class="platform-pick">
                <input type="checkbox" :checked="form.platforms.includes(option.value)" @change="togglePlatform(option.value)" />
                {{ option.label }}
                <small v-if="option.value === 'ios'" class="qc-muted">（资料）</small>
              </label>
            </div>
          </div>
          <div class="ap-field">
            <label>H5 / Web 访问地址</label>
            <input v-model="form.h5_url" class="qc-input" type="url" placeholder="https://…（H5 或 Web 产品必填）" />
          </div>
        </div>
        <div class="ap-modal-foot">
          <button type="button" class="qc-btn" @click="editorOpen = false">取消</button>
          <button type="button" class="qc-btn qc-btn-primary" :disabled="saving" data-testid="product-save" @click="saveProduct">保存</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.tab-card {
  display: flex;
  gap: 4px;
  padding: 8px;
}

.tab-btn {
  padding: 8px 16px;
  border: 0;
  border-radius: 8px;
  color: var(--workstation-text-muted);
  background: transparent;
  font: inherit;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
}

.tab-btn:hover {
  background: var(--workstation-panel-soft);
}

.tab-btn.active {
  color: #ffffff;
  background: var(--workstation-primary);
}

.filter-card {
  padding: 16px;
}

.filter-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
}

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

.table-wrap {
  overflow-x: auto;
}

.platform-chip {
  margin-right: 4px;
}

.actions {
  white-space: nowrap;
}

.actions .qc-link + .qc-link {
  margin-left: 12px;
}

.qc-link.is-muted {
  color: var(--workstation-text-muted);
}

.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

.platform-picks {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
}

.platform-pick {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
}

.detail-list {
  display: grid;
  grid-template-columns: 96px 1fr;
  gap: 10px 16px;
  margin: 0;
  font-size: 14px;
}

.detail-list dt {
  color: var(--workstation-text-muted);
}

.detail-list dd {
  margin: 0;
  color: var(--workstation-text);
}

.detail-list .break {
  word-break: break-all;
}

.ios-note {
  margin: 16px 0 0;
  font-size: 12px;
}

@media (max-width: 720px) {
  .form-row {
    grid-template-columns: 1fr;
  }
}
</style>
