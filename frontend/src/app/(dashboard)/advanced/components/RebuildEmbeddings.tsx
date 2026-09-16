'use client'

import { useState, useMemo } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Progress } from '@/components/ui/progress'
import { Loader2, AlertCircle, CheckCircle2, XCircle, Clock } from 'lucide-react'
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from '@/components/ui/accordion'
import { embeddingApi } from '@/lib/api/embedding'
import type { RebuildEmbeddingsRequest } from '@/lib/api/embedding'
import { useJobs } from '@/lib/hooks/use-jobs'
import { useTranslation } from '@/lib/hooks/use-translation'

export function RebuildEmbeddings() {
  const { t } = useTranslation()
  const [mode, setMode] = useState<'existing' | 'all'>('existing')
  const [includeSources, setIncludeSources] = useState(true)
  const [includeNotes, setIncludeNotes] = useState(true)
  const [includeInsights, setIncludeInsights] = useState(true)
  const [dismissedId, setDismissedId] = useState<string | null>(null)

  // A rebuild outlives this component: it used to live in a local setInterval,
  // so navigating away and back lost the run entirely. Adopt whatever rebuild
  // the server says is in flight instead of remembering it here - that also
  // picks up a rebuild started in another tab.
  const queryClient = useQueryClient()
  const { jobs } = useJobs({ recent: true, limit: 25 })
  const serverCommandId = useMemo(() => {
    const job = jobs.find((j) => j.command === 'rebuild_embeddings')
    return job && job.job_id !== dismissedId ? job.job_id : null
  }, [jobs, dismissedId])

  const rebuildMutation = useMutation({
    mutationFn: async (request: RebuildEmbeddingsRequest) => {
      return embeddingApi.rebuildEmbeddings(request)
    },
    onSuccess: () => {
      // Wake the jobs poll: it stops entirely while the queue is empty (see
      // jobsRefetchInterval), so the rebuild would not reach the sidebar
      // indicator until some unrelated refetch happened.
      queryClient.invalidateQueries({ queryKey: ['jobs'] })
    },
  })

  const commandId = rebuildMutation.data?.command_id ?? serverCommandId

  const { data: status = null } = useQuery({
    queryKey: ['embeddings', 'rebuild', commandId],
    queryFn: () => embeddingApi.getRebuildStatus(commandId as string),
    enabled: !!commandId,
    staleTime: 0,
    refetchInterval: (current) => {
      const data = current.state.data
      if (!data) return 5000
      return data.status === 'completed' || data.status === 'failed' ? false : 5000
    },
  })

  const handleStartRebuild = () => {
    const request: RebuildEmbeddingsRequest = {
      mode,
      include_sources: includeSources,
      include_notes: includeNotes,
      include_insights: includeInsights
    }

    rebuildMutation.mutate(request)
  }

  const handleReset = () => {
    // Hide the finished run without deleting its job record - the jobs drawer
    // keeps showing it.
    if (commandId) setDismissedId(commandId)
    rebuildMutation.reset()
  }

  const isAnyTypeSelected = includeSources || includeNotes || includeInsights
  const isRebuildActive = commandId && status && (status.status === 'queued' || status.status === 'running')

  const progressData = status?.progress
  const stats = status?.stats

  const totalItems = progressData?.total_items ?? progressData?.total ?? 0
  const processedItems = progressData?.processed_items ?? progressData?.processed ?? 0
  const derivedProgressPercent = progressData?.percentage ?? (totalItems > 0 ? (processedItems / totalItems) * 100 : 0)
  const progressPercent = Number.isFinite(derivedProgressPercent) ? derivedProgressPercent : 0

  const sourcesProcessed = stats?.sources_processed ?? stats?.sources ?? 0
  const notesProcessed = stats?.notes_processed ?? stats?.notes ?? 0
  const insightsProcessed = stats?.insights_processed ?? stats?.insights ?? 0
  const failedItems = stats?.failed_items ?? stats?.failed ?? 0

  const computedDuration = status?.started_at && status?.completed_at
    ? (new Date(status.completed_at).getTime() - new Date(status.started_at).getTime()) / 1000
    : undefined
  const processingTimeSeconds = stats?.processing_time ?? computedDuration

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          {t('advanced.rebuildEmbeddings')}
        </CardTitle>
        <CardDescription>
          {t('advanced.rebuildEmbeddingsDesc')}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        {/* Configuration Form */}
        {!isRebuildActive && (
          <div className="space-y-6">
            <div className="space-y-3">
              <Label htmlFor="mode">{t('advanced.rebuild.mode')}</Label>
              <Select value={mode} onValueChange={(value) => setMode(value as 'existing' | 'all')}>
                <SelectTrigger id="mode">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="existing">{t('advanced.rebuild.existing')}</SelectItem>
                  <SelectItem value="all">{t('advanced.rebuild.all')}</SelectItem>
                </SelectContent>
              </Select>
              <p className="text-sm text-muted-foreground">
                {mode === 'existing'
                  ? t('advanced.rebuild.existingDesc')
                  : t('advanced.rebuild.allDesc')}
              </p>
            </div>

            <div className="space-y-3" role="group" aria-labelledby="include-label">
              <span id="include-label" className="text-sm font-medium leading-none">{t('advanced.rebuild.include')}</span>
              <div className="space-y-3">
                <div className="flex items-center space-x-2">
                  <Checkbox
                    id="sources"
                    checked={includeSources}
                    onCheckedChange={(checked) => setIncludeSources(checked === true)}
                  />
                  <Label htmlFor="sources" className="font-normal cursor-pointer">
                    {t('navigation.sources')}
                  </Label>
                </div>
                <div className="flex items-center space-x-2">
                  <Checkbox
                    id="notes"
                    checked={includeNotes}
                    onCheckedChange={(checked) => setIncludeNotes(checked === true)}
                  />
                  <Label htmlFor="notes" className="font-normal cursor-pointer">
                    {t('common.notes')}
                  </Label>
                </div>
                <div className="flex items-center space-x-2">
                  <Checkbox
                    id="insights"
                    checked={includeInsights}
                    onCheckedChange={(checked) => setIncludeInsights(checked === true)}
                  />
                  <Label htmlFor="insights" className="font-normal cursor-pointer">
                    {t('common.insights')}
                  </Label>
                </div>
              </div>
              {!isAnyTypeSelected && (
                <Alert variant="destructive">
                  <AlertCircle className="h-4 w-4" />
                  <AlertDescription>
                    {t('advanced.rebuild.selectOneError')}
                  </AlertDescription>
                </Alert>
              )}
            </div>

            <Button
              onClick={handleStartRebuild}
              disabled={!isAnyTypeSelected || rebuildMutation.isPending}
              className="w-full"
            >
              {rebuildMutation.isPending ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  {t('advanced.rebuild.starting')}
                </>
              ) : (
                t('advanced.rebuild.startBtn')
              )}
            </Button>

            {rebuildMutation.isError && (
              <Alert variant="destructive">
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>
                  {t('advanced.rebuild.failed')}: {(rebuildMutation.error as Error)?.message || t('common.error')}
                </AlertDescription>
              </Alert>
            )}
          </div>
        )}

        {/* Status Display */}
        {status && (
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                {status.status === 'queued' && <Clock className="h-5 w-5 text-warn" />}
                {status.status === 'running' && <Loader2 className="h-5 w-5 text-teal animate-spin" />}
                {status.status === 'completed' && <CheckCircle2 className="h-5 w-5 text-fern" />}
                {status.status === 'failed' && <XCircle className="h-5 w-5 text-destructive" />}
                <div className="flex flex-col">
                  <span className="font-medium">
                    {status.status === 'queued' && t('advanced.rebuild.queued')}
                    {status.status === 'running' && t('advanced.rebuild.running')}
                    {status.status === 'completed' && t('advanced.rebuild.completed')}
                    {status.status === 'failed' && t('advanced.rebuild.failed')}
                  </span>
                  {status.status === 'running' && (
                    <span className="text-sm text-muted-foreground">
                      {t('advanced.rebuild.leavePageHint')}
                    </span>
                  )}
                </div>
              </div>
              {(status.status === 'completed' || status.status === 'failed') && (
                <Button variant="outline" size="sm" onClick={handleReset}>
                  {t('advanced.rebuild.startNew')}
                </Button>
              )}
            </div>

            {progressData && (
              <div className="space-y-2">
                <div className="flex justify-between text-sm">
                  <span>{t('common.progress')}</span>
                  <span className="font-medium">
                    {t('advanced.rebuild.itemsProcessed', { processed: processedItems.toString(), total: totalItems.toString(), percent: progressPercent.toFixed(1) })}
                  </span>
                </div>
                <Progress value={progressPercent} className="h-2" />
                {failedItems > 0 && (
                  <p className="text-sm text-warn">
                    ⚠️ {t('advanced.rebuild.failedItems', { count: failedItems })}
                  </p>
                )}
              </div>
            )}

             {stats && (
              <div className="grid grid-cols-4 gap-4">
                <div className="space-y-1">
                  <p className="text-sm text-muted-foreground">{t('navigation.sources')}</p>
                  <p className="font-mono text-2xl font-bold">{sourcesProcessed}</p>
                </div>
                <div className="space-y-1">
                  <p className="text-sm text-muted-foreground">{t('common.notes')}</p>
                  <p className="font-mono text-2xl font-bold">{notesProcessed}</p>
                </div>
                <div className="space-y-1">
                  <p className="text-sm text-muted-foreground">{t('common.insights')}</p>
                  <p className="font-mono text-2xl font-bold">{insightsProcessed}</p>
                </div>
                <div className="space-y-1">
                  <p className="text-sm text-muted-foreground">{t('advanced.rebuild.time')}</p>
                  <p className="font-mono text-2xl font-bold">
                    {processingTimeSeconds !== undefined ? `${processingTimeSeconds.toFixed(1)}s` : '—'}
                  </p>
                </div>
              </div>
            )}

            {status.error_message && (
              <Alert variant="destructive">
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>{status.error_message}</AlertDescription>
              </Alert>
            )}

            {status.started_at && (
              <div className="text-sm text-muted-foreground space-y-1">
                <p>{t('common.created', { time: new Date(status.started_at).toLocaleString() })}</p>
                {status.completed_at && (
                  <p>{t('notebooks.updated')}: {new Date(status.completed_at).toLocaleString()}</p>
                )}
              </div>
            )}
          </div>
        )}

        {/* Help Section */}
         <Accordion type="single" collapsible className="w-full">
          <AccordionItem value="when">
            <AccordionTrigger>{t('advanced.rebuild.whenToRebuild')}</AccordionTrigger>
            <AccordionContent className="space-y-2 text-sm">
              <p>{t('advanced.rebuild.whenToRebuildAns')}</p>
            </AccordionContent>
          </AccordionItem>

          <AccordionItem value="time">
            <AccordionTrigger>{t('advanced.rebuild.howLong')}</AccordionTrigger>
            <AccordionContent className="space-y-2 text-sm">
              <p>{t('advanced.rebuild.howLongAns')}</p>
            </AccordionContent>
          </AccordionItem>

          <AccordionItem value="safe">
            <AccordionTrigger>{t('advanced.rebuild.isSafe')}</AccordionTrigger>
            <AccordionContent className="space-y-2 text-sm">
              <p>{t('advanced.rebuild.isSafeAns')}</p>
            </AccordionContent>
          </AccordionItem>
        </Accordion>
      </CardContent>
    </Card>
  )
}
