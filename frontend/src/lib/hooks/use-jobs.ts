import { useMemo } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { jobsApi } from '@/lib/api/jobs'
import { QUERY_KEYS } from '@/lib/api/query-client'
import { useToast } from '@/lib/hooks/use-toast'
import { useTranslation } from '@/lib/hooks/use-translation'
import { getApiErrorMessage } from '@/lib/utils/error-handler'
import { Job, isJobActive } from '@/lib/types/jobs'

/** Poll while work is in flight; stop entirely when the queue is empty. */
const ACTIVE_POLL_MS = 2000
const IDLE_POLL_MS = 15000

/**
 * How often to re-poll, given what came back last time.
 *
 * Exported so the "stop polling when idle" rule is directly testable - a 2s
 * poll that never stops is a background request on every open tab for the
 * whole session, and nothing can change once the queue has drained.
 */
export function jobsRefetchInterval(
  jobs: Job[] | undefined,
  recent: boolean
): number | false {
  if (jobs?.some(isJobActive)) return ACTIVE_POLL_MS
  // A "recent" listing keeps a slow heartbeat so a job started elsewhere
  // (another tab, the API directly) still shows up. The active-only listing
  // stops dead: nothing is running, so nothing can change.
  return recent ? IDLE_POLL_MS : false
}

/**
 * The whole in-flight picture in one request.
 *
 * Replaces the per-component polling that made a job invisible the moment you
 * navigated away from the page that started it. One query, one cache entry,
 * shared by the indicator and the drawer.
 */
export function useJobs(options?: { recent?: boolean; limit?: number }) {
  const { recent = false, limit = 25 } = options ?? {}

  const query = useQuery({
    queryKey: QUERY_KEYS.jobs({ recent, limit }),
    queryFn: () => jobsApi.list({ active: !recent, limit }),
    // Status rows change server-side on every transition; never serve stale.
    staleTime: 0,
    refetchInterval: (current) =>
      jobsRefetchInterval(current.state.data as Job[] | undefined, recent),
  })

  const jobs = useMemo(() => query.data ?? [], [query.data])
  const activeJobs = useMemo(() => jobs.filter(isJobActive), [jobs])

  return {
    ...query,
    jobs,
    activeJobs,
    activeCount: activeJobs.length,
  }
}

/** Active jobs only - what the sidebar indicator and drawer render. */
export function useActiveJobs() {
  return useJobs({ recent: false })
}

export function useCancelJob() {
  const queryClient = useQueryClient()
  const { toast } = useToast()
  const { t } = useTranslation()

  return useMutation({
    mutationFn: (jobId: string) => jobsApi.cancel(jobId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['jobs'] })
      toast({
        title: t('jobs.cancelled'),
        description: t('jobs.cancelledDesc'),
      })
    },
    onError: (error: unknown) => {
      // 409 means the worker already picked it up - expected, not a bug.
      toast({
        title: t('common.error'),
        description: getApiErrorMessage(error, (key) => t(key), t('jobs.cancelFailed')),
        variant: 'destructive',
      })
      queryClient.invalidateQueries({ queryKey: ['jobs'] })
    },
  })
}
