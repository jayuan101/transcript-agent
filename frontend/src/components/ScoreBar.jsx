import React from 'react'

// A labelled 0–100 score with a colour-coded bar.
// Shared by VideoAnalysis and LiveInterview.
export function scoreVariant(value) {
  return value >= 75 ? '#22c55e' : value >= 50 ? '#3b82f6' : value >= 30 ? '#f59e0b' : '#ef4444'
}

export default function ScoreBar({ label, value }) {
  const v = Number(value) || 0
  return (
    <div className="ta-score">
      <div className="ta-score-head">
        <span>{label}</span>
        <span>{v}</span>
      </div>
      <div className="ta-score-track">
        <div className="ta-score-fill" style={{ width: `${v}%`, background: scoreVariant(v) }} />
      </div>
    </div>
  )
}
