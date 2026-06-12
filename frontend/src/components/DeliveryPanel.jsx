import React from 'react'
import { Card } from 'primereact/card'
import { Tag } from 'primereact/tag'
import { Button } from 'primereact/button'
import ScoreBar from './ScoreBar.jsx'
import { downloadUrl } from '../api.js'

// Renders the on-screen delivery analysis (body language, emotion, eye contact)
// that the combined interview-video run produces alongside the transcript.
function PersonCard({ p }) {
  return (
    <Card className="ta-w-full" style={{ height: '100%' }}>
      <div className="ta-flex" style={{ justifyContent: 'space-between', marginBottom: '0.5rem' }}>
        <strong>{p.role}</strong>
        <Tag value={`${p.overall}/100`} />
      </div>
      <ScoreBar label="Confidence" value={p.confidence} />
      <ScoreBar label="Composure" value={p.composure} />
      <ScoreBar label="Eye contact" value={p.eye_contact} />
      <ScoreBar label="Engagement" value={p.engagement} />
      <ScoreBar label="Energy" value={p.energy} />
      <ScoreBar label="Receptiveness" value={p.receptiveness} />
      <hr style={{ border: 0, borderTop: '1px solid var(--surface-border)' }} />
      <p className="ta-small" style={{ marginBottom: '0.25rem' }}>Dominant emotion: <Tag severity="secondary" value={p.dominant_emotion} /></p>
      <p className="ta-small" style={{ marginBottom: '0.25rem' }}>Open body: {p.open_body_pct}% · Arms crossed: {p.arm_crossed_pct}% · Forward lean: {p.forward_lean_pct}%</p>
      <p className="ta-small" style={{ marginBottom: 0 }}>Talk time: {p.talk_time_pct}%</p>
      {p.cultural && (
        <div className="ta-mt">
          <p className="ta-small" style={{ marginBottom: '0.25rem' }}>
            American standard: <Tag severity="info" value={`${p.cultural.american_score}/100`} /> · Adaptation: <Tag severity="info" value={`${p.cultural.adaptation_score}/100`} />
          </p>
          {(p.cultural.adaptation_tips || []).slice(0, 3).map((t, i) => (
            <p key={i} className="ta-small ta-muted" style={{ marginBottom: 0 }}>• {t}</p>
          ))}
        </div>
      )}
    </Card>
  )
}

export default function DeliveryPanel({ delivery, jobId }) {
  if (!delivery) return null
  const persons = delivery.persons || []

  return (
    <>
      <div className="ta-flex ta-mt" style={{ marginBottom: '0.75rem' }}>
        <span>Overall: <Tag value={`${delivery.overall_score}/100`} /></span>
        <span>Rapport: <Tag severity="info" value={`${delivery.rapport_score}/100`} /></span>
        <span>Talk balance: <Tag severity="info" value={`${delivery.talk_balance_score}/100`} /></span>
      </div>

      <div className="ta-caps" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))' }}>
        {persons.map((p) => <PersonCard key={p.person_id} p={p} />)}
      </div>

      {(delivery.observations || []).length > 0 && (
        <>
          <div className="ta-section-title ta-mt">Observations</div>
          <ul className="ta-small">{delivery.observations.map((o, i) => <li key={i}>{o}</li>)}</ul>
        </>
      )}

      {delivery.annotated_video && (
        <div className="ta-mt">
          <video controls src={downloadUrl(jobId, delivery.annotated_video)} style={{ maxWidth: '100%', borderRadius: 8 }} />
          <div className="ta-mt">
            <Button label="Download annotated video" icon="pi pi-download" outlined size="small"
              onClick={() => window.open(downloadUrl(jobId, delivery.annotated_video), '_blank')} />
          </div>
        </div>
      )}
    </>
  )
}
