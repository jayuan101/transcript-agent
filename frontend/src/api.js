// Thin client for the Transcript Agent FastAPI backend (api.py).

export async function startTranscription(formData) {
  const res = await fetch('/api/transcribe', { method: 'POST', body: formData })
  if (!res.ok) throw new Error((await safeDetail(res)) || `Upload failed (HTTP ${res.status})`)
  return res.json() // { job_id, status, poll_url }
}

export async function startVideoAnalysis(formData) {
  const res = await fetch('/api/analyze-video', { method: 'POST', body: formData })
  if (!res.ok) throw new Error((await safeDetail(res)) || `Upload failed (HTTP ${res.status})`)
  return res.json()
}

export async function transcribeClip(blob, { whisperModel = 'tiny', language = '' } = {}) {
  const fd = new FormData()
  fd.append('file', new File([blob], 'clip.webm', { type: blob.type || 'audio/webm' }))
  fd.append('whisper_model', whisperModel)
  if (language) fd.append('language', language)
  const res = await fetch('/api/transcribe-clip', { method: 'POST', body: fd })
  if (!res.ok) throw new Error((await safeDetail(res)) || `Transcription failed (HTTP ${res.status})`)
  return res.json() // { text, language, seconds }
}

export async function getJob(jobId) {
  const res = await fetch(`/api/jobs/${jobId}`)
  if (!res.ok) throw new Error((await safeDetail(res)) || `Job poll failed (HTTP ${res.status})`)
  return res.json()
}

export function downloadUrl(jobId, name) {
  return `/api/jobs/${jobId}/download/${encodeURIComponent(name)}`
}

export async function getDevices() {
  try {
    const res = await fetch('/api/devices')
    if (!res.ok) return null
    return res.json() // { gpu_available, device, name, kind, reason? }
  } catch { return null }
}

export async function getHistory() {
  const res = await fetch('/api/history')
  if (!res.ok) throw new Error((await safeDetail(res)) || `History load failed (HTTP ${res.status})`)
  return res.json() // { entries, count, total_cost_usd, total_tok_in, total_tok_out }
}

export async function deleteHistory(id) {
  const res = await fetch(`/api/history/${encodeURIComponent(id)}`, { method: 'DELETE' })
  if (!res.ok) throw new Error((await safeDetail(res)) || `Delete failed (HTTP ${res.status})`)
  return res.json()
}

export async function getTrash() {
  const res = await fetch('/api/trash')
  if (!res.ok) throw new Error((await safeDetail(res)) || `Trash load failed (HTTP ${res.status})`)
  return res.json() // { entries, count }
}

export async function restoreTrash(id) {
  const res = await fetch(`/api/trash/${encodeURIComponent(id)}/restore`, { method: 'POST' })
  if (!res.ok) throw new Error((await safeDetail(res)) || `Restore failed (HTTP ${res.status})`)
  return res.json()
}

export async function emptyTrash() {
  const res = await fetch('/api/trash/empty', { method: 'POST' })
  if (!res.ok) throw new Error((await safeDetail(res)) || `Empty trash failed (HTTP ${res.status})`)
  return res.json()
}

export async function cancelJob(jobId) {
  const res = await fetch(`/api/jobs/${jobId}/cancel`, { method: 'POST' })
  if (!res.ok) throw new Error((await safeDetail(res)) || `Cancel failed (HTTP ${res.status})`)
  return res.json()
}

// Regenerate PDF & DOCX, optionally translated into another language.
export async function regenerateReports(jobId, { targetLang, provider, model, baseUrl, llmApiKey } = {}) {
  const fd = new FormData()
  if (targetLang) fd.append('target_lang', targetLang)
  if (provider) fd.append('provider', provider)
  if (model) fd.append('model', model)
  if (baseUrl) fd.append('base_url', baseUrl)
  if (llmApiKey) fd.append('llm_api_key', llmApiKey)
  const res = await fetch(`/api/jobs/${jobId}/regenerate`, { method: 'POST', body: fd })
  if (!res.ok) throw new Error((await safeDetail(res)) || `Regenerate failed (HTTP ${res.status})`)
  return res.json() // { pdf, docx, downloads }
}

export async function checkUpdate() {
  try {
    const res = await fetch('/api/update-check')
    if (!res.ok) return null
    return res.json() // { current, latest, update_available, url, notes }
  } catch { return null }
}

export async function checkHealth() {
  try {
    const res = await fetch('/health')
    return res.ok
  } catch {
    return false
  }
}

// Poll a job until it reaches a terminal state, calling onUpdate on each tick.
export function pollJob(jobId, onUpdate, { intervalMs = 2000 } = {}) {
  let stopped = false
  let timer = null
  const tick = async () => {
    if (stopped) return
    try {
      const job = await getJob(jobId)
      onUpdate(job)
      if (job.status === 'done' || job.status === 'error') return
    } catch (e) {
      onUpdate({ status: 'error', error: e.message })
      return
    }
    timer = setTimeout(tick, intervalMs)
  }
  tick()
  return () => { stopped = true; clearTimeout(timer) }
}

async function safeDetail(res) {
  try {
    const data = await res.json()
    return data.detail || data.error || null
  } catch {
    return null
  }
}
