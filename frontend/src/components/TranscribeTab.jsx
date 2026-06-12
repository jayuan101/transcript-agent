import React, { useRef, useState, useEffect } from 'react'
import { Message } from 'primereact/message'
import UploadForm from './UploadForm.jsx'
import ProgressPanel from './ProgressPanel.jsx'
import Results from './Results.jsx'
import NetworkMonitor from './NetworkMonitor.jsx'
import { startTranscription, pollJob, regenerateReports } from '../api.js'

export default function TranscribeTab() {
  const [job, setJob] = useState(null)
  const [jobId, setJobId] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const stopRef = useRef(null)

  useEffect(() => () => stopRef.current?.(), [])

  async function handleSubmit(formData, { outputLang } = {}) {
    setError(''); setJob(null); setBusy(true)
    try {
      const { job_id } = await startTranscription(formData)
      setJobId(job_id)
      setJob({ status: 'queued', log: [] })
      stopRef.current = pollJob(job_id, (j) => {
        setJob(j)
        if (j.status === 'done' || j.status === 'error') {
          setBusy(false)
          if (j.status === 'error' && !/cancelled by user/i.test(j.error || '')) {
            setError(j.error || 'Processing failed')
          }
          if (j.status === 'done' && outputLang) {
            regenerateReports(job_id, { targetLang: outputLang, provider: j.provider, model: j.model })
              .then((res) => setJob((cur) => ({ ...cur, downloads: res.downloads || cur.downloads })))
              .catch((e) => setError(e.message))
          }
        }
      })
    } catch (e) {
      setError(e.message); setBusy(false)
    }
  }

  return (
    <>
      {error && (
        <Message severity="error" className="ta-w-full ta-mt" text={error}
          style={{ display: 'flex', justifyContent: 'space-between' }} />
      )}
      <UploadForm onSubmit={handleSubmit} busy={busy} />
      <ProgressPanel job={job} jobId={jobId} />
      <Results job={job} jobId={jobId} />
      {/* Network monitor stays at the very bottom (log → results → network). */}
      <NetworkMonitor job={job} />
    </>
  )
}
