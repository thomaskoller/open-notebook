import apiClient from './client'
import { Job } from '@/lib/types/jobs'

export interface ListJobsParams {
  /** Only queued/running/retrying jobs. */
  active?: boolean
  command?: string
  status?: string
  limit?: number
}

export const jobsApi = {
  list: async (params: ListJobsParams = {}) => {
    const response = await apiClient.get<Job[]>('/commands/jobs', {
      params: {
        active: params.active,
        command_filter: params.command,
        status_filter: params.status,
        limit: params.limit,
      },
    })
    return response.data
  },

  get: async (jobId: string) => {
    const response = await apiClient.get<Job>(`/commands/jobs/${jobId}`)
    return response.data
  },

  /** Cancel a queued job. Rejects with 409 if it already started or finished. */
  cancel: async (jobId: string) => {
    const response = await apiClient.delete<{ job_id: string; cancelled: boolean }>(
      `/commands/jobs/${jobId}`
    )
    return response.data
  },
}
