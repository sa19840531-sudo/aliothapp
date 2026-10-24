import type { H5ShortflowPlan } from '@/services/h5-shortflows.api'
import type { RedactionArtifact } from '@/services/evidence-redactions.api'

/** 导出闭集只使用本次绑定，不把历史审阅材料补进当前导出。 */
export function shortflowRedactionArtifacts(plan: H5ShortflowPlan) {
  const artifacts = new Map<string, RedactionArtifact>()
  let blockedReason = ''
  for (const checkpoint of plan.checkpoints) for (const attempt of checkpoint.attempts) for (const binding of attempt.bindings) {
    if (binding.integrity !== 'current') blockedReason = '绑定材料已变化，请刷新台账并核对后再导出。'
    for (const artifact of binding.artifacts) {
      const item = { ...artifact, manifest_id: binding.manifest_id, manifest_version: binding.manifest_version }
      const previous = artifacts.get(artifact.artifact_id)
      if (previous && JSON.stringify(previous) !== JSON.stringify(item)) blockedReason = '同一材料对应多个不同的 Manifest 身份，请核对绑定后再导出。'
      artifacts.set(artifact.artifact_id, item)
    }
  }
  return { artifacts: [...artifacts.values()], blockedReason }
}
