import React, { useEffect, useRef, useState } from 'react'
import { Card } from 'primereact/card'
import { Tag } from 'primereact/tag'
import { ProgressBar } from 'primereact/progressbar'
import { Button } from 'primereact/button'
import { parseProgress, fmtDur } from '../progress.js'
import { cancelJob } from '../api.js'

const STATUS_SEVERITY = {
  queued: 'secondary',
  processing: 'info',
  done: 'success',
  error: 'danger',
}

export default function ProgressPanel({ job, jobId }) {
  const logRef = useRef(null)
  const log = job?.log || []
  const status = job?.status || 'queued'
  const working = status === 'processing' || status === 'queued'
  const [cancelling, setCancelling] = useState(false)

  // Live elapsed clock — ticks every second while working.
  const startRef = useRef(null)
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (working && startRef.current == null) startRef.current = Date.now()
    if (!working) return
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [working])

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [log.length])

  if (!job) return null

  const { percent, eta } = parseProgress(job)
  const elapsed = startRef.current ? (now - startRef.current) / 1000 : 0
  const showPct = working && percent != null
  const etaTxt = working && eta != null ? fmtDur(eta) : null
  const cancelledByUser = status === 'error' && /cancelled by user/i.test(job.error || '')
  const stopping = cancelling || /^cancelling/i.test(job.progress || '')

  async function onCancel() {
    if (!jobId) return
    setCancelling(true)
    try { await cancelJob(jobId) } catch { /* ignore */ }
  }

  return (
    <Card className="ta-mt ta-w-full">
      <div className="ta-flex" style={{ justifyContent: 'space-between', marginBottom: '0.5rem' }}>
        <span className="ta-section-title" style={{ margin: 0 }}>Progress</span>
        <div className="ta-flex">
          <Tag
            severity={cancelledByUser ? 'warning' : (STATUS_SEVERITY[status] || 'secondary')}
            value={cancelledByUser ? 'stopped' : status}
          />
          {working && (
            <Button
              label={stopping ? 'Stopping…' : 'Stop'}
              icon="pi pi-stop-circle"
              severity="danger" outlined size="small"
              disabled={stopping}
              onClick={onCancel}
            />
          )}
        </div>
      </div>

      <ProgressBar
        value={showPct ? percent : undefined}
        mode={showPct ? 'determinate' : 'indeterminate'}
        showValue={showPct}
        style={{ height: '0.9rem', marginBottom: '0.5rem' }}
      />

      {/* Real-time ETA + % + elapsed */}
      <div className="ta-flex ta-small" style={{ marginBottom: '0.5rem' }}>
        {showPct && <span><strong>{percent}%</strong> complete</span>}
        {etaTxt && <span>⏳ ≈ <strong>{etaTxt}</strong> remaining</span>}
        {working && <span className="ta-muted">elapsed {fmtDur(elapsed)}</span>}
        {status === 'done' && <span style={{ color: 'var(--green-500)' }}>✅ finished in {fmtDur(elapsed)}</span>}
        {cancelledByUser && <span style={{ color: 'var(--orange-500)' }}>⏹ stopped after {fmtDur(elapsed)}</span>}
      </div>

      {job.progress && <p className="ta-muted ta-small" style={{ marginBottom: '0.5rem' }}>{job.progress}</p>}

      {log.length > 0 && (
        <pre ref={logRef} className="ta-pre" style={{ maxHeight: 240 }}>
          {log.join('\n')}
        </pre>
      )}
    </Card>
  )
}
