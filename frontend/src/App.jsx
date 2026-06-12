import React, { useEffect, useState } from 'react'
import { TabView, TabPanel } from 'primereact/tabview'
import { Button } from 'primereact/button'
import { Message } from 'primereact/message'
import TranscribeTab from './components/TranscribeTab.jsx'
import LiveInterview from './components/LiveInterview.jsx'
import HistoryTab from './components/HistoryTab.jsx'
import { checkHealth, checkUpdate } from './api.js'
import { applyTheme } from './theme.js'

const PILLS = ['🤖 8 AI Providers', '🌐 37+ Languages', '🔒 100% Private', '🎵 Audio · Video · Docs']

export default function App() {
  const [dark, setDark] = useState(localStorage.getItem('ta-dark') === 'true')
  const [apiUp, setApiUp] = useState(null)
  const [update, setUpdate] = useState(null)
  const [checking, setChecking] = useState(false)

  useEffect(() => {
    applyTheme(dark)
    localStorage.setItem('ta-dark', String(dark))
  }, [dark])

  useEffect(() => { checkHealth().then(setApiUp) }, [])

  async function onCheckUpdate() {
    setChecking(true)
    try { setUpdate(await checkUpdate()) }
    finally { setChecking(false) }
  }

  return (
    <div className="ta-page">
      {/* Top bar */}
      <div className="ta-topbar">
        <strong style={{ fontSize: '1.05rem' }}>🎤 Transcript Agent</strong>
        <div className="ta-topbar-actions">
          <Button label="Check for Updates" icon="pi pi-refresh" text size="small"
            loading={checking} onClick={onCheckUpdate} />
          <a className="p-button p-button-text p-button-sm" href="/docs" target="_blank" rel="noreferrer">
            <span className="pi pi-book" style={{ marginRight: 6 }} />API docs
          </a>
          <Button
            icon={dark ? 'pi pi-moon' : 'pi pi-sun'}
            label={dark ? 'Dark' : 'Light'}
            outlined size="small"
            onClick={() => setDark((d) => !d)}
            aria-label="Toggle light or dark theme"
          />
        </div>
      </div>

      {/* Hero */}
      <div className="ta-hero">
        <div className="ta-hero-icon">🎤</div>
        <div className="ta-hero-body">
          <span className="ta-hero-name">Transcript Agent</span>
          <span className="ta-hero-tag">Whisper transcription · Multi-provider AI · Speaker diarization</span>
        </div>
        <div className="ta-hero-pills">
          {PILLS.map((p) => <span className="ta-pill" key={p}>{p}</span>)}
        </div>
      </div>

      {/* Update banner */}
      {update && (
        <div className="ta-mt" style={{ marginBottom: '1rem' }}>
          {update.update_available ? (
            <Message severity="warn" className="ta-w-full" content={
              <span>
                🔔 <strong>Update available — v{update.latest}</strong> (you have v{update.current}).{' '}
                <a href={update.url} target="_blank" rel="noreferrer">Download →</a>
              </span>
            } />
          ) : (
            <Message severity="success" className="ta-w-full"
              text={`✅ You're up to date — v${update.current} is the latest.`} />
          )}
        </div>
      )}

      {apiUp === false && (
        <div style={{ marginBottom: '1rem' }}>
          <Message severity="warn" className="ta-w-full"
            text="Backend not reachable. Start it with: python api.py (defaults to port 8000)." />
        </div>
      )}

      <TabView>
        <TabPanel header="📝 Transcribe & Analyze">
          <TranscribeTab />
        </TabPanel>
        <TabPanel header="🔴 Live Interview">
          <LiveInterview />
        </TabPanel>
        <TabPanel header="🗂 History & Spend">
          <HistoryTab />
        </TabPanel>
      </TabView>

      {/* Footer */}
      <div className="ta-footer">
        <span>Transcript Agent · OpenAI Whisper · Anthropic Claude · 100% Private</span>
        <a className="ta-btn-donate" href="https://paypal.me/jay247616" target="_blank" rel="noreferrer">💙 Donate</a>
        <a className="ta-btn-bug" href="https://forms.gle/aEMqRjFGyAVWVKQ77" target="_blank" rel="noreferrer">🐛 Report a Bug</a>
        <a href="https://github.com/jayuan101/transcript-agent/blob/main/CHANGELOG.md" target="_blank" rel="noreferrer">Changelog →</a>
      </div>
    </div>
  )
}
