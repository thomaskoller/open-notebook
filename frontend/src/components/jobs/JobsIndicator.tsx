'use client'

import { useState } from 'react'
import { Activity, Loader2 } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { useActiveJobs } from '@/lib/hooks/use-jobs'
import { useTranslation } from '@/lib/hooks/use-translation'
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip'

import { JobsDrawer } from './JobsDrawer'

/**
 * Sidebar entry point for background work.
 *
 * Always mounted, so a job stays visible no matter which page started it -
 * the gap that made the embeddings rebuild disappear on navigation.
 */
export function JobsIndicator({ isCollapsed = false }: { isCollapsed?: boolean }) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const { activeCount } = useActiveJobs()

  const busy = activeCount > 0
  const label = busy
    ? t('jobs.activeCount', { count: activeCount })
    : t('jobs.title')

  const trigger = (
    <Button
      variant="outline"
      className={cn(
        'sidebar-menu-item w-full gap-2',
        isCollapsed ? 'justify-center' : 'justify-start max-lg:justify-center max-lg:px-2'
      )}
      onClick={() => setOpen(true)}
      aria-label={label}
    >
      {busy ? (
        <Loader2 className="h-4 w-4 shrink-0 animate-spin text-teal" />
      ) : (
        <Activity className="h-4 w-4 shrink-0" />
      )}
      {!isCollapsed && <span className="truncate max-lg:hidden">{label}</span>}
    </Button>
  )

  return (
    <>
      {isCollapsed ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <div>{trigger}</div>
          </TooltipTrigger>
          <TooltipContent side="right">{label}</TooltipContent>
        </Tooltip>
      ) : (
        trigger
      )}
      <JobsDrawer open={open} onOpenChange={setOpen} />
    </>
  )
}
