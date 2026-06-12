import React, { useState } from 'react'
import { Card } from 'primereact/card'
import { TabView, TabPanel } from 'primereact/tabview'
import { Tag } from 'primereact/tag'
import { Button } from 'primereact/button'
import { Dropdown } from 'primereact/dropdown'
import { Message } from 'primereact/message'
import { downloadUrl, regenerateReports } from '../api.js'
import { OUTPUT_LANGUAGES } from '../providers.js'
import DeliveryPanel from './DeliveryPanel.jsx'

function copy(text) {
  navigator.clipboard?.writeText(text || '')
}

// action_items may be plain strings or {action, owner, timeline} objects.
function renderActionItem(a) {
  if (a && typeof a === 'object') {
    const parts = [a.action, a.owner && `— ${a.owner}`, a.timeline && `(${a.timeline})`]
    return parts.filter(Boolean).join(' ')
  }
  return String(a)
}

function renderAccent(v) {
  return Array.isArray(v) ? v.join(', ') : (v || '')
}

const SCORE_SEVERITY = (s) => (s === 'Great' ? 'success' : s === 'Good' ? 'info' : s === 'Needs Improvement' ? 'warning' : 'danger')

function InterviewCoaching({ ia }) {
  if (!ia || Object.keys(ia).length === 0) {
    return <p className="ta-muted">Interview Mode was not enabled for this run.</p>
  }
  const questions = ia.questions || []
  const coding = ia.coding_challenges || []
  const strengths = ia.strengths || []
  const weaknesses = ia.weaknesses || []
  const prep = ia.prep_guide || []

  return (
    <>
      <div className="ta-flex" style={{ marginBottom: '0.75rem' }}>
        {ia.overall_verdict && <span>Verdict: <Tag severity={SCORE_SEVERITY(ia.overall_verdict)} value={ia.overall_verdict} /></span>}
        {ia.overall_score != null && <span>Score: <Tag value={`${ia.overall_score}/10`} /></span>}
        {ia.detected_role && <span className="ta-muted">Role: {ia.detected_role}</span>}
      </div>

      {strengths.length > 0 && (
        <>
          <div className="ta-section-title" style={{ color: 'var(--green-500)' }}>Strengths</div>
          <ul className="ta-small">{strengths.map((s, i) => <li key={i}>{s}</li>)}</ul>
        </>
      )}
      {weaknesses.length > 0 && (
        <>
          <div className="ta-section-title" style={{ color: 'var(--red-500)' }}>Areas to improve</div>
          <ul className="ta-small">{weaknesses.map((s, i) => <li key={i}>{s}</li>)}</ul>
        </>
      )}

      {questions.length > 0 && <div className="ta-section-title ta-mt">Questions ({questions.length})</div>}
      {questions.map((q, i) => (
        <Card key={i} className="ta-mt ta-w-full" style={{ marginBottom: '0.5rem' }}>
          <div className="ta-flex" style={{ justifyContent: 'space-between' }}>
            <strong>{q.question || `Question ${i + 1}`}</strong>
            <div style={{ whiteSpace: 'nowrap' }}>
              {q.type && <Tag severity="secondary" value={q.type} style={{ marginRight: '0.3rem' }} />}
              {q.score && <Tag severity={SCORE_SEVERITY(q.score)} value={q.score} />}
            </div>
          </div>
          {q.answer_said && <p className="ta-small" style={{ marginBottom: '0.25rem' }}><span className="ta-muted">Said:</span> {q.answer_said}</p>}
          {q.score_reason && <p className="ta-small ta-muted" style={{ marginBottom: '0.25rem' }}>{q.score_reason}</p>}
          {q.model_answer && <p className="ta-small" style={{ marginBottom: '0.25rem' }}><span className="ta-muted">Model answer:</span> {q.model_answer}</p>}
          {q.coaching_tip && <p className="ta-small" style={{ marginBottom: 0 }}>💡 <em>{q.coaching_tip}</em></p>}
        </Card>
      ))}

      {coding.length > 0 && (
        <>
          <div className="ta-section-title ta-mt">
            Coding challenges {ia.coding_score != null && <Tag value={`${ia.coding_score}/10`} />}
          </div>
          {coding.map((c, i) => (
            <Card key={i} className="ta-w-full" style={{ marginBottom: '0.5rem' }}>
              <strong>{c.challenge || c.title || `Challenge ${i + 1}`}</strong>
              {c.score && <Tag severity={SCORE_SEVERITY(c.score)} value={c.score} style={{ marginLeft: '0.5rem' }} />}
              {c.score_reason && <p className="ta-small ta-muted" style={{ marginBottom: 0, marginTop: '0.25rem' }}>{c.score_reason}</p>}
              {c.coaching_tip && <p className="ta-small" style={{ marginBottom: 0 }}>💡 <em>{c.coaching_tip}</em></p>}
            </Card>
          ))}
        </>
      )}

      {prep.length > 0 && (
        <>
          <div className="ta-section-title ta-mt">Prep guide</div>
          <ul className="ta-small">{prep.map((p, i) => <li key={i}>{typeof p === 'object' ? (p.topic || JSON.stringify(p)) : p}</li>)}</ul>
        </>
      )}
    </>
  )
}

const kb = (n) => `${Math.max(1, Math.round(n / 1024))} KB`

// Match files to a friendly label + icon based on the suffix save_results()/the
// API write them with. Mirrors the old Gradio download-button layout.
const FILE_KINDS = [
  { test: /\.docx$/i, label: 'DOCX (Word)', icon: 'pi pi-file-word' },
  { test: /\.pdf$/i, label: 'PDF', icon: 'pi pi-file-pdf' },
  { test: /annotated_video\.mp4$/i, label: 'Annotated video', icon: 'pi pi-video' },
  { test: /_report\.md$/i, label: 'Markdown report', icon: 'pi pi-file-edit' },
  { test: /_transcript\.txt$/i, label: 'Transcript .txt', icon: 'pi pi-file' },
  { test: /_speakers\.txt$/i, label: 'Speaker dialogue .txt', icon: 'pi pi-users' },
  { test: /_combined\.txt$/i, label: 'Combined .txt', icon: 'pi pi-file' },
  { test: /\.srt$/i, label: 'SRT subtitles', icon: 'pi pi-closed-captioning' },
  { test: /\.vtt$/i, label: 'VTT subtitles', icon: 'pi pi-closed-captioning' },
  { test: /_full\.json$/i, label: 'Raw JSON', icon: 'pi pi-code' },
]

function describeFile(f) {
  const kind = FILE_KINDS.find((k) => k.test.test(f.name))
  return { ...f, label: kind?.label || f.name, icon: kind?.icon || 'pi pi-download' }
}

function Downloads({ jobId, files }) {
  if (!files || files.length === 0) return null

  const described = files.map(describeFile)

  // Reports: DOCX, PDF, Markdown report.
  const reports = described.filter((f) => /\.docx$|\.pdf$|_report\.md$/i.test(f.name))
  // Transcripts: plain text, speaker dialogue, combined.
  const transcripts = described.filter((f) => /_transcript\.txt$|_speakers\.txt$|_combined\.txt$/i.test(f.name))
  // Subtitles & data: SRT, VTT, raw JSON.
  const data = described.filter((f) => /\.srt$|\.vtt$|_full\.json$/i.test(f.name))
  const video = described.filter((f) => /annotated_video\.mp4$/i.test(f.name))
  const known = [...reports, ...transcripts, ...data, ...video]
  const other = described.filter((f) => !known.includes(f))

  const row = (label, items, severity) => items.length > 0 && (
    <div style={{ marginBottom: '0.75rem' }}>
      <div className="ta-muted ta-small" style={{ marginBottom: '0.4rem' }}>{label}</div>
      <div className="ta-flex">
        {items.map((f) => (
          <Button key={f.name} label={`${f.label} · ${kb(f.size)}`} icon={f.icon} outlined size="small" severity={severity}
            onClick={() => window.open(downloadUrl(jobId, f.name), '_blank')} />
        ))}
      </div>
    </div>
  )

  return (
    <Card className="ta-mt ta-w-full" title="⬇ Download report">
      <p className="ta-muted ta-small" style={{ marginTop: 0 }}>
        The full report is available as DOCX, PDF, and Markdown — plus raw transcripts, subtitles, and data.
      </p>
      {row('Reports', reports, 'danger')}
      {row('Transcripts', transcripts)}
      {row('Subtitles & data', data, 'info')}
      {row('Video', video, 'success')}
      {row('Other files', other)}
    </Card>
  )
}

function RegenerateBar({ jobId, job, onDone }) {
  const [targetLang, setTargetLang] = useState('Same as source')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function onRegenerate() {
    setBusy(true); setError('')
    try {
      const res = await regenerateReports(jobId, {
        targetLang: targetLang === 'Same as source' ? undefined : targetLang,
        provider: job.provider, model: job.model,
      })
      onDone?.(res)
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card className="ta-mt ta-w-full" title="🔁 Regenerate report">
      <p className="ta-muted ta-small" style={{ marginTop: 0 }}>
        Re-render the PDF &amp; DOCX, optionally translated into another language.
      </p>
      <div className="ta-flex">
        <Dropdown
          value={targetLang}
          onChange={(e) => setTargetLang(e.value)}
          options={OUTPUT_LANGUAGES.map((l) => ({ label: l, value: l }))}
          style={{ minWidth: 220 }}
        />
        <Button label={busy ? 'Regenerating…' : 'Regenerate'} icon="pi pi-refresh" loading={busy} onClick={onRegenerate} />
      </div>
      {error && <Message severity="error" className="ta-mt ta-w-full" text={error} />}
    </Card>
  )
}

export default function Results({ job, jobId }) {
  const [extraDownloads, setExtraDownloads] = useState(null)
  if (!job || job.status !== 'done') return null

  const transcript = job.transcript || '(no transcript returned)'
  const dialogue = job.speaker_dialogue || ''
  const keyPoints = job.key_points || []
  const actionItems = job.action_items || []
  const speakers = job.speaker_stats || []
  const profiles = job.speaker_profiles || {}
  const ia = job.interview_analysis || {}
  const hasInterview = ia && Object.keys(ia).length > 0
  const delivery = job.delivery || null

  const copyAll = [
    job.summary && `SUMMARY\n${job.summary}`,
    keyPoints.length > 0 && `KEY POINTS\n${keyPoints.map((k) => `- ${k}`).join('\n')}`,
    actionItems.length > 0 && `ACTION ITEMS\n${actionItems.map((a) => `- ${renderActionItem(a)}`).join('\n')}`,
    `TRANSCRIPT\n${transcript}`,
    dialogue && `SPEAKER DIALOGUE\n${dialogue}`,
  ].filter(Boolean).join('\n\n')

  const downloads = extraDownloads?.downloads || job.downloads

  return (
    <>
      <Card className="ta-mt ta-w-full">
        <div className="ta-flex" style={{ justifyContent: 'space-between', marginBottom: '0.75rem' }}>
          <span className="ta-section-title" style={{ margin: 0 }}>Results</span>
          {(job.cost_usd != null || job.tok_in != null) && (
            <span className="ta-small">
              This run: <Tag severity="success" value={job.cost_usd != null ? (job.cost_usd < 0.01 ? `$${job.cost_usd.toFixed(4)}` : `$${job.cost_usd.toFixed(3)}`) : '—'} />
              <span className="ta-muted" style={{ marginLeft: '0.5rem' }}>{(job.tok_in || 0).toLocaleString()}↑ / {(job.tok_out || 0).toLocaleString()}↓ tokens</span>
            </span>
          )}
        </div>

        <TabView>
          <TabPanel header="Summary">
            <p style={{ whiteSpace: 'pre-wrap' }}>{job.summary || '(no summary)'}</p>
            {keyPoints.length > 0 && (
              <>
                <div className="ta-section-title ta-mt">Key points</div>
                <ul className="ta-small">{keyPoints.map((k, i) => <li key={i}>{k}</li>)}</ul>
              </>
            )}
            {actionItems.length > 0 && (
              <>
                <div className="ta-section-title ta-mt">Action items</div>
                <ul className="ta-small">{actionItems.map((a, i) => <li key={i}>{renderActionItem(a)}</li>)}</ul>
              </>
            )}
          </TabPanel>

          <TabPanel header="Transcript">
            <div className="ta-flex" style={{ justifyContent: 'flex-end', marginBottom: '0.5rem' }}>
              <Button label="Copy" icon="pi pi-copy" size="small" outlined onClick={() => copy(transcript)} />
            </div>
            <pre className="ta-pre">{transcript}</pre>
          </TabPanel>

          <TabPanel header="Speaker Dialogue">
            {dialogue ? (
              <>
                <div className="ta-flex" style={{ justifyContent: 'flex-end', marginBottom: '0.5rem' }}>
                  <Button label="Copy" icon="pi pi-copy" size="small" outlined onClick={() => copy(dialogue)} />
                </div>
                <pre className="ta-pre">{dialogue}</pre>
              </>
            ) : <p className="ta-muted">No speaker-labelled dialogue (enable Panel mode for diarization).</p>}
          </TabPanel>

          <TabPanel header="Speaker Profiles">
            {Object.keys(profiles).length === 0 ? (
              <p className="ta-muted">No speaker profiles available.</p>
            ) : Object.entries(profiles).map(([name, text]) => (
              <div key={name} className="ta-mt">
                <strong>{name}</strong>
                <p style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>{String(text)}</p>
              </div>
            ))}
          </TabPanel>

          <TabPanel header="Speech Analytics">
            {speakers.length === 0 ? (
              <p className="ta-muted">No speech analytics available.</p>
            ) : (
              <table className="ta-w-full ta-small" style={{ borderCollapse: 'collapse' }}>
                <thead>
                  <tr style={{ textAlign: 'left', borderBottom: '1px solid var(--surface-border)' }}>
                    <th style={{ padding: '0.4rem' }}>Name</th>
                    <th style={{ padding: '0.4rem' }}>WPM</th>
                    <th style={{ padding: '0.4rem' }}>Pace</th>
                    <th style={{ padding: '0.4rem' }}>Speaking %</th>
                    <th style={{ padding: '0.4rem' }}>Accent</th>
                  </tr>
                </thead>
                <tbody>
                  {speakers.map((s, i) => (
                    <tr key={i} style={{ borderBottom: '1px solid var(--surface-border)' }}>
                      <td style={{ padding: '0.4rem' }}>{s.name}</td>
                      <td style={{ padding: '0.4rem' }}>{s.words_per_minute}</td>
                      <td style={{ padding: '0.4rem' }}>{s.pace_label}</td>
                      <td style={{ padding: '0.4rem' }}>{s.speaking_percentage}%</td>
                      <td style={{ padding: '0.4rem' }}>{renderAccent(s.accent_indicators)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </TabPanel>

          {hasInterview && (
            <TabPanel header="🎤 Interview Coaching">
              <InterviewCoaching ia={ia} />
            </TabPanel>
          )}

          {delivery && (
            <TabPanel header="🎥 Delivery">
              <DeliveryPanel delivery={delivery} jobId={jobId} />
            </TabPanel>
          )}

          <TabPanel header="📋 Copy All">
            <div className="ta-flex" style={{ justifyContent: 'flex-end', marginBottom: '0.5rem' }}>
              <Button label="Copy everything" icon="pi pi-copy" size="small" onClick={() => copy(copyAll)} />
            </div>
            <pre className="ta-pre">{copyAll}</pre>
          </TabPanel>
        </TabView>
      </Card>

      <RegenerateBar jobId={jobId} job={job} onDone={setExtraDownloads} />
      <Downloads jobId={jobId} files={downloads} />
    </>
  )
}
