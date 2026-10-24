import { requestForm } from './http'

export interface AppBuildUploadResponse {
  platform_build_ref: string
  status: string
  original_filename: string
  sha256: string
  size_bytes: number
  package_name: string | null
  version_name: string | null
  version_code: number | null
}

export function uploadAppBuild(file: File): Promise<AppBuildUploadResponse> {
  const form = new FormData()
  form.append('file', file, file.name)
  return requestForm<AppBuildUploadResponse>('/app-builds', form)
}
