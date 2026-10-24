import { ref } from 'vue'
import { issueEvidenceFileUrl, issueEvidenceFileTicket } from '@/services/task-insight.api'

/** 同域并发签票上限：避免一次打满浏览器连接池，把缩略图请求饿死在慢接口后面。 */
const MAX_CONCURRENT_TICKETS = 4

/** 管理短时证据地址；地址只绑定单个 artifact，不包含长期登录 JWT。 */
export function useEvidenceFileUrls() {
  const tickets = ref<Record<string, { url: string; expires: number }>>({})
  const pending = new Map<string, Promise<void>>()
  const failed = ref<Record<string, string>>({})
  let generation = 0
  let activeTickets = 0
  const ticketWaiters: Array<() => void> = []

  async function withTicketSlot<T>(run: () => Promise<T>): Promise<T> {
    if (activeTickets >= MAX_CONCURRENT_TICKETS) {
      await new Promise<void>((resolve) => {
        ticketWaiters.push(resolve)
      })
    }
    activeTickets += 1
    try {
      return await run()
    } finally {
      activeTickets -= 1
      ticketWaiters.shift()?.()
    }
  }

  async function load(artifactIds: Iterable<string>, options: { priorityIds?: Iterable<string> } = {}) {
    const priority = new Set([...((options.priorityIds ?? []) as Iterable<string>)].filter(Boolean))
    const unique = [...new Set(artifactIds)].filter(Boolean)
    // 预览区优先签票，其余随后；仍受并发槽位约束。
    const ordered = [
      ...unique.filter((id) => priority.has(id)),
      ...unique.filter((id) => !priority.has(id)),
    ]
    await Promise.all(ordered.map((id) => {
      if ((tickets.value[id]?.expires ?? 0) > Date.now() + 1000) return
      if (pending.has(id)) return pending.get(id)
      const current = generation
      const loading = withTicketSlot(async () => {
        try {
          const ticket = await issueEvidenceFileTicket(id)
          const expires = Date.parse(ticket.expires_at)
          if (!ticket.url || !Number.isFinite(expires) || expires <= Date.now() + 1000) {
            throw new Error('证据访问地址已失效')
          }
          if (current !== generation) return
          tickets.value = { ...tickets.value, [id]: { url: ticket.url, expires } }
          delete failed.value[id]
        } catch (cause) {
          if (current === generation) {
            const reason = cause instanceof Error ? cause.message : ''
            failed.value[id] = `证据访问地址申请失败，请重新打开或下载原图${reason ? `：${reason}` : ''}`
          }
        } finally {
          if (current === generation) pending.delete(id)
        }
      })
      pending.set(id, loading)
      return loading
    }))
  }

  function get(artifactId: string) {
    const ticket = tickets.value[artifactId]
    if (!ticket) return ''
    if (ticket.expires <= Date.now() + 1000) {
      // 只在再次查看过期图时续签一次；拒绝不自动循环重试或绕过对象授权。
      if (!failed.value[artifactId]) void load([artifactId])
      return ''
    }
    return ticket.url
  }

  async function open(artifactId: string, options: { download?: boolean } = {}) {
    const url = await issueEvidenceFileUrl(artifactId, options)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.target = '_blank'
    anchor.rel = 'noopener'
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
  }

  function clear() {
    generation += 1
    tickets.value = {}
    pending.clear()
    failed.value = {}
    // 唤醒排队中的签票槽，让旧 generation 的请求尽快退出，不堵死新任务。
    activeTickets = 0
    const waiters = ticketWaiters.splice(0, ticketWaiters.length)
    for (const wake of waiters) wake()
  }

  return { clear, get, getError: (id: string) => failed.value[id] || '', load, open }
}
