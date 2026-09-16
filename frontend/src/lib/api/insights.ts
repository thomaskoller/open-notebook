import apiClient from './client'

export interface SourceInsightResponse {
  id: string
  source_id: string
  insight_type: string
  content: string
  // Insights created before backend migration 19 have no timestamps
  created: string | null
  updated: string | null
}

export interface CreateSourceInsightRequest {
  transformation_id: string
}

export interface InsightCreationResponse {
  status: 'pending'
  message: string
  source_id: string
  transformation_id: string
  command_id?: string
}

/** @deprecated use Job from '@/lib/types/jobs' - kept for this module's callers */
export interface CommandJobStatusResponse {
  job_id: string
  status: string
  result?: Record<string, unknown>
  error_message?: string
}

export type CommandOutcome =
  | { outcome: 'completed' }
  | { outcome: 'failed'; errorMessage?: string }
  | { outcome: 'timeout' }

export const insightsApi = {
  listForSource: async (sourceId: string) => {
    const response = await apiClient.get<SourceInsightResponse[]>(`/sources/${sourceId}/insights`)
    return response.data
  },

  get: async (insightId: string) => {
    const response = await apiClient.get<SourceInsightResponse>(`/insights/${insightId}`)
    return response.data
  },

  create: async (sourceId: string, data: CreateSourceInsightRequest) => {
    const response = await apiClient.post<InsightCreationResponse>(
      `/sources/${sourceId}/insights`,
      data
    )
    return response.data
  },

  delete: async (insightId: string) => {
    await apiClient.delete(`/insights/${insightId}`)
  },

  getCommandStatus: async (commandId: string) => {
    const response = await apiClient.get<CommandJobStatusResponse>(
      `/commands/jobs/${commandId}`
    )
    return response.data
  },

  /**
   * Poll command status until it reaches a terminal state.
   *
   * Returns *why* it ended, not just a boolean: a failed job's message is the
   * only thing that tells the user what went wrong, and callers used to
   * discard it (failures reached console.error and nothing else).
   */
  waitForCommand: async (
    commandId: string,
    options?: { maxAttempts?: number; intervalMs?: number }
  ): Promise<CommandOutcome> => {
    const maxAttempts = options?.maxAttempts ?? 60 // Default 60 attempts
    const intervalMs = options?.intervalMs ?? 2000 // Default 2 seconds

    for (let i = 0; i < maxAttempts; i++) {
      try {
        const status = await insightsApi.getCommandStatus(commandId)
        if (status.status === 'completed') {
          return { outcome: 'completed' }
        }
        if (status.status === 'failed' || status.status === 'cancelled') {
          return { outcome: 'failed', errorMessage: status.error_message }
        }
        // Still queued/running/retrying - wait and poll again
        await new Promise(resolve => setTimeout(resolve, intervalMs))
      } catch (error) {
        console.error('Error checking command status:', error)
        // Continue polling on error
        await new Promise(resolve => setTimeout(resolve, intervalMs))
      }
    }
    return { outcome: 'timeout' }
  }
}