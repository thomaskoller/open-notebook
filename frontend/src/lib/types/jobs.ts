/**
 * Background job types.
 *
 * One vocabulary for every async operation (source processing, embeddings,
 * insights, podcasts). Before this, sources, podcasts, the embeddings rebuild
 * and the generic command endpoint each had their own status strings.
 *
 * Mirrors CommandJobStatusResponse in api/models.py.
 */

export type JobStatus =
  | 'queued'
  | 'running'
  | 'retrying'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'unknown'

/** Statuses where the job is still going to do something. */
export const ACTIVE_JOB_STATUSES: JobStatus[] = ['queued', 'running', 'retrying']

/** Only queued jobs can be cancelled - see ADR-009 (threads worker pool). */
export const CANCELLABLE_JOB_STATUSES: JobStatus[] = ['queued']

export interface JobProgress {
  message?: string
  current?: number
  total?: number
  percent?: number
}

export interface Job {
  job_id: string
  status: JobStatus
  /** Task name, e.g. "embed_source". Null on rows written before migration 26. */
  command: string | null
  /** Celery task id - the handle to look this job up in Flower. */
  task_id: string | null
  result: Record<string, unknown> | null
  error_message: string | null
  progress: JobProgress | null
  attempt: number
  created: string | null
  updated: string | null
}

export function isJobActive(job: Job): boolean {
  return ACTIVE_JOB_STATUSES.includes(job.status)
}

export function isJobCancellable(job: Job): boolean {
  return CANCELLABLE_JOB_STATUSES.includes(job.status)
}
