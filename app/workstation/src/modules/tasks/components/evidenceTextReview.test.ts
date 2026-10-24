/// <reference types="node" />
import { webcrypto } from 'node:crypto'
import { afterEach, expect, it, vi } from 'vitest'
import type { H5ShortflowArtifact } from '@/services/h5-shortflows.api'
import { isReadableEvidence, observationSampleReview, observationSamples, readEvidenceText } from './evidenceTextReview'

afterEach(() => vi.unstubAllGlobals())

it('连续样本依真实单调时间排列且保留精确包身份和重复摘要图片', () => {
  const artifact = { artifact_type: 'screenshot', checksum: 'sha256:same' }
  const samples = observationSamples(JSON.stringify({ schema_version: 'checkpoint-observation.v1', samples: [
    { observed_at_ms: 1600, screenshot_sha256: 'same' },
    { observed_at_ms: 1000, screenshot_sha256: 'missing' },
    { observed_at_ms: 1600, screenshot_sha256: 'same' },
  ] }), 'packet-1', [{ ...artifact, artifact_id: 'a' }, { ...artifact, artifact_id: 'b' }] as H5ShortflowArtifact[])
  expect(samples.map((sample) => sample.id)).toEqual(['packet-1:sample:1', 'packet-1:sample:0', 'packet-1:sample:2'])
  expect(samples.map((sample) => sample.elapsed_ms)).toEqual([0, 600, 600])
  expect(samples[0]?.screenshot_ids).toEqual([])
  expect(samples[1]?.screenshot_ids).toEqual(['a', 'b'])
})

it('没有真实采样协议与时间不制造连续样本', () => {
  expect(observationSamples('{"samples":[{}]}', 'p', [])).toEqual([])
  expect(observationSamples('{"schema_version":"checkpoint-observation.v1","samples":[{"observed_at_ms":null}]}', 'p', [])).toEqual([])
  expect(observationSampleReview('{"schema_version":"checkpoint-observation.v1","samples":[{"observed_at_ms":null}]}', 'p', []).notice).toContain('1 项缺少有效时间或截图摘要')
  expect(observationSampleReview('{"schema_version":"checkpoint-observation.v1","samples":null}', 'p', []).notice).toContain('不能还原实际时序')
})

it('原件读取逐字校验大小和摘要，不接收内容漂移或超限文本', async () => {
  vi.stubGlobal('crypto', webcrypto)
  const content = '<script>window.canary = 1</script>'
  const bytes = new TextEncoder().encode(content)
  const checksum = Buffer.from(await webcrypto.subtle.digest('SHA-256', bytes)).toString('hex')
  const artifact: H5ShortflowArtifact = { artifact_id: 'dom', artifact_type: 'dom', content_type: 'text/html', size_bytes: bytes.length, checksum: `sha256:${checksum}` }
  vi.stubGlobal('fetch', vi.fn(async () => new Response(bytes)))
  expect(await readEvidenceText('/signed', artifact, new AbortController().signal)).toBe(content)
  await expect(readEvidenceText('/signed', { ...artifact, checksum: 'sha256:wrong' }, new AbortController().signal)).rejects.toThrow('摘要')
  await expect(readEvidenceText('/signed', { ...artifact, size_bytes: 1 }, new AbortController().signal)).rejects.toThrow('大小')
  expect(isReadableEvidence({ ...artifact, size_bytes: 512 * 1024 + 1 })).toBe(false)
})
