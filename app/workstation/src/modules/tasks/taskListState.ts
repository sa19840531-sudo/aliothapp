import type { LocationQuery, LocationQueryRaw } from 'vue-router'
import type { TaskStatus } from '@/types/task'

export interface TaskListFilters {
  taskName: string
  department: string
  productCode: string
  platform: string
  submitterName: string
  status: TaskStatus | 'all'
  createdStart: string
  createdEnd: string
}

const statuses: ReadonlyArray<TaskStatus> = ['queued', 'running', 'canceling', 'review', 'done', 'failed', 'canceled']

function queryText(query: LocationQuery, key: string) {
  const value = query[key]
  return typeof value === 'string' ? value : ''
}

function dateText(query: LocationQuery, key: string) {
  const value = queryText(query, key)
  return /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : ''
}

/** 列表状态只读取已声明的筛选参数，详情页的其他参数不会带回列表。 */
export function readTaskListState(query: LocationQuery = {}) {
  const status = queryText(query, 'status')
  const requestedPage = Number(queryText(query, 'page'))
  const requestedPageSize = Number(queryText(query, 'page_size'))
  return {
    filters: {
      taskName: queryText(query, 'task_name'),
      department: queryText(query, 'department_name'),
      productCode: queryText(query, 'product_code'),
      platform: ['h5', 'web', 'app'].includes(queryText(query, 'target_platform'))
        ? queryText(query, 'target_platform')
        : '',
      submitterName: queryText(query, 'submitter_name'),
      status: statuses.includes(status as TaskStatus) ? status as TaskStatus : 'all',
      createdStart: dateText(query, 'created_from'),
      createdEnd: dateText(query, 'created_to'),
    } satisfies TaskListFilters,
    page: Number.isSafeInteger(requestedPage) && requestedPage > 0 ? requestedPage : 1,
    pageSize: [10, 20, 50].includes(requestedPageSize) ? requestedPageSize : 20,
  }
}

export function taskListQuery(filters: TaskListFilters, page: number, pageSize: number): LocationQueryRaw {
  const query: LocationQueryRaw = { page: String(page), page_size: String(pageSize) }
  const values = {
    task_name: filters.taskName.trim(),
    department_name: filters.department,
    product_code: filters.productCode,
    target_platform: filters.platform,
    submitter_name: filters.submitterName.trim(),
    status: filters.status === 'all' ? '' : filters.status,
    created_from: filters.createdStart,
    created_to: filters.createdEnd,
  }
  for (const [key, value] of Object.entries(values)) {
    if (value) query[key] = value
  }
  return query
}

export function taskListReturnQuery(query: LocationQuery = {}) {
  if (!Object.keys(query).some((key) => ['page', 'page_size', 'task_name', 'product_code', 'status', 'department_name', 'target_platform', 'submitter_name', 'created_from', 'created_to'].includes(key))) {
    return undefined
  }
  const state = readTaskListState(query)
  return taskListQuery(state.filters, state.page, state.pageSize)
}
