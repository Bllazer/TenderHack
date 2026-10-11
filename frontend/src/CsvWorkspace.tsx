import { useEffect, useRef, useState } from 'react'
import { useSessionState } from './session'
import { analyze, emptyDraft, userMessage } from './api'
import type { Draft, Result } from './api'
export type CsvItem = { line: number; draft: Draft; result: Result | null; error: string }
export default function CsvWorkspace({ onOpen, update, onRemove }: { onOpen: (item: CsvItem) => void; update: CsvItem | null; onRemove: () => void }) {
  const [rows, setRows] = useSessionState<CsvItem[]>('csv.rows', [])
  const [filename, setFilename] = useSessionState('csv.filename', '')
  const [notes, setNotes] = useSessionState<string[]>('csv.notes', [])
  const [error, setError] = useState('')
  const [copiedLine, setCopiedLine] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useSessionState('csv.progress', 0)
  const [skipHeader, setSkipHeader] = useSessionState('csv.skipHeader', true)
  const abort = useRef<AbortController | null>(null)
  useEffect(() => () => abort.current?.abort(), [])
  useEffect(() => { if (update) setRows(items => items.map(item => item.line === update.line ? update : item)) }, [update])
  async function upload(file?: File) {
    if (!file || busy) return
    setError(''); setProgress(0)
    if (!file.name.toLowerCase().endsWith('.csv')) { setError('Выберите файл CSV.'); return }
    if (file.size > 2_000_000) { setError('Размер файла должен быть не больше 2 МБ.'); return }
    const controller = new AbortController(); abort.current = controller; setBusy(true)
    try {
      const bytes = new Uint8Array(await file.arrayBuffer())
      let binary = ''; for (const byte of bytes) binary += String.fromCharCode(byte)
      const response = await fetch('/api/v1/analyses/csv', { method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: controller.signal,
        body: JSON.stringify({ csv_base64: btoa(binary), filename: file.name, run_limit: 100, skip_header: skipHeader }) })
      const data = await response.json()
      if (!response.ok) throw new Error(typeof data.detail?.message === 'string' ? userMessage(data.detail.message) : 'Не удалось прочитать файл. Проверьте формат CSV.')
      setRows(data.rows.map((item: { line: number; description: string }) => ({ line: item.line, draft: { ...emptyDraft(), description: item.description }, result: null, error: '' })))
      setFilename(file.name); setProgress(0)
      setNotes([...data.notes, ...(data.truncated ? ['Взяты только первые 100 позиций. Разделите большой файл на части.'] : []), ...data.rows_skipped.map((item: { line: number; reason: string }) => `Строка ${item.line} пропущена: ${userMessage(item.reason)}`)])
    } catch (e) { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : 'Не удалось загрузить файл.') }
    finally { setBusy(false) }
  }
  async function run() {
    const controller = new AbortController(); abort.current = controller; setBusy(true); setError(''); setProgress(0)
    try {
      for (let i = 0; i < rows.length; i++) {
        if (controller.signal.aborted) break
        const item = rows[i]
        {
          try {
            const result = await analyze(item.draft, controller.signal)
            setRows(current => current.map(row => row.line === item.line ? { ...row, result, error: '' } : row))
          } catch (e) {
            if (controller.signal.aborted) break
            setRows(current => current.map(row => row.line === item.line ? { ...row, error: e instanceof Error ? e.message : 'Не удалось проверить товар' } : row))
          }
        }
        setProgress(i + 1)
      }
    } finally { setBusy(false) }
  }
  function removeFile() {
    if (busy) return
    setRows([]); setFilename(''); setNotes([]); setError(''); setProgress(0); setCopiedLine(null); onRemove()
  }
  async function copyCode(item: CsvItem) {
    const code = item.result?.selected_ktru_code
    if (!code) return
    try { await navigator.clipboard.writeText(code); setCopiedLine(item.line); setError('') }
    catch { setError('Не удалось скопировать код. Выделите его и скопируйте вручную.') }
  }
  function exportCsv() {
    const quote = (value: string) => '"' + value.replaceAll('"', '""') + '"'
    const text = '\ufeff' + [['Строка', 'Описание', 'Состояние', 'Подтверждённый КТРУ'], ...rows.map(row => [String(row.line), row.draft.description, row.error || label(row.result), row.result?.selected_ktru_code || ''])].map(row => row.map(quote).join(';')).join('\r\n')
    const url = URL.createObjectURL(new Blob([text], { type: 'text/csv;charset=utf-8' })); const link = document.createElement('a'); link.href = url; link.download = 'Результаты подбора.csv'; link.click(); URL.revokeObjectURL(url)
  }

  const checked = rows.filter(row => row.result || row.error).length

  return <section className="picker csv-workspace" aria-label="Подбор из CSV">
    <div className="csv-page-title"><div><h2>{filename ? 'Товары из файла' : 'Загрузка товаров из CSV'}</h2><p className="help">{filename ? 'Проверьте описания и уточните позиции, для которых не определён код.' : 'Добавьте список товаров — подберём характеристики и позиции КТРУ для каждой строки.'}</p></div></div>
    {!filename && <div className="csv-upload-panel">    <label className="csv-header"><input type="checkbox" checked={skipHeader} disabled={busy} onChange={e => setSkipHeader(e.target.checked)} />Первая строка содержит названия колонок</label>
    <div className="csv-drop" onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); void upload(e.dataTransfer.files[0]) }}><label className="secondary csv-upload">Выбрать CSV<input type="file" accept=".csv,text/csv" disabled={busy} onChange={e => { void upload(e.target.files?.[0]); e.target.value = '' }} /></label><span>или перетащите файл сюда</span></div>
<p className="csv-format">CSV · до 100 товаров · до 2 МБ. Пример характеристики: «Мощность: 650 Вт».</p></div>}
    {filename && <details className="csv-replace"><summary>Загрузить другой файл</summary>    <label className="csv-header"><input type="checkbox" checked={skipHeader} disabled={busy} onChange={e => setSkipHeader(e.target.checked)} />Первая строка содержит названия колонок</label>
    <div className="csv-drop" onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); void upload(e.dataTransfer.files[0]) }}><label className="secondary csv-upload">Выбрать CSV<input type="file" accept=".csv,text/csv" disabled={busy} onChange={e => { void upload(e.target.files?.[0]); e.target.value = '' }} /></label><span>или перетащите файл сюда</span></div>
</details>}
    {error && <p className="form-error" role="alert">{error}</p>}
    {notes.map((note, i) => <p className="data-notice" key={i}>{note}</p>)}
    {filename && <div className="csv-toolbar"><div className="csv-file-heading"><span className="csv-file-icon" aria-hidden="true">CSV</span><div><h3>{filename}</h3><span className="csv-file-meta">{rows.length} позиций · изменения сохраняются в этой вкладке</span></div><button type="button" className="link-button csv-remove" disabled={busy} onClick={removeFile}>Убрать файл</button></div><div className="csv-actions"><button className="secondary" disabled={busy} onClick={exportCsv}>Скачать результаты</button>{busy ? <button className="secondary" onClick={() => abort.current?.abort()}>Остановить</button> : <button className="primary" disabled={rows.some(row => !row.draft.description.trim())} onClick={() => void run()}>{checked > 0 ? 'Перепроверить товары' : 'Проверить товары'}</button>}</div></div>}
    {busy && <div className="csv-processing" role="status" aria-live="polite"><span className="csv-spinner" aria-hidden="true" /><div><strong>{rows.length ? 'Подбираем характеристики товаров' : 'Читаем CSV-файл'}</strong><p>{rows.length ? `Проверено ${progress} из ${rows.length}. Можно остановить обработку — готовые результаты сохранятся.` : 'Распознаём строки и проверяем формат файла…'}</p></div>{rows.length > 0 && <progress aria-label="Прогресс проверки товаров" value={progress} max={rows.length} />}</div>}
    {rows.length > 0 && <><p className="csv-table-hint">Описание можно редактировать прямо в таблице. «Уточнить» открывает пошаговый подбор.</p><div className="csv-table-wrap"><table className="csv-table"><thead><tr><th>Строка</th><th>Описание товара</th><th>Результат</th><th>Действие</th></tr></thead><tbody>{rows.map(item => <tr key={item.line} className={item.result?.status === 'resolved' ? 'csv-confirmed-row' : ''}><td><span className="csv-line-number">{item.line}</span></td><td><textarea ref={element => { if (element) { element.style.height = 'auto'; element.style.height = element.scrollHeight + 'px' } }} onInput={event => { const element = event.currentTarget; element.style.height = 'auto'; element.style.height = element.scrollHeight + 'px' }} aria-label={`Описание строки ${item.line}`} value={item.draft.description} disabled={busy} maxLength={12000} onChange={e => setRows(current => current.map(row => row.line === item.line ? { ...row, draft: { ...emptyDraft(), description: e.target.value }, result: null, error: '' } : row))} /></td><td><span className={`csv-status ${item.error ? 'status-error' : item.result?.status === 'resolved' ? 'status-success' : item.result?.status === 'needs_data' ? 'status-error' : item.result ? 'status-attention' : 'status-pending'}`}><span className="status-dot" aria-hidden="true" />{item.error ? 'Не удалось проверить' : label(item.result)}</span>{item.error && <p className="csv-row-error">{item.error}</p>}{item.result?.selected_ktru_code && <div className="csv-code"><span className="result-code">{item.result.selected_ktru_code}</span><button type="button" className="link-button" onClick={() => void copyCode(item)} aria-label={`Копировать код строки ${item.line}`}>{copiedLine === item.line ? 'Скопировано' : 'Копировать'}</button></div>}</td><td><button className="secondary" disabled={busy || !item.draft.description.trim()} onClick={() => onOpen(item)}>{item.result?.status === 'resolved' ? 'Посмотреть' : item.result ? 'Уточнить' : 'Открыть'}</button></td></tr>)}</tbody></table></div></>}
  </section>
}
function label(result: Result | null) {
  if (!result) return 'Не проверено'
  if (result.status === 'resolved') return 'Код подтверждён'
  if (result.status === 'needs_data') return 'Проверка недоступна'
  if (result.next_question?.kind === 'category') return 'Уточните тип товара'
  if (result.next_question?.kind === 'code') return 'Подтвердите код'
  if (result.candidate_count > 1) return `Найдено вариантов: ${result.candidate_count}`
  return 'Нужно уточнение'
}
