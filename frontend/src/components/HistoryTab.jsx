import React, { useEffect, useState, useCallback } from 'react'
import { Card } from 'primereact/card'
import { DataTable } from 'primereact/datatable'
import { Column } from 'primereact/column'
import { Button } from 'primereact/button'
import { Tag } from 'primereact/tag'
import { Message } from 'primereact/message'
import { TabView, TabPanel } from 'primereact/tabview'
import { ProgressSpinner } from 'primereact/progressspinner'
import { getHistory, deleteHistory, getTrash, restoreTrash, emptyTrash } from '../api.js'

const money = (n) => (n == null ? '—' : n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(3)}`)
const num = (n) => (n || 0).toLocaleString()

function ScoreCell({ row }) {
  if (!row.overall_verdict && (row.overall_score === '' || row.overall_score == null)) return '—'
  return (
    <span className="ta-flex" style={{ gap: '0.35rem' }}>
      {row.overall_verdict && <Tag severity="info" value={row.overall_verdict} />}
      {row.overall_score !== '' && row.overall_score != null && <span className="ta-small">{row.overall_score}/10</span>}
    </span>
  )
}

function HistoryPanel() {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [busyId, setBusyId] = useState(null)

  const load = useCallback(async () => {
    setLoading(true); setError('')
    try { setData(await getHistory()) }
    catch (e) { setError(e.message) }
    finally { setLoading(false) }
  }, [])

  useEffect(() => { load() }, [load])

  async function onDelete(id) {
    setBusyId(id)
    try { await deleteHistory(id); await load() }
    catch (e) { setError(e.message) }
    finally { setBusyId(null) }
  }

  const entries = data?.entries || []

  return (
    <>
      <div className="ta-flex" style={{ justifyContent: 'space-between', marginBottom: '0.75rem' }}>
        <div className="ta-flex">
          <span>Total spend: <Tag severity="success" value={money(data?.total_cost_usd)} /></span>
          <span className="ta-muted ta-small">
            {num(data?.total_tok_in)}↑ / {num(data?.total_tok_out)}↓ tokens · {data?.count || 0} runs
          </span>
        </div>
        <Button label="Refresh" icon="pi pi-refresh" outlined size="small" onClick={load} loading={loading} />
      </div>

      {error && <Message severity="error" className="ta-w-full" style={{ marginBottom: '0.75rem' }} text={error} />}

      {loading ? (
        <div style={{ textAlign: 'center', padding: '2rem' }}><ProgressSpinner style={{ width: 40, height: 40 }} /></div>
      ) : entries.length === 0 ? (
        <p className="ta-muted" style={{ marginBottom: 0 }}>
          No runs yet. Past runs are saved to <code>history.jsonl</code> and persist across restarts (shared with the desktop app).
        </p>
      ) : (
        <DataTable value={entries} size="small" stripedRows scrollable scrollHeight="480px">
          <Column field="timestamp" header="When" style={{ whiteSpace: 'nowrap' }} />
          <Column field="filename" header="File" style={{ maxWidth: 240 }} body={(r) => (
            <span title={r.filename} style={{ display: 'block', overflow: 'hidden', textOverflow: 'ellipsis' }}>{r.filename}</span>
          )} />
          <Column field="ai_model" header="Model" body={(r) => r.ai_model || '—'} />
          <Column header="Score" body={(r) => <ScoreCell row={r} />} />
          <Column header="Tokens" align="right" body={(r) => `${num(r.tok_in)}↑ ${num(r.tok_out)}↓`} />
          <Column header="Cost" align="right" body={(r) => <strong>{money(r.cost_usd)}</strong>} />
          <Column header="" align="right" body={(r) => (
            <Button
              icon={busyId === r.id ? 'pi pi-spin pi-spinner' : 'pi pi-trash'}
              severity="danger" outlined size="small" title="Move to trash"
              disabled={busyId === r.id} onClick={() => onDelete(r.id)}
            />
          )} />
        </DataTable>
      )}
    </>
  )
}

function TrashPanel() {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [busyId, setBusyId] = useState(null)
  const [emptying, setEmptying] = useState(false)

  const load = useCallback(async () => {
    setLoading(true); setError('')
    try { setData(await getTrash()) }
    catch (e) { setError(e.message) }
    finally { setLoading(false) }
  }, [])

  useEffect(() => { load() }, [load])

  async function onRestore(id) {
    setBusyId(id)
    try { await restoreTrash(id); await load() }
    catch (e) { setError(e.message) }
    finally { setBusyId(null) }
  }

  async function onEmpty() {
    setEmptying(true)
    try { await emptyTrash(); await load() }
    catch (e) { setError(e.message) }
    finally { setEmptying(false) }
  }

  const entries = data?.entries || []

  return (
    <>
      <div className="ta-flex" style={{ justifyContent: 'space-between', marginBottom: '0.75rem' }}>
        <span className="ta-muted ta-small">{data?.count || 0} trashed run(s)</span>
        <div className="ta-flex">
          <Button label="Refresh" icon="pi pi-refresh" outlined size="small" onClick={load} loading={loading} />
          <Button label="Empty trash" icon="pi pi-trash" severity="danger" outlined size="small"
            disabled={entries.length === 0} loading={emptying} onClick={onEmpty} />
        </div>
      </div>

      {error && <Message severity="error" className="ta-w-full" style={{ marginBottom: '0.75rem' }} text={error} />}

      {loading ? (
        <div style={{ textAlign: 'center', padding: '2rem' }}><ProgressSpinner style={{ width: 40, height: 40 }} /></div>
      ) : entries.length === 0 ? (
        <p className="ta-muted" style={{ marginBottom: 0 }}>Trash is empty.</p>
      ) : (
        <DataTable value={entries} size="small" stripedRows scrollable scrollHeight="480px">
          <Column field="timestamp" header="When" style={{ whiteSpace: 'nowrap' }} />
          <Column field="filename" header="File" style={{ maxWidth: 240 }} body={(r) => (
            <span title={r.filename} style={{ display: 'block', overflow: 'hidden', textOverflow: 'ellipsis' }}>{r.filename}</span>
          )} />
          <Column field="ai_model" header="Model" body={(r) => r.ai_model || '—'} />
          <Column header="Score" body={(r) => <ScoreCell row={r} />} />
          <Column header="Cost" align="right" body={(r) => <strong>{money(r.cost_usd)}</strong>} />
          <Column header="" align="right" body={(r) => (
            <Button
              icon={busyId === r.id ? 'pi pi-spin pi-spinner' : 'pi pi-undo'}
              outlined size="small" title="Restore to history"
              disabled={busyId === r.id} onClick={() => onRestore(r.id)}
            />
          )} />
        </DataTable>
      )}
    </>
  )
}

export default function HistoryTab() {
  return (
    <Card className="ta-w-full">
      <TabView>
        <TabPanel header="History">
          <HistoryPanel />
        </TabPanel>
        <TabPanel header="🗑 Trash">
          <TrashPanel />
        </TabPanel>
      </TabView>
    </Card>
  )
}
