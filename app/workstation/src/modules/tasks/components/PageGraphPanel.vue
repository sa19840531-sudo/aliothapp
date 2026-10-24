<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { getPageGraphProjection } from '@/services/task-insight.api'
import type { PageGraphProjection } from '@/types/task-insight'

const props = defineProps<{
  taskId: string
  loadingParent?: boolean
}>()

const loading = ref(false)
const loadError = ref('')
const graph = ref<PageGraphProjection | null>(null)

const available = computed(() => graph.value?.status !== 'unavailable' && Boolean(graph.value))

const stateCount = computed(() => graph.value?.states?.length ?? 0)
const transitionCount = computed(() => graph.value?.transitions?.length ?? 0)

const layerSplit = computed(() => {
  const states = graph.value?.states ?? []
  let business = 0
  let overlay = 0
  let external = 0
  for (const state of states) {
    if (state.is_overlay) overlay += 1
    else if (state.is_external) external += 1
    else if (state.counts_as_business_page) business += 1
  }
  return { business, overlay, external }
})

const treeRoots = computed(() => {
  const tree = graph.value?.exploration_tree
  if (!tree || !Array.isArray(tree.roots)) return []
  return tree.roots.slice(0, 12)
})

async function loadGraph(taskId: string) {
  if (!taskId) return
  loading.value = true
  loadError.value = ''
  try {
    graph.value = await getPageGraphProjection(taskId)
  } catch (error) {
    graph.value = null
    loadError.value = error instanceof Error ? error.message : '页面图加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void loadGraph(props.taskId)
})

watch(
  () => props.taskId,
  (next) => {
    void loadGraph(next)
  },
)
</script>

<template>
  <section class="page-graph-panel workstation-glass-panel">
    <header class="panel-head">
      <div>
        <strong>页面状态图</strong>
        <p class="muted">只读诊断 · 不驱动 Completion Gate／调度</p>
      </div>
      <!-- 加载中不下「暂无图事实」的结论，正文已经在说「正在加载」 -->
      <span v-if="!loading && !loadingParent" class="status-pill" :class="available ? 'tone-ok' : 'tone-warn'">
        {{ available ? '可观测' : '暂无图事实' }}
      </span>
    </header>

    <p v-if="loading || loadingParent" class="muted">正在加载页面图…</p>
    <p v-else-if="loadError" class="error-text">{{ loadError }}</p>
    <p v-else-if="graph?.status === 'unavailable'" class="muted">
      {{ graph.reason || '页面图暂不可用' }}
    </p>
    <template v-else-if="available">
      <div class="count-row">
        <span><b>状态</b> {{ stateCount }}</span>
        <span><b>转场</b> {{ transitionCount }}</span>
        <span><b>业务</b> {{ layerSplit.business }}</span>
        <span><b>弹层</b> {{ layerSplit.overlay }}</span>
        <span><b>外跳</b> {{ layerSplit.external }}</span>
      </div>
      <ul v-if="treeRoots.length" class="tree-list">
        <li v-for="(root, index) in treeRoots" :key="String(root.state_id || index)">
          {{ root.label || root.logical_page_key || root.state_id || '未命名状态' }}
          <em v-if="root.page_type">{{ root.page_type }}</em>
        </li>
      </ul>
      <p v-else class="muted">尚无探索树根节点可展示。</p>
    </template>
  </section>
</template>

<style scoped>
.page-graph-panel {
  padding: 16px 18px;
  margin-top: 12px;
}
.panel-head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: flex-start;
  margin-bottom: 12px;
}
.panel-head p {
  margin: 4px 0 0;
  font-size: 12px;
}
.muted {
  color: var(--alioth-text-muted, #6b7280);
}
.error-text {
  color: #b42318;
}
.count-row {
  display: flex;
  flex-wrap: wrap;
  gap: 12px 16px;
  font-size: 13px;
  margin-bottom: 10px;
}
.count-row b {
  margin-right: 4px;
}
.tree-list {
  margin: 0;
  padding-left: 18px;
  font-size: 13px;
  line-height: 1.55;
}
.tree-list em {
  margin-left: 8px;
  font-style: normal;
  color: var(--alioth-text-muted, #6b7280);
  font-size: 12px;
}
.status-pill {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
  white-space: nowrap;
}
.tone-ok {
  background: rgba(16, 185, 129, 0.12);
  color: #047857;
}
.tone-warn {
  background: rgba(245, 158, 11, 0.14);
  color: #b45309;
}
</style>
