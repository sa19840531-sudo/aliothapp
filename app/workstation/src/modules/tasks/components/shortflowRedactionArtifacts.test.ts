import { expect, it } from 'vitest'
import { shortflowPlan } from '@/test/shortflowFixtures'
import { shortflowRedactionArtifacts } from './shortflowRedactionArtifacts'

it('只取当前绑定且按身份去重，不把历史审阅材料加入包', () => {
  const plan = shortflowPlan()
  const binding = plan.checkpoints[0]!.attempts[0]!.bindings[0]!
  const first = binding.artifacts[0]!
  binding.artifacts.push({ ...first })
  binding.manifest_versions = [{ manifest_id: 'historical', manifest_version: 1, artifacts: [{ ...first, artifact_id: 'old-only' }] } as never]
  const { artifacts, blockedReason } = shortflowRedactionArtifacts(plan)
  expect(artifacts.filter((item) => item.artifact_id === first.artifact_id)).toHaveLength(1)
  expect(artifacts.some((item) => item.artifact_id === 'old-only')).toBe(false)
  expect(artifacts.find((item) => item.artifact_id === first.artifact_id)).toMatchObject({ manifest_id: binding.manifest_id, manifest_version: binding.manifest_version })
  expect(blockedReason).toBe('')
})

it('同材料跨 Manifest 或失效绑定阻断导出', () => {
  const plan = shortflowPlan()
  const attempt = plan.checkpoints[0]!.attempts[0]!
  const binding = attempt.bindings[0]!
  attempt.bindings.push({ ...binding, manifest_id: 'foreign-manifest', artifacts: [...binding.artifacts] })
  expect(shortflowRedactionArtifacts(plan).blockedReason).toContain('多个不同')
  attempt.bindings.pop()
  binding.integrity = 'changed'
  expect(shortflowRedactionArtifacts(plan).blockedReason).toContain('绑定材料已变化')
})
