// Shared progress helpers used by ProgressPanel and NetworkMonitor so both show
// the same live percent + ETA pulled from the backend's job.progress / log.

export function fmtDur(secs) {
  if (secs == null || !isFinite(secs) || secs < 0) return null
  secs = Math.round(secs)
  const m = Math.floor(secs / 60)
  const s = secs % 60
  return m ? `${m}m ${String(s).padStart(2, '0')}s` : `${s}s`
}

// The backend streams progress text like:
//   "Delivery analysis… 33%  {'done': 254, 'total': 599, 'eta': 88}"
// and STT logs an estimate like "Est. transcription time: 2m 31s".
// Pull a percent + an ETA (seconds) out of whatever is available.
export function parseProgress(job) {
  const text = job?.progress || ''
  const log = job?.log || []
  let percent = null
  let eta = null

  const pctM = text.match(/(\d{1,3})\s*%/)
  if (pctM) percent = Math.min(100, parseInt(pctM[1], 10))

  const etaM = text.match(/'eta':\s*([\d.]+)/)
  if (etaM) eta = parseFloat(etaM[1])

  // Fallback ETA: the STT "Est. transcription time: 2m 31s" line.
  if (eta == null) {
    for (let i = log.length - 1; i >= 0; i--) {
      const m = log[i].match(/Est\.?\s*transcription time:\s*(?:(\d+)m\s*)?(\d+)s/i)
      if (m) { eta = (parseInt(m[1] || '0', 10) * 60) + parseInt(m[2], 10); break }
    }
  }
  return { percent, eta }
}
