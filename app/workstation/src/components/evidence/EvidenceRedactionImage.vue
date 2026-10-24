<script setup lang="ts">
import { computed, ref } from 'vue'

const props = defineProps<{ src: string; regions: Array<{ x: number; y: number; width: number; height: number }> }>()
const emit = defineEmits<{ size: [width: number, height: number]; region: [region: { x: number; y: number; width: number; height: number }] }>()
const image = ref<HTMLImageElement | null>(null)
const width = ref(0)
const height = ref(0)
const start = ref<{ x: number; y: number; pointer: number }>()
const end = ref<{ x: number; y: number }>()
const draft = computed(() => start.value && end.value ? rectangle(start.value, end.value) : null)
const rectangles = computed(() => [...props.regions, ...(draft.value ? [draft.value] : [])])

function loaded(event: Event) {
  const target = event.target as HTMLImageElement
  width.value = target.naturalWidth; height.value = target.naturalHeight
  emit('size', width.value, height.value)
}

function point(event: PointerEvent) {
  const bounds = image.value?.getBoundingClientRect()
  if (!bounds?.width || !bounds.height || !width.value || !height.value) return
  return { x: Math.max(0, Math.min(width.value, (event.clientX - bounds.left) / bounds.width * width.value)),
    y: Math.max(0, Math.min(height.value, (event.clientY - bounds.top) / bounds.height * height.value)) }
}

function rectangle(a: { x: number; y: number }, b: { x: number; y: number }) {
  const x = Math.floor(Math.min(a.x, b.x)); const y = Math.floor(Math.min(a.y, b.y))
  return { x, y, width: Math.ceil(Math.max(a.x, b.x)) - x, height: Math.ceil(Math.max(a.y, b.y)) - y }
}

function begin(event: PointerEvent) {
  if (event.button !== 0) return
  const position = point(event)
  if (!position) return
  start.value = { ...position, pointer: event.pointerId }; end.value = position
  ;(event.currentTarget as HTMLElement).setPointerCapture?.(event.pointerId)
}

function move(event: PointerEvent) {
  if (start.value?.pointer !== event.pointerId) return
  end.value = point(event)
}

function finish(event: PointerEvent) {
  if (start.value?.pointer !== event.pointerId) return
  end.value = point(event)
  const region = draft.value
  start.value = undefined; end.value = undefined
  if (region && region.width > 0 && region.height > 0) emit('region', region)
}
</script>

<template>
  <div class="image-editor" @pointerdown.prevent="begin" @pointermove="move" @pointerup="finish" @pointercancel="start = undefined; end = undefined" data-testid="redaction-image-editor">
    <img ref="image" :src="src" alt="脱敏编辑原件，可拖动框选区域" draggable="false" @load="loaded" />
    <span v-for="(region, index) in rectangles" :key="index" class="region" :style="{ left: `${region.x / width * 100}%`, top: `${region.y / height * 100}%`, width: `${region.width / width * 100}%`, height: `${region.height / height * 100}%` }" aria-hidden="true" />
  </div>
</template>

<style scoped>
.image-editor { display: inline-block; position: relative; max-width: 100%; touch-action: none; cursor: crosshair; }
img { display: block; max-width: 100%; max-height: 60vh; object-fit: contain; user-select: none; }
.region { position: absolute; box-sizing: border-box; border: 2px solid #d73d3d; background: rgb(0 0 0 / 45%); pointer-events: none; }
</style>
