import React from 'react'
import { Card } from 'primereact/card'
import { Tag } from 'primereact/tag'
import { ProgressBar } from 'primereact/progressbar'
import { parseProgress, fmtDur } from '../progress.js'

// Real-time network monitor: how much data the run is pulling (download) and
// pushing (upload), with live throughput and session totals. Fed by the
// backend's per-job `network` field (system-wide psutil counters since the job
// started). Rendered at the very bottom of the page.
export default function NetworkMonitor({ job }) {
  const net = job?.network
  if (!net) return null

  const rows = [
    { dir: '⬇️ Download', total: net.recv_mb, rate: net.dn_rate_mbs },
    { dir: '⬆️ Upload', total: net.sent_mb, rate: net.up_rate_mbs },
  ]

  const working = job?.status === 'processing' || job?.status === 'queued'
  const { percent, eta } = parseProgress(job)
  const showPct = working && percent != null
  const etaTxt = working && eta != null ? fmtDur(eta) : null

  return (
    <Card className="ta-mt ta-w-full">
      <div className="ta-flex" style={{ marginBottom: '0.5rem' }}>
        <span className="ta-section-title" style={{ margin: 0 }}>📡 Network monitor <span className="ta-muted">(live)</span></span>
        {showPct && <Tag severity="info" value={`${percent}%`} />}
        {etaTxt && <span className="ta-small">⏳ ≈ {etaTxt} left</span>}
      </div>

      {showPct && (
        <ProgressBar value={percent} showValue style={{ height: '1.1rem', marginBottom: '0.75rem' }} />
      )}

      <table className="ta-w-full ta-small" style={{ borderCollapse: 'collapse' }}>
        <thead>
          <tr className="ta-muted" style={{ textAlign: 'left' }}>
            <th style={{ fontWeight: 600 }}>Direction</th>
            <th style={{ fontWeight: 600, textAlign: 'right' }}>Rate</th>
            <th style={{ fontWeight: 600, textAlign: 'right' }}>Session total</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.dir}>
              <td style={{ padding: '0.25rem 0' }}>{r.dir}</td>
              <td style={{ textAlign: 'right' }}>{(r.rate ?? 0).toFixed(2)} MB/s</td>
              <td style={{ textAlign: 'right' }}><strong>{(r.total ?? 0).toFixed(2)} MB</strong></td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="ta-muted ta-small" style={{ marginBottom: 0, marginTop: '0.5rem' }}>
        System-wide throughput since the job started ({net.elapsed ?? 0}s elapsed) — a live gauge of how much the run is transferring.
      </p>
    </Card>
  )
}
