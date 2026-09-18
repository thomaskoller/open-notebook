'use client'

import { formatDistanceToNow } from 'date-fns'
import {
  AlertCircle,
  Ban,
  CheckCircle2,
  Clock,
  Loader2,
  RotateCcw,
  XCircle,
} from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Progress } from '@/components/ui/progress'
import { useJobs, useCancelJob } from '@/lib/hooks/use-jobs'
import { useTranslation } from '@/lib/hooks/use-translation'
import { Job, JobStatus, isJobCancellable } from '@/lib/types/jobs'

type BadgeVariant = 'default' | 'secondary' | 'destructive' | 'outline'

const STATUS_STYLE: Record<
  JobStatus,
  { icon: typeof Clock; variant: BadgeVariant; spin?: boolean; labelKey: string }
> = {
  queued: { icon: Clock, variant: 'secondary', labelKey: 'jobs.status.queued' },
  running: { icon: Loader2, variant: 'default', spin: true, labelKey: 'jobs.status.running' },
  retrying: { icon: RotateCcw, variant: 'outline', labelKey: 'jobs.status.retrying' },
  completed: { icon: CheckCircle2, variant: 'secondary', labelKey: 'jobs.status.completed' },
  failed: { icon: XCircle, variant: 'destructive', labelKey: 'jobs.status.failed' },
  cancelled: { icon: Ban, variant: 'outline', labelKey: 'jobs.status.cancelled' },
  unknown: { icon: AlertCircle, variant: 'outline', labelKey: 'jobs.status.unknown' },
}

// Written out rather than built with a template literal so the i18n
// unused-key check (src/lib/locales/index.test.ts) can see every key, and so
// an unrecognised task/stage name degrades to its raw value instead of
// rendering a missing-key placeholder.
const COMMAND_LABEL_KEY: Record<string, string> = {
  process_source: 'jobs.commands.process_source',
  run_transformation: 'jobs.commands.run_transformation',
  embed_source: 'jobs.commands.embed_source',
  embed_note: 'jobs.commands.embed_note',
  embed_insight: 'jobs.commands.embed_insight',
  create_insight: 'jobs.commands.create_insight',
  rebuild_embeddings: 'jobs.commands.rebuild_embeddings',
  generate_podcast: 'jobs.commands.generate_podcast',
}

const PROGRESS_LABEL_KEY: Record<string, string> = {
  extracting: 'jobs.progress.extracting',
  transforming: 'jobs.progress.transforming',
  saving: 'jobs.progress.saving',
  resolving_profiles: 'jobs.progress.resolving_profiles',
  generating_outline: 'jobs.progress.generating_outline',
  generating_transcript: 'jobs.progress.generating_transcript',
  combining_audio: 'jobs.progress.combining_audio',
  generating_audio: 'jobs.progress.generating_audio',
  queueing_embeddings: 'jobs.progress.queueing_embeddings',
}

function relativeTime(value: string | null): string | null {
  if (!value) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return null
  return formatDistanceToNow(date, { addSuffix: true })
}

function JobRow({ job }: { job: Job }) {
  const { t } = useTranslation()
  const cancelJob = useCancelJob()

  const style = STATUS_STYLE[job.status] ?? STATUS_STYLE.unknown
  const Icon = style.icon
  const when = relativeTime(job.updated ?? job.created)

  // `command` is null only for rows written before migration 26.
  const commandKey = job.command ? COMMAND_LABEL_KEY[job.command] : undefined
  const name = commandKey
    ? t(commandKey)
    : (job.command ?? t('jobs.unknownCommand'))

  const progress = job.progress
  const percent =
    progress?.percent ??
    (progress?.total && progress.total > 0 && progress.current !== undefined
      ? Math.round((progress.current / progress.total) * 100)
      : undefined)

  return (
    <div className="space-y-2 rounded-lg border p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <p className="truncate text-sm font-medium">{name}</p>
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <Badge variant={style.variant} className="gap-1">
              <Icon className={`h-3 w-3 ${style.spin ? 'animate-spin' : ''}`} />
              {t(style.labelKey)}
            </Badge>
            {job.attempt > 0 && (
              <span>{t('jobs.attempt', { count: job.attempt })}</span>
            )}
            {when && <span>{when}</span>}
          </div>
        </div>

        {isJobCancellable(job) && (
          <Button
            variant="ghost"
            size="sm"
            disabled={cancelJob.isPending}
            onClick={() => cancelJob.mutate(job.job_id)}
          >
            {t('common.cancel')}
          </Button>
        )}
      </div>

      {progress?.message && (
        <p className="text-xs text-muted-foreground">
          {PROGRESS_LABEL_KEY[progress.message]
            ? t(PROGRESS_LABEL_KEY[progress.message])
            : progress.message}
          {progress.total ? ` — ${progress.current ?? 0}/${progress.total}` : ''}
        </p>
      )}

      {percent !== undefined && <Progress value={percent} className="h-1.5" />}

      {job.error_message && (
        <p className="break-words text-xs text-destructive">{job.error_message}</p>
      )}
    </div>
  )
}

export function JobsDrawer({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const { t } = useTranslation()
  // Recent listing (not active-only) so a job that just finished or failed is
  // still on screen - a job that vanishes the instant it fails tells the user
  // nothing.
  const { jobs, activeCount, isLoading } = useJobs({ recent: true, limit: 25 })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{t('jobs.title')}</DialogTitle>
          <DialogDescription>
            {activeCount > 0
              ? t('jobs.activeCount', { count: activeCount })
              : t('jobs.noneActive')}
          </DialogDescription>
        </DialogHeader>

        <div className="max-h-[60vh] overflow-y-auto overscroll-contain pr-3">
          <div className="space-y-2">
            {isLoading && (
              <p className="py-6 text-center text-sm text-muted-foreground">
                {t('common.loading')}
              </p>
            )}
            {!isLoading && jobs.length === 0 && (
              <p className="py-6 text-center text-sm text-muted-foreground">
                {t('jobs.empty')}
              </p>
            )}
            {jobs.map((job) => (
              <JobRow key={job.job_id} job={job} />
            ))}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
