import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'

import { jobsRefetchInterval, useJobs } from './use-jobs'
import { jobsApi } from '@/lib/api/jobs'
import type { Job, JobStatus } from '@/lib/types/jobs'

vi.mock('@/lib/api/jobs', () => ({
  jobsApi: { list: vi.fn(), get: vi.fn(), cancel: vi.fn() },
}))

vi.mock('@/lib/hooks/use-translation', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}))

function job(status: JobStatus): Job {
  return {
    job_id: `command:${status}`,
    status,
    command: 'embed_source',
    task_id: 'task-1',
    result: null,
    error_message: null,
    progress: null,
    attempt: 0,
    created: null,
    updated: null,
  }
}

function wrapper({ children }: { children: React.ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

describe('useJobs', () => {
  beforeEach(() => vi.clearAllMocks())

  it('requests only active jobs by default', async () => {
    vi.mocked(jobsApi.list).mockResolvedValue([])

    renderHook(() => useJobs(), { wrapper })

    await waitFor(() =>
      expect(jobsApi.list).toHaveBeenCalledWith({ active: true, limit: 25 })
    )
  })

  it('requests the recent listing when asked, so finished jobs stay visible', async () => {
    vi.mocked(jobsApi.list).mockResolvedValue([])

    renderHook(() => useJobs({ recent: true, limit: 10 }), { wrapper })

    await waitFor(() =>
      expect(jobsApi.list).toHaveBeenCalledWith({ active: false, limit: 10 })
    )
  })

  it('counts queued, running and retrying jobs as active', async () => {
    vi.mocked(jobsApi.list).mockResolvedValue([
      job('queued'),
      job('running'),
      job('retrying'),
      job('completed'),
      job('failed'),
      job('cancelled'),
    ])

    const { result } = renderHook(() => useJobs({ recent: true }), { wrapper })

    await waitFor(() => expect(result.current.jobs).toHaveLength(6))
    expect(result.current.activeCount).toBe(3)
    expect(result.current.activeJobs.map((j) => j.status)).toEqual([
      'queued',
      'running',
      'retrying',
    ])
  })

})

describe('jobsRefetchInterval', () => {
  it('polls fast while any job is in flight', () => {
    expect(jobsRefetchInterval([job('completed'), job('running')], false)).toBe(2000)
    expect(jobsRefetchInterval([job('queued')], true)).toBe(2000)
  })

  it('stops polling the active listing once the queue drains', () => {
    // A 2s poll that never stops is a background request on every open tab for
    // the whole session, and nothing can change once nothing is running.
    expect(jobsRefetchInterval([], false)).toBe(false)
    expect(jobsRefetchInterval([job('completed')], false)).toBe(false)
  })

  it('keeps a slow heartbeat on the recent listing', () => {
    // So a job started in another tab (or straight against the API) appears.
    expect(jobsRefetchInterval([job('failed')], true)).toBe(15000)
    expect(jobsRefetchInterval(undefined, true)).toBe(15000)
  })
})
