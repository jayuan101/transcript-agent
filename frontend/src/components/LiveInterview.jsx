import React, { useRef, useState, useEffect, useCallback } from 'react'
import { Card } from 'primereact/card'
import { Button } from 'primereact/button'
import { Tag } from 'primereact/tag'
import { Message } from 'primereact/message'
import { startVideoAnalysis, getJob, transcribeClip } from '../api.js'
import ScoreBar from './ScoreBar.jsx'

const CHUNK_SECONDS = 5

export default function LiveInterview() {
  const videoRef = useRef(null)
  const streamRef = useRef(null)
  const activeRef = useRef(false)
  const transcriptRef = useRef(null)
  const [running, setRunning] = useState(false)
  const [status, setStatus] = useState('')
  const [error, setError] = useState('')
  const [scores, setScores] = useState(null)
  const [tickCount, setTickCount] = useState(0)
  const [transcript, setTranscript] = useState([]) // [{ t: '12:00:01', text: '...' }]
  const [liveTranscribe, setLiveTranscribe] = useState(true)

  const stopAll = useCallback(() => {
    activeRef.current = false
    setRunning(false)
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((t) => t.stop())
      streamRef.current = null
    }
    setStatus('Stopped.')
  }, [])

  useEffect(() => () => stopAll(), [stopAll])

  useEffect(() => {
    if (transcriptRef.current) transcriptRef.current.scrollTop = transcriptRef.current.scrollHeight
  }, [transcript.length])

  // Record one clip from the live stream and resolve with a Blob.
  function recordClip(stream, seconds) {
    return new Promise((resolve, reject) => {
      let mime = 'video/webm;codecs=vp8,opus'
      if (!MediaRecorder.isTypeSupported(mime)) mime = 'video/webm'
      const chunks = []
      let rec
      try {
        rec = new MediaRecorder(stream, { mimeType: mime })
      } catch (e) {
        reject(e); return
      }
      rec.ondataavailable = (e) => { if (e.data.size > 0) chunks.push(e.data) }
      rec.onstop = () => resolve(new Blob(chunks, { type: 'video/webm' }))
      rec.onerror = (e) => reject(e.error || new Error('recorder error'))
      rec.start()
      setTimeout(() => { if (rec.state !== 'inactive') rec.stop() }, seconds * 1000)
    })
  }

  async function analyzeClip(blob) {
    const fd = new FormData()
    fd.append('file', new File([blob], 'live.webm', { type: 'video/webm' }))
    fd.append('person_count', '1')
    fd.append('roles', 'Candidate')
    fd.append('sample_fps', '1')
    const { job_id } = await startVideoAnalysis(fd)
    // poll until done
    for (let i = 0; i < 60; i++) {
      await new Promise((r) => setTimeout(r, 1500))
      const j = await getJob(job_id)
      if (j.status === 'done') return j
      if (j.status === 'error') throw new Error(j.error || 'analysis failed')
      if (!activeRef.current) return null
    }
    throw new Error('analysis timed out')
  }

  function addTranscriptLine(text) {
    if (!text) return
    const t = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
    setTranscript((prev) => [...prev, { t, text }])
  }

  async function loop() {
    while (activeRef.current) {
      try {
        setStatus(`Recording ${CHUNK_SECONDS}s clip…`)
        const blob = await recordClip(streamRef.current, CHUNK_SECONDS)
        if (!activeRef.current) break
        setStatus('Analyzing clip…')

        const tasks = [analyzeClip(blob)]
        if (liveTranscribe) tasks.push(transcribeClip(blob, { whisperModel: 'tiny' }))
        const results = await Promise.allSettled(tasks)

        if (!activeRef.current) break

        const analysis = results[0]
        if (analysis.status === 'fulfilled' && analysis.value) {
          const p = (analysis.value.persons || [])[0]
          if (p) {
            setScores(p)
            setTickCount((c) => c + 1)
          }
        } else if (analysis.status === 'rejected') {
          throw analysis.reason
        }

        if (liveTranscribe && results[1]) {
          if (results[1].status === 'fulfilled') {
            addTranscriptLine(results[1].value.text)
          }
        }

        setStatus('Live — scores update every few seconds.')
      } catch (e) {
        setError(e.message)
        // keep going on transient errors
        await new Promise((r) => setTimeout(r, 1000))
      }
    }
  }

  async function start() {
    setError(''); setScores(null); setTickCount(0); setTranscript([])
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ video: true, audio: true })
      streamRef.current = stream
      if (videoRef.current) videoRef.current.srcObject = stream
      activeRef.current = true
      setRunning(true)
      setStatus('Starting…')
      loop()
    } catch (e) {
      setError('Camera/microphone access failed: ' + e.message)
    }
  }

  return (
    <>
      <Card className="ta-w-full" title="🔴 Live interview coaching">
        <p className="ta-muted ta-small" style={{ marginTop: 0 }}>
          Your webcam &amp; mic are recorded in {CHUNK_SECONDS}-second clips and analyzed on the server —
          delivery scores and a rolling live transcript refresh after each clip. Nothing is stored permanently.
        </p>
        <div className="ta-cols">
          <div>
            <video ref={videoRef} autoPlay playsInline muted style={{ width: '100%', borderRadius: 8, background: '#000' }} />
            <div className="ta-flex ta-mt">
              {!running ? (
                <Button label="Start live analysis" icon="pi pi-circle-fill" severity="danger" onClick={start} />
              ) : (
                <Button label="Stop" icon="pi pi-stop" severity="secondary" onClick={stopAll} />
              )}
              {running && <Tag severity="info" value={`clips analyzed: ${tickCount}`} />}
              <Button
                label={liveTranscribe ? 'Live transcription: on' : 'Live transcription: off'}
                icon={liveTranscribe ? 'pi pi-microphone' : 'pi pi-microphone-slash'}
                outlined size="small"
                onClick={() => setLiveTranscribe((v) => !v)}
              />
            </div>
            {status && <p className="ta-muted ta-small ta-mt" style={{ marginBottom: 0 }}>{status}</p>}
          </div>
          <div>
            <Card title="Live scores" className="ta-w-full" style={{ height: '100%' }}>
              {!scores ? (
                <p className="ta-muted ta-small">Waiting for the first clip…</p>
              ) : (
                <>
                  <div className="ta-flex" style={{ marginBottom: '0.5rem' }}>
                    <span>Overall: <Tag value={`${scores.overall}/100`} /></span>
                    <span className="ta-muted">{scores.dominant_emotion}</span>
                  </div>
                  <ScoreBar label="Confidence" value={scores.confidence} />
                  <ScoreBar label="Composure" value={scores.composure} />
                  <ScoreBar label="Eye contact" value={scores.eye_contact} />
                  <ScoreBar label="Engagement" value={scores.engagement} />
                  <ScoreBar label="Energy" value={scores.energy} />
                </>
              )}
            </Card>

            {liveTranscribe && (
              <Card title="🎙 Live transcript" className="ta-w-full ta-mt">
                {transcript.length === 0 ? (
                  <p className="ta-muted ta-small">Waiting for the first transcribed clip…</p>
                ) : (
                  <pre ref={transcriptRef} className="ta-pre" style={{ maxHeight: 220 }}>
                    {transcript.map((l) => `[${l.t}] ${l.text}`).join('\n')}
                  </pre>
                )}
              </Card>
            )}
          </div>
        </div>
      </Card>
      {error && <Message severity="warn" className="ta-mt ta-w-full" text={error} />}
    </>
  )
}
