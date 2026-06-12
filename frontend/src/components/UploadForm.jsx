import React, { useMemo, useState, useEffect } from 'react'
import { Card } from 'primereact/card'
import { Dropdown } from 'primereact/dropdown'
import { InputText } from 'primereact/inputtext'
import { InputTextarea } from 'primereact/inputtextarea'
import { InputNumber } from 'primereact/inputnumber'
import { Password } from 'primereact/password'
import { InputSwitch } from 'primereact/inputswitch'
import { Checkbox } from 'primereact/checkbox'
import { Button } from 'primereact/button'
import { Accordion, AccordionTab } from 'primereact/accordion'
import { Message } from 'primereact/message'
import { FileUpload } from 'primereact/fileupload'
import {
  PROVIDERS, STT_CATALOG, sttEngineByKey, REPORT_STYLES, LANGUAGES,
  LANGUAGE_VARIANTS, CAPABILITIES, OUTPUT_LANGUAGES,
} from '../providers.js'
import { getDevices } from '../api.js'

const REPORT_SECTIONS = [
  ['include_summary', 'Summary'],
  ['include_key_points', 'Key points'],
  ['include_action_items', 'Action items'],
  ['include_transcript', 'Full transcript'],
  ['include_speaker_profiles', 'Speaker profiles'],
  ['include_speech_analytics', 'Speech analytics'],
]

const FILE_ACCEPT = '.mp3,.wav,.m4a,.flac,.ogg,.aac,.mp4,.mov,.mkv,.webm,.srt,.vtt,.pdf,.docx,.txt'

export default function UploadForm({ onSubmit, busy }) {
  const [file, setFile] = useState(null)
  const [sourceUrl, setSourceUrl] = useState('')

  // Core
  const [language, setLanguage] = useState('')
  const [languageVariant, setLanguageVariant] = useState('')
  const [reportStyle, setReportStyle] = useState('formal')
  const [outputLang, setOutputLang] = useState('Same as source')

  // Transcription — engine and model picked separately (see STT_CATALOG)
  const [sttEngine, setSttEngine] = useState('whisper_local')
  const [sttModel, setSttModel] = useState('base')

  // AI provider / model
  const [providerName, setProviderName] = useState('Claude (Anthropic)')
  const [model, setModel] = useState(PROVIDERS['Claude (Anthropic)'].models[0])
  const [llmApiKey, setLlmApiKey] = useState('')

  // Speech-to-text API key (only needed for cloud STT engines)
  const [sttApiKey, setSttApiKey] = useState('')
  const [useGpu, setUseGpu] = useState(true)

  // Detected compute device (GPU/CPU) — fetched once, only relevant to local Whisper.
  const [device, setDevice] = useState(null)
  useEffect(() => {
    getDevices().then((d) => {
      setDevice(d)
      if (d && !d.gpu_available) setUseGpu(false) // no GPU → default to CPU
    })
  }, [])

  // Diarization
  const [panelMode, setPanelMode] = useState(false)
  const [numSpeakers, setNumSpeakers] = useState(null)

  // Report sections + flags
  const [sections, setSections] = useState(() => Object.fromEntries(REPORT_SECTIONS.map(([k]) => [k, true])))
  const [transcriptionOnly, setTranscriptionOnly] = useState(false)

  // Interview
  const [interviewMode, setInterviewMode] = useState(false)
  const [interviewDeep, setInterviewDeep] = useState(false)
  const [candidateProfile, setCandidateProfile] = useState('')
  const [profileFile, setProfileFile] = useState(null)

  const provider = PROVIDERS[providerName]
  const engineEntry = sttEngineByKey(sttEngine)
  const localStt = sttEngine === 'whisper_local'
  const variantOptions = LANGUAGE_VARIANTS[language] || null

  // Engine dropdown — grouped by free (local) vs cloud.
  const engineOptions = useMemo(
    () => STT_CATALOG.map((eng) => ({
      value: eng.engine,
      label: eng.cloud ? `${eng.label} (cloud)` : `${eng.label} — free`,
    })),
    [],
  )

  // Model dropdown — options depend on the selected engine.
  const modelOptions = useMemo(
    () => (engineEntry?.models || []).map(([val, lbl]) => ({ value: val, label: lbl })),
    [engineEntry],
  )

  function pickProvider(name) {
    setProviderName(name)
    setModel(PROVIDERS[name].models[0]) // reset model to the new provider's default
  }

  function pickSttEngine(engine) {
    setSttEngine(engine)
    const entry = sttEngineByKey(engine)
    setSttModel(entry?.default || entry?.models?.[0]?.[0] || '')
  }

  function toggleSection(key, checked) {
    setSections((s) => ({ ...s, [key]: checked }))
  }

  function onLanguageChange(code) {
    setLanguage(code)
    setLanguageVariant('') // reset variant when the base language changes
  }

  const sectionSummary = useMemo(
    () => REPORT_SECTIONS.filter(([k]) => sections[k]).map(([, l]) => l).join(', ') || 'none',
    [sections],
  )

  const url = sourceUrl.trim()
  const canSubmit = !!file || !!url

  function handleSubmit(e) {
    e.preventDefault()
    if (!canSubmit) return
    const fd = new FormData()
    // An uploaded file takes precedence; otherwise the server downloads the URL.
    if (file) fd.append('file', file)
    else fd.append('source_url', url)

    // STT — engine and model are picked separately.
    fd.append('stt_engine', sttEngine)
    if (localStt) {
      fd.append('whisper_model', sttModel || 'base')
    } else {
      if (sttModel) fd.append('stt_model', sttModel)
      if (sttApiKey.trim()) fd.append('stt_api_key', sttApiKey.trim())
    }
    fd.append('use_gpu', String(useGpu))
    if (language) fd.append('language', language)
    if (languageVariant.trim()) fd.append('language_variant', languageVariant.trim())

    // LLM provider
    fd.append('provider', provider.type)
    fd.append('model', model)
    if (provider.baseUrl) fd.append('base_url', provider.baseUrl)
    if (llmApiKey.trim()) fd.append('llm_api_key', llmApiKey.trim())

    // Report
    fd.append('report_style', reportStyle)
    fd.append('transcription_only', String(transcriptionOnly))
    for (const [key] of REPORT_SECTIONS) fd.append(key, String(sections[key]))

    // Diarization
    fd.append('panel_mode', String(panelMode))
    if (panelMode && numSpeakers) fd.append('num_speakers', String(numSpeakers))

    // Interview
    fd.append('interview_mode', String(interviewMode))
    if (interviewMode) {
      fd.append('interview_deep', String(interviewDeep))
      if (candidateProfile.trim()) fd.append('candidate_profile', candidateProfile)
      if (profileFile) fd.append('profile_file', profileFile)
    }

    onSubmit(fd, { outputLang: outputLang === 'Same as source' ? '' : outputLang })
  }

  return (
    <Card title="Upload &amp; configure" className="ta-w-full">
      <form onSubmit={handleSubmit}>
        <div className="ta-field">
          <label htmlFor="file">Audio / video / document</label>
          <FileUpload
            mode="basic"
            name="file"
            accept={FILE_ACCEPT}
            chooseLabel={file ? file.name : 'Choose file'}
            auto={false}
            disabled={busy}
            onSelect={(e) => setFile(e.files?.[0] || null)}
            onClear={() => setFile(null)}
          />
          <small className="ta-muted">
            Large files (e.g. a 3-hour video) are streamed to the server and processed in the background.
          </small>
        </div>

        <div className="ta-field">
          <label htmlFor="sourceUrl">…or paste a direct file URL <span className="ta-muted">(no upload)</span></label>
          <InputText
            id="sourceUrl"
            type="url"
            className="ta-w-full"
            placeholder="https://…/interview.mp4"
            value={sourceUrl}
            onChange={(e) => setSourceUrl(e.target.value)}
            disabled={busy || !!file}
          />
          <small className="ta-muted">
            The server downloads it directly — handy for big files. Direct links only (S3, Dropbox, Nextcloud…); not YouTube.
          </small>
        </div>

        <div className="ta-grid-2">
          <div className="ta-field">
            <label htmlFor="sttEngine">Transcription engine</label>
            <Dropdown
              id="sttEngine"
              className="ta-w-full"
              value={sttEngine}
              onChange={(e) => pickSttEngine(e.value)}
              options={engineOptions}
              optionLabel="label"
              optionValue="value"
              disabled={busy}
            />
            <small className="ta-muted">
              {localStt ? 'Runs locally — free, offline, no API key.' : 'Cloud engine — needs an API key below.'}
            </small>
          </div>
          <div className="ta-field">
            <label htmlFor="sttModel">Transcription model</label>
            <Dropdown
              id="sttModel"
              className="ta-w-full"
              value={sttModel}
              onChange={(e) => setSttModel(e.value)}
              options={modelOptions}
              optionLabel="label"
              optionValue="value"
              disabled={busy}
            />
          </div>
        </div>

        <div className="ta-grid-2">
          <div className="ta-field">
            <label htmlFor="language">Language</label>
            <Dropdown
              id="language"
              className="ta-w-full"
              value={language}
              onChange={(e) => onLanguageChange(e.value)}
              options={LANGUAGES}
              optionLabel="label"
              optionValue="code"
              disabled={busy}
            />
          </div>
          {variantOptions && (
            <div className="ta-field">
              <label htmlFor="languageVariant">Regional variant / dialect</label>
              <Dropdown
                id="languageVariant"
                className="ta-w-full"
                value={languageVariant}
                onChange={(e) => setLanguageVariant(e.value)}
                options={variantOptions}
                optionLabel="label"
                optionValue="value"
                placeholder="Auto"
                showClear
                disabled={busy}
              />
            </div>
          )}
        </div>

        <div className="ta-grid-2">
          <div className="ta-field">
            <label htmlFor="reportStyle">Report style</label>
            <Dropdown
              id="reportStyle"
              className="ta-w-full"
              value={reportStyle}
              onChange={(e) => setReportStyle(e.value)}
              options={REPORT_STYLES.map((s) => ({ label: s, value: s }))}
              disabled={busy}
            />
          </div>
          <div className="ta-field">
            <label htmlFor="outputLang">Translate output to</label>
            <Dropdown
              id="outputLang"
              className="ta-w-full"
              value={outputLang}
              onChange={(e) => setOutputLang(e.value)}
              options={OUTPUT_LANGUAGES.map((l) => ({ label: l, value: l }))}
              disabled={busy}
            />
            <small className="ta-muted">Translate the transcript &amp; report to a different language.</small>
          </div>
        </div>

        {/* ── STT key — sits directly under the Transcription selector ──── */}
        {!localStt && (
          <div className="ta-field">
            <label htmlFor="sttApiKey">
              🔑 {engineEntry?.label.split(' (')[0]} API key <span className="ta-muted">— required for this cloud engine</span>
            </label>
            <Password
              id="sttApiKey"
              className="ta-w-full"
              inputClassName="ta-w-full"
              feedback={false}
              toggleMask
              autoComplete="off"
              placeholder={`${engineEntry?.label.split(' (')[0]} key`}
              value={sttApiKey}
              onChange={(e) => setSttApiKey(e.target.value)}
              disabled={busy}
            />
            <small className="ta-muted">Sent only with this request — never stored on the server.</small>
          </div>
        )}

        <Accordion multiple className="ta-mt" activeIndex={[]}>
          {/* ── AI model ─────────────────────────────────────────────── */}
          <AccordionTab header={`🤖 AI model — ${providerName} · ${model}`}>
            <div className="ta-grid-2">
              <div className="ta-field">
                <label htmlFor="provider">Provider</label>
                <Dropdown
                  id="provider"
                  className="ta-w-full"
                  value={providerName}
                  onChange={(e) => pickProvider(e.value)}
                  options={Object.keys(PROVIDERS).map((p) => ({ label: p, value: p }))}
                  disabled={busy}
                />
              </div>
              <div className="ta-field">
                <label htmlFor="model">Model</label>
                <Dropdown
                  id="model"
                  className="ta-w-full"
                  value={model}
                  onChange={(e) => setModel(e.value)}
                  options={provider.models.map((m) => ({ label: m, value: m }))}
                  disabled={busy}
                />
              </div>
            </div>
            <div className="ta-field">
              <label htmlFor="llmApiKey">🤖 {providerName} API key <span className="ta-muted">— optional; overrides the server key</span></label>
              <Password
                id="llmApiKey"
                className="ta-w-full"
                inputClassName="ta-w-full"
                feedback={false}
                toggleMask
                autoComplete="off"
                placeholder={`${providerName} key — ${provider.keyPlaceholder}`}
                value={llmApiKey}
                onChange={(e) => setLlmApiKey(e.target.value)}
                disabled={busy}
              />
              <small className="ta-muted">Sent only with this request — never stored on the server.</small>
            </div>
          </AccordionTab>

          {/* ── Speech-to-text ───────────────────────────────────────── */}
          <AccordionTab header={`🎚 Speech-to-text — ${engineEntry?.label || sttEngine}${sttModel ? ` · ${sttModel}` : ''}`}>
            <p className="ta-small ta-muted ta-mt" style={{ marginTop: 0 }}>
              Pick the engine &amp; model at the top under <strong>Transcription engine / model</strong>.
              {!localStt && ' The API key field is right under it.'}
            </p>
            <div className="ta-flex">
              <InputSwitch
                inputId="useGpu"
                checked={useGpu}
                onChange={(e) => setUseGpu(e.value)}
                disabled={busy || !localStt}
              />
              <label htmlFor="useGpu" style={{ marginBottom: 0 }}>
                {device?.gpu_available
                  ? `Use GPU — ${device.kind} (${device.name})`
                  : 'Use GPU acceleration (if available)'}
              </label>
            </div>
            <small className="ta-muted" style={{ display: 'block', marginTop: '0.4rem' }}>
              {!localStt
                ? "Cloud engines run on the provider's servers — your GPU/CPU isn't used."
                : device
                  ? (device.gpu_available
                      ? `GPU detected: ${device.kind}. Toggle off to force CPU.`
                      : useGpu
                        ? `No GPU detected — this will still run on CPU. (${device.reason || 'CPU only'}) Your AMD GPU would need DirectML, which has no build for this Python.`
                        : 'Running on CPU.')
                  : 'Checking for a GPU…'}
            </small>
          </AccordionTab>

          {/* ── Report sections ──────────────────────────────────────── */}
          <AccordionTab header={`📋 Report sections — ${transcriptionOnly ? 'transcript only' : sectionSummary}`}>
            <div className="ta-flex" style={{ marginBottom: '0.75rem' }}>
              <InputSwitch
                inputId="transcriptionOnly"
                checked={transcriptionOnly}
                onChange={(e) => setTranscriptionOnly(e.value)}
                disabled={busy}
              />
              <label htmlFor="transcriptionOnly" style={{ marginBottom: 0 }}>
                Transcription only (skip AI analysis — fastest)
              </label>
            </div>
            <div className="ta-caps">
              {REPORT_SECTIONS.map(([key, label]) => (
                <div className="ta-flex" key={key} style={{ gap: '0.5rem' }}>
                  <Checkbox
                    inputId={key}
                    checked={sections[key]}
                    onChange={(e) => toggleSection(key, e.checked)}
                    disabled={busy || transcriptionOnly}
                  />
                  <label htmlFor={key} style={{ marginBottom: 0 }}>{label}</label>
                </div>
              ))}
            </div>
          </AccordionTab>

          {/* ── What we support ──────────────────────────────────────── */}
          <AccordionTab header="✨ What we support">
            <div className="ta-caps">
              {CAPABILITIES.map((c) => (
                <div className="ta-cap" key={c.title}>
                  <span className="ta-cap-ico">{c.icon}</span>
                  <div>
                    <div className="ta-cap-title">{c.title}</div>
                    <div className="ta-cap-text">{c.text}</div>
                  </div>
                </div>
              ))}
            </div>
          </AccordionTab>
        </Accordion>

        <div className="ta-flex ta-mt">
          <InputSwitch
            inputId="panelMode"
            checked={panelMode}
            onChange={(e) => setPanelMode(e.value)}
            disabled={busy}
          />
          <label htmlFor="panelMode" style={{ marginBottom: 0 }}>Panel mode (multi-speaker diarization)</label>
          {panelMode && (
            <InputNumber
              value={numSpeakers}
              onValueChange={(e) => setNumSpeakers(e.value)}
              min={2}
              max={20}
              placeholder="# speakers (optional)"
              showButtons={false}
              disabled={busy}
              style={{ width: 180 }}
            />
          )}
        </div>

        {panelMode && engineEntry?.diarizes && (
          <Message severity="success" className="ta-mt ta-w-full" content={
            <span>
              ✓ Panel mode uses <strong>{engineEntry.label}</strong>'s built-in speaker
              diarization — fast, cloud-based, no Whisper download.
            </span>
          } />
        )}
        {panelMode && !localStt && !engineEntry?.diarizes && (
          <Message severity="warn" className="ta-mt ta-w-full" content={
            <span>
              ⚠️ <strong>{engineEntry?.label.split(' (')[0]}</strong> can't diarize, so Panel mode
              falls back to <strong>local Whisper</strong> for speaker separation. Pick Deepgram or
              AssemblyAI to diarize in the cloud, or turn off Panel mode.
            </span>
          } />
        )}

        <hr className="ta-mt" style={{ border: 0, borderTop: '1px solid var(--surface-border)' }} />
        <div className="ta-flex ta-mt">
          <InputSwitch
            inputId="interviewMode"
            checked={interviewMode}
            onChange={(e) => setInterviewMode(e.value)}
            disabled={busy}
          />
          <label htmlFor="interviewMode" style={{ marginBottom: 0 }}>
            🎤 Interview Mode (per-question coaching, scores, advancement %)
          </label>
        </div>

        {interviewMode && (
          <div className="ta-mt" style={{ paddingLeft: '0.85rem', borderLeft: '3px solid var(--surface-border)' }}>
            <p className="ta-small ta-muted" style={{ marginTop: 0 }}>
              🎥 If the upload is a video, on-screen delivery analysis (body language, emotion,
              eye contact + an annotated video) runs automatically in the same pass.
            </p>
            <div className="ta-flex" style={{ marginBottom: '0.75rem' }}>
              <InputSwitch
                inputId="interviewDeep"
                checked={interviewDeep}
                onChange={(e) => setInterviewDeep(e.value)}
                disabled={busy}
              />
              <label htmlFor="interviewDeep" style={{ marginBottom: 0 }}>
                Deep analysis (slower, more thorough per-question breakdown)
              </label>
            </div>
            <div className="ta-field">
              <label htmlFor="candidateProfile">Candidate profile / résumé (optional)</label>
              <InputTextarea
                id="candidateProfile"
                className="ta-w-full"
                rows={3}
                placeholder="Paste the candidate's résumé or background for more tailored coaching…"
                value={candidateProfile}
                onChange={(e) => setCandidateProfile(e.target.value)}
                disabled={busy}
              />
            </div>
            <div className="ta-field">
              <label htmlFor="profileFile">…or upload a résumé / profile file (PDF, DOCX, TXT)</label>
              <FileUpload
                mode="basic"
                name="profile_file"
                accept=".pdf,.docx,.txt"
                chooseLabel={profileFile ? profileFile.name : 'Choose profile file'}
                auto={false}
                disabled={busy}
                onSelect={(e) => setProfileFile(e.files?.[0] || null)}
                onClear={() => setProfileFile(null)}
              />
            </div>
          </div>
        )}

        <div className="ta-mt">
          <Button
            type="submit"
            label={busy ? 'Processing…' : 'Start transcription'}
            icon={busy ? 'pi pi-spin pi-spinner' : 'pi pi-play'}
            className="ta-w-full"
            size="large"
            disabled={busy || !canSubmit}
          />
        </div>
      </form>
    </Card>
  )
}
