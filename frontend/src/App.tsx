import { useEffect, useRef, useState } from 'react'
import { analyze, emptyDraft } from './api'
import type { Draft, Fact, Result } from './api'
import './App.css'

type Stage = 'input' | 'question' | 'review' | 'correction' | 'done'
type Snapshot = { stage: Stage; draft: Draft; result: Result | null }
const examples = [
  ['Инструмент', 'Дрель электрическая; питание: от сети; мощность: 0,65 кВт; без удара.'],
  ['Мебель', 'Стол письменный; высота: 75 см; материал: дерево.'],
  ['Неполное описание', 'Куртка рабочая для зимы.'],
]

function App() {
  const [stage, setStage] = useState<Stage>('input')
  const [draft, setDraft] = useState<Draft>(emptyDraft)
  const [result, setResult] = useState<Result | null>(null)
  const [history, setHistory] = useState<Snapshot[]>([])
  const [answer, setAnswer] = useState('')
  const [answerUnit, setAnswerUnit] = useState('')
  const [unknown, setUnknown] = useState(false)
  const [editing, setEditing] = useState<Fact | null>(null)
  const [editName, setEditName] = useState('')
  const [editValue, setEditValue] = useState('')
  const [editUnit, setEditUnit] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)
  const busy = useRef(false)
  const descriptionTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const recheckDescription = useRef(false)
  const sequence = useRef(0)
  const controller = useRef<AbortController | null>(null)
  const titleRef = useRef<HTMLHeadingElement>(null)
  const question = result?.next_question
  const canConfirm = !!result && result.status === 'needs_input' && question?.kind === 'code' && result.candidates.length === 1 && result.candidate_count === 1 && result.verification.type_verified && result.verification.ktru_unique
    && result.verification.fields_complete && result.verification.value_verified && result.verification.catalog_scope_complete && !result.verification.blocking_issues && question.options.some(option => option.value === result.candidates[0].ktru_code)

  function invalidate() { busy.current = false; if (descriptionTimer.current) clearTimeout(descriptionTimer.current); sequence.current++; controller.current?.abort(); setLoading(false); setError(''); setCopied(false) }
  useEffect(() => () => { if (descriptionTimer.current) clearTimeout(descriptionTimer.current); sequence.current++; controller.current?.abort() }, [])
  useEffect(() => { titleRef.current?.focus({ preventScroll: true }) }, [stage, question?.id])

  async function submit(next: Draft) {
    if (!next.description.trim()) { setError('Введите описание товара.'); return }
    invalidate()
    const requestId = sequence.current
    const abort = new AbortController()
    controller.current = abort
    busy.current = true; setLoading(true); setDraft(next); setResult(null); if (stage === 'done') setStage('review')
    try {
      const response = await analyze(next, abort.signal)
      if (requestId !== sequence.current) return
      setHistory(items => [...items, { stage, draft, result }])
      setResult(response); setAnswer(''); setAnswerUnit(response.next_question?.unit || ''); setUnknown(false)
      setStage(response.status === 'resolved' && response.selected_ktru_code ? 'done'
        : response.status === 'needs_input' && response.next_question && response.next_question.kind !== 'code' && response.next_question.kind !== 'conflict' ? 'question' : 'review')
    } catch (failure) {
      if (requestId !== sequence.current || abort.signal.aborted) return
      setError(failure instanceof Error ? failure.message : 'Не удалось связаться с API. Повторите запрос.')
    } finally { if (requestId === sequence.current) { busy.current = false; setLoading(false) } }
  }

  function changeDescription() {
    invalidate(); recheckDescription.current = true; setResult(null)
    setDraft(current => ({ ...current, confirmed_ktru_code: null })); setStage('input')
  }
  function editDescription(value: string) {
    invalidate(); setResult(null)
    const next: Draft = { ...draft, description: value, attributes: draft.attributes.map(item => ({ ...item, replaces: null })),
      confirmed_category_id: null, confirmed_ktru_code: null, answers: [] }
    setDraft(next)
    if (recheckDescription.current && value.trim()) descriptionTimer.current = setTimeout(() => void submit(next), 700)
  }
  function back() {
    invalidate()
    const previous = history.at(-1)
    if (previous) {
      setDraft(previous.draft); setResult(previous.result); setStage(previous.stage); setHistory(items => items.slice(0, -1))
    } else setStage('input')
  }
  function edit(fact: Fact | null) {
    invalidate(); setDraft(current => ({ ...current, confirmed_ktru_code: null })); setEditing(fact); setEditName(fact?.name || ''); setEditValue(fact?.value || ''); setEditUnit(fact?.unit || '')
    setHistory(items => [...items, { stage, draft, result }]); setStage('correction')
  }
  function applyEdit() {
    if (busy.current) return
    if (!editName.trim() || !editValue.trim()) { setError('Укажите имя и значение характеристики.'); return }
    const existing = draft.attributes.find(item => item.id === editing?.id || (!!editing && item.replaces === editing.id))
    const item = { id: existing?.id || `input:${crypto.randomUUID()}`, name: editName, value: editValue, unit: editUnit || null,
      unknown: false, subject: existing?.subject || (editing?.subject === 'packaging' ? 'packaging' as const : editing?.subject === 'unspecified' ? 'unspecified' as const : 'product' as const),
      replaces: existing?.replaces || editing?.id || null }
    void submit({ ...draft, attributes: [...draft.attributes.filter(fact => fact.id !== item.id), item], confirmed_ktru_code: null })
  }
  function submitAnswer() {
    if (!question || busy.current) return
    if (!unknown && !answer.trim()) { setError('Выберите ответ или укажите «Не знаю».'); return }
    const next = { ...draft, confirmed_ktru_code: question.kind === 'correction' ? draft.confirmed_ktru_code : null, answers: [...draft.answers.filter(item => item.question_id !== question.id),
      { question_id: question.id, value: unknown ? null : answer, unit: answerUnit || null, unknown }] }
    if (question.kind === 'category') next.confirmed_category_id = unknown ? null : answer
    void submit(next)
  }
  async function copy() {
    if (!result?.selected_ktru_code) return
    try { await navigator.clipboard.writeText(result.selected_ktru_code); setCopied(true) }
    catch { setError('Выделите код и скопируйте его вручную.') }
  }
  function reset() { invalidate(); recheckDescription.current = false; setDraft(emptyDraft()); setResult(null); setStage('input'); setHistory([]) }
  const title = stage === 'input' ? 'Описание товара' : stage === 'question' ? question?.title || 'Уточните сведения'
    : stage === 'correction' ? 'Исправьте характеристику' : stage === 'done' ? 'Подбор завершён' : question?.title || 'Проверьте результат'
  const currentStep = stage === 'input' ? 0 : stage === 'question' || stage === 'correction' ? 1 : 2

  return <main className="catalog-page">
    <div className="page-context">Каталог товаров <span aria-hidden="true">/</span> Новая позиция</div>
    <div className="page-heading"><h1>Подбор КТРУ</h1><span className="preview-label">API</span></div>
    <section className="picker" aria-label="Подбор кода КТРУ" aria-busy={loading}>
      <ol className="progress" aria-label="Этапы подбора">
        {['Описание', 'Уточнение', 'Результат'].map((label, index) => <li key={label} className={index === currentStep ? 'current' : index < currentStep ? 'complete' : ''} aria-current={index === currentStep ? 'step' : undefined}><span>{index + 1}.</span> {label}</li>)}
      </ol>
      <div className="step-content">
        <h2 ref={titleRef} tabIndex={-1} className="step-title">{title}</h2>
        {stage === 'input' && <form id="description-form" onSubmit={event => { event.preventDefault(); void submit(draft) }}>
          <p className="help">Укажите товар, известные характеристики и единицы. Исходное описание сохранится между шагами.</p>
          <label className="sr-only" htmlFor="description">Описание товара</label>
          <textarea aria-describedby={error ? 'form-error' : undefined} id="description" value={draft.description} maxLength={12000} onChange={event => editDescription(event.target.value)} placeholder="Например: дрель электрическая; мощность: 650 Вт" aria-invalid={!!error} />
          <div className="example-links"><span>Примеры:</span>{examples.map(([label, text]) => <button type="button" key={label} className="link-button" onClick={() => editDescription(text)}>{label}</button>)}</div>
        </form>}
        {stage === 'question' && question && <form id="question-form" onSubmit={event => { event.preventDefault(); submitAnswer() }}>
          <p className="help">{question.reason}</p>
          {question.options.length > 0 ? <fieldset className="radio-list" disabled={loading}><legend className="sr-only">{question.title}</legend>
            {question.options.map(option => <label className="radio-row" key={option.value}><input type="radio" name="answer" checked={!unknown && answer === option.value} onChange={() => { setAnswer(option.value); setUnknown(false); setError('') }} /><span>{option.label}</span></label>)}
          </fieldset> : <div className="measurement"><label className="sr-only" htmlFor="answer">Значение</label><input aria-describedby={error ? 'form-error' : undefined} aria-invalid={!!error} id="answer" value={answer} disabled={unknown || loading} onChange={event => setAnswer(event.target.value)} placeholder="Значение" />
            <label className="sr-only" htmlFor="answer-unit">Единица</label><input id="answer-unit" value={answerUnit} disabled={unknown || loading} onChange={event => setAnswerUnit(event.target.value)} placeholder="Единица" /></div>}
          {question.allow_unknown && <label className="radio-row"><input type="radio" name="answer" checked={unknown} disabled={loading} onChange={() => { setUnknown(true); setError('') }} /><span>Не знаю</span></label>}
          <button type="button" className="link-button" onClick={() => edit(null)}>Добавить или исправить сведения</button>
        </form>}
        {stage === 'correction' && <form id="correction-form" onSubmit={event => { event.preventDefault(); applyEdit() }}>
          <p className="help">Правка запускает полную проверку. Остальные сведения сохранятся.</p>
          <div className="edit-fields"><label>Характеристика<input value={editName} onChange={event => setEditName(event.target.value)} maxLength={256} /></label>
            <label>Значение<input value={editValue} onChange={event => setEditValue(event.target.value)} maxLength={512} /></label>
            <label>Единица<input value={editUnit} onChange={event => setEditUnit(event.target.value)} maxLength={64} /></label></div>
        </form>}
        {result && stage !== 'input' && stage !== 'correction' && <div className="result">
          {stage === 'review' && question && <p className="help">{question.reason}</p>}
          {result.status === 'needs_data' && <p className="data-notice" role="status">Сейчас сервису недостаточно данных для проверенного подбора. Описание и характеристики сохранены; причина и следующие шаги указаны ниже.</p>}
          {result.status === 'ambiguous' && <p className="data-notice" role="status">Осталось несколько подходящих позиций. Однозначный код пока не определён — дополните или исправьте характеристики.</p>}
          {result.next_steps.map(step => <p className="help compact" key={step}>{step}</p>)}
          {result.facts.length > 0 && <><h3 className="section-label">Характеристики</h3><dl className="result-data">{result.facts.map(fact => <div key={fact.id}><dt>{fact.name || 'Неустановленное свойство'}</dt><dd>{fact.unknown ? 'Не знаю' : `${fact.value} ${fact.unit || ''}`} {fact.normalized_value !== null && (fact.normalized_value !== fact.value || fact.normalized_unit !== fact.unit) && <small> → {fact.normalized_value} {fact.normalized_unit}</small>} <button type="button" className="link-button" onClick={() => edit(fact)}>Исправить</button></dd></div>)}</dl></>}
          {result.corrections.map((item, index) => <p className="unit-note" key={index}>{item.original} → {item.normalized}. {item.explanation}{item.confirmation_required && ' Требуется подтверждение — исходный факт пока не изменён.'}</p>)}
          {result.candidates.length > 0 && <><h3 className="section-label">Конечные позиции: {result.candidate_count}</h3>
            {result.candidates.map(candidate => <div className="candidate" key={candidate.ktru_code}><strong>{candidate.name}</strong><div className="result-code">{candidate.ktru_code}</div>
              <p className="help compact">{candidate.outcome === 'compatible' ? 'Значения совместимы' : candidate.outcome === 'incompatible' ? 'Значения несовместимы' : 'Проверка не завершена'}{candidate.missing_required.length > 0 && ` · Требуется: ${candidate.missing_required.join(', ')}`}</p></div>)}</>}
          {result.excluded.length > 0 && <details><summary>Исключено позиций: {result.excluded_count}</summary>{result.excluded.map(candidate => <p key={candidate.ktru_code}>{candidate.name}: {candidate.checks.filter(check => check.outcome === 'incompatible').map(check => `${check.name} — ${check.reason}`).join('; ')}</p>)}</details>}
          {(result.unparsed.length > 0 || result.additional.length > 0) && <details><summary>Неразобранные и дополнительные сведения</summary>{[...result.unparsed, ...result.additional].map((item, index) => <p key={index}>{item.raw} {item.id?.startsWith('fragment:') && <button className="link-button" onClick={() => edit({ id: item.id!, name: '', value: item.raw, unit: null, raw: item.raw, source: 'text', subject: 'product', unknown: false, normalized_value: null, normalized_unit: null })}>Указать характеристику</button>}</p>)}</details>}
          {result.issues.map((issue, index) => <p className={issue.severity === 'error' ? 'form-error' : 'help compact'} key={index}>{issue.explanation} {issue.suggested_action}</p>)}
          {stage === 'done' && <div className="result-name"><h3 className="result-code">{result.selected_ktru_code}</h3><span className="confirmed">Подтверждено и проверено</span></div>}
          <div className="known-data"><span>Проверено позиций: {result.scope.evaluated_count}</span><span>{result.scope.complete ? 'Область проверена полностью' : 'Покрытие не подтверждено'}</span></div>
        </div>}
        {loading && <p className="help" role="status">Проверяем описание и характеристики… Предыдущий результат больше не действует.</p>}
        {error && <p className="form-error" id="form-error" role="alert">{error}</p>}
      </div>
      <div className="action-bar">
        {stage === 'input' ? <span className="action-hint">Описание сохранится на следующих шагах</span> : <button className="link-button back-button" onClick={back}>Назад</button>}
        <div className="action-buttons">
          {draft.confirmed_category_id && stage !== 'input' && stage !== 'correction' && <button className="secondary" disabled={loading} onClick={() => void submit({ ...draft, confirmed_category_id: null, confirmed_ktru_code: null, answers: draft.answers.filter(item => item.question_id !== 'category') })}>Изменить категорию</button>}
          {stage === 'input' && <button className="primary" type="submit" form="description-form" disabled={loading}>Подобрать КТРУ</button>}
          {error && stage === 'question' && <button className="primary" disabled={loading} onClick={() => void submit(draft)}>Повторить проверку</button>}
          {stage === 'question' && question && <button className="primary" type="submit" form="question-form" disabled={loading}>Продолжить</button>}
          {stage === 'correction' && <button className="primary" type="submit" form="correction-form" disabled={loading}>Проверить правку</button>}
          {stage === 'review' && <><button className="secondary" onClick={changeDescription}>Изменить описание</button><button className="secondary" onClick={() => edit(null)}>Добавить характеристику</button>
            {canConfirm && <button className="primary" disabled={loading} onClick={() => void submit({ ...draft, confirmed_ktru_code: question!.options.find(option => option.value === result!.candidates[0].ktru_code)!.value })}>Подтвердить код</button>}
            {(result?.status === 'needs_data' || !!error) && <button className="primary" disabled={loading} onClick={() => void submit(draft)}>Повторить проверку</button>}</>}
          {stage === 'done' && <><button className="secondary" onClick={changeDescription}>Изменить описание</button><button className="secondary" onClick={reset}>Новый подбор</button><button className="primary" onClick={() => void copy()}>{copied ? 'Скопировано' : 'Скопировать код'}</button></>}
        </div>
      </div>
    </section>
    <div className="preview-note">{result ? `Каталог: ${result.catalog_version} · Алгоритм: ${result.engine_version}` : 'Категорию и код подтверждает пользователь. Значения проверяет API.'}</div>
  </main>
}
export default App
