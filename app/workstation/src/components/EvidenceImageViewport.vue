<script setup lang="ts">
import { ref, watch } from 'vue'

const props = defineProps<{ src: string; alt: string; error?: string }>()
const viewport = ref<HTMLElement | null>(null)
const zoom = ref(1)
const fittedWidth = ref<number | null>(null)
const failed = ref(false)

watch(() => props.src, () => {
  zoom.value = 1
  fittedWidth.value = null
  failed.value = false
})

function fitImage(event: Event) {
  const image = event.target as HTMLImageElement
  if (image.getAttribute('src') !== props.src) return
  if (image.naturalWidth && image.naturalHeight) {
    const ratio = Math.min(1, (viewport.value?.clientWidth || 680) / image.naturalWidth, window.innerHeight * 0.6 / image.naturalHeight)
    fittedWidth.value = image.naturalWidth * ratio
  }
}

function imageFailed(event: Event) {
  if ((event.target as HTMLImageElement).getAttribute('src') === props.src) failed.value = true
}
</script>

<template>
  <div class="evidence-image-viewport">
    <div class="zoom-controls">
      <button type="button" class="qc-btn" aria-label="缩小截图" :disabled="zoom <= 0.25" @click="zoom -= 0.25">−</button>
      <span data-testid="screenshot-zoom">{{ Math.round(zoom * 100) }}%</span>
      <button type="button" class="qc-btn" aria-label="放大截图" :disabled="zoom >= 4" @click="zoom += 0.25">＋</button>
      <button type="button" class="qc-btn" aria-label="重置截图缩放" @click="zoom = 1">适应窗口</button>
    </div>
    <div ref="viewport" class="image-scroll-area" data-testid="screenshot-viewport">
      <p v-if="failed || error" class="qc-empty qc-error">{{ error || '截图加载失败，请重试查看或下载原图' }}</p>
      <p v-else-if="!src" class="qc-empty">截图加载中…</p>
      <img v-else :key="src" :src="src" :alt="alt" :style="{ width: fittedWidth ? `${fittedWidth * zoom}px` : '100%' }" @load="fitImage" @error="imageFailed" />
    </div>
  </div>
</template>

<style scoped>
.evidence-image-viewport { flex: 1; min-width: 0; }
.zoom-controls { display: flex; align-items: center; justify-content: center; gap: 8px; margin-bottom: 10px; }
.zoom-controls span { min-width: 48px; text-align: center; font-variant-numeric: tabular-nums; }
.image-scroll-area { max-height: 62vh; overflow: auto; }
.image-scroll-area img { display: block; max-width: none; height: auto; margin: 0 auto; border-radius: 10px; }
</style>
