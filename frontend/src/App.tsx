import { useEffect, useRef, useState } from 'react'
import { analyze, catalogPosition, emptyDraft, userMessage } from './api'
import type { CatalogPositionCard, Draft, Fact, Result } from './api'
import type { KeyboardEvent } from 'react'
import './App.css'
import { useSessionState, readSession, writeSession } from './session'
import CsvWorkspace from './CsvWorkspace'
import type { CsvItem } from './CsvWorkspace'

type Stage = 'input' | 'question' | 'review' | 'correction' | 'done'
/** Есть ли открытый вопрос, который пользователь должен закрыть до результата. */
function hasPendingQuestion(result: Result): boolean {
  return result.status === 'needs_input' && !!result.next_question
    && !['code', 'conflict'].includes(result.next_question.kind)
}
function resultStage(result: Result): Stage {
  if (result.status === 'resolved' && result.selected_ktru_code) return 'done'
  // Вопрос важнее «осталась одна позиция»: иначе шаг результата открывался раньше,
  // чем пользователь успевал указать требуемую характеристику (например, длину).
  if (hasPendingQuestion(result)) return 'question'
  if (result.candidate_count === 1 || result.categories.length === 0) return 'review'
  return 'review'
}

type Snapshot = { stage: Stage; draft: Draft; result: Result | null }
const examples = [
  ['Инструмент', 'Дрель электрическая; питание: от сети; мощность: 0,65 кВт; без удара.'],
  ['Мебель', 'Стол письменный; высота: 75 см; материал: дерево.'],
  ['Неполное описание', 'Куртка рабочая для зимы.'],
]

function App() {
  const [mode, setMode] = useSessionState<'single' | 'csv'>('app.mode', 'single')
  const [csvLine, setCsvLine] = useSessionState<number | null>('app.csvLine', null)
  const [csvUpdate, setCsvUpdate] = useSessionState<CsvItem | null>('app.csvUpdate', null)
  const manual = useRef<Snapshot | null>(readSession('app.manual', null))
  const [stage, setStage] = useSessionState<Stage>('app.stage', 'input')
  const [draft, setDraft] = useSessionState<Draft>('app.draft', emptyDraft)
  const [result, setResult] = useSessionState<Result | null>('app.result', null)
  const [history, setHistory] = useSessionState<Snapshot[]>('app.history', [])
  const [answer, setAnswer] = useSessionState('app.answer', '')
  const [answerUnit, setAnswerUnit] = useSessionState('app.answerUnit', '')
  //: Множественный выбор (question.multiple) передаётся через `values` и хранит ВСЕ
  //: отмеченные элементы: одиночное значение здесь потеряло бы часть набора.
  const [answerSet, setAnswerSet] = useSessionState<string[]>('app.answerSet', [])
  const [unknown, setUnknown] = useSessionState('app.unknown', false)
  const [editing, setEditing] = useSessionState<Fact | null>('app.editing', null)
  const [editName, setEditName] = useSessionState('app.editName', '')
  const [editValue, setEditValue] = useSessionState('app.editValue', '')
  const [editUnit, setEditUnit] = useSessionState('app.editUnit', '')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)
  //: Просмотр карточки КТРУ: справочные правила позиции отдельно от фактов пользователя.
  const [card, setCard] = useState<{ code: string; data: CatalogPositionCard } | null>(null)
  const [cardError, setCardError] = useState('')
  const [descriptionHint, setDescriptionHint] = useSessionState('app.descriptionHint', false)
  const busy = useRef(false)
  const sequence = useRef(0)
  const controller = useRef<AbortController | null>(null)
  const titleRef = useRef<HTMLHeadingElement>(null)
  useEffect(() => { if (!result && (stage === 'question' || stage === 'review' || stage === 'done')) setStage('input') }, [])
  const question = result?.next_question
  const canConfirm = !!result && result.status === 'needs_input' && question?.kind === 'code' && result.candidates.length === 1 && result.candidate_count === 1 && result.verification.type_verified && result.verification.ktru_unique
    && result.verification.fields_complete && result.verification.value_verified && result.verification.catalog_scope_complete && !result.verification.blocking_issues && question.options.some(option => option.value === result.candidates[0].ktru_code)

  function openCsv(item: CsvItem) {
    invalidate(); setCsvLine(item.line); setMode('single'); setDraft(item.draft); setResult(item.result); setHistory([]); setAnswer(''); setAnswerSet([]); setUnknown(false)
    setAnswerUnit(item.result?.next_question?.unit || '')
    setStage(item.result ? resultStage(item.result) : 'input')
    if (!item.result) void submit(item.draft)
  }
  function showCsv() {
    if (mode === 'csv') return
    if (csvLine !== null) setCsvUpdate({ line: csvLine, draft, result, error })
    else { manual.current = { stage, draft, result }; writeSession('app.manual', manual.current) }
    invalidate(); setCsvLine(null); setMode('csv')
  }
  function showSingle() {
    if (manual.current) { setStage(manual.current.stage); setDraft(manual.current.draft); setResult(manual.current.result) }
    else { setStage('input'); setDraft(emptyDraft()); setResult(null) }
    setHistory([]); setAnswer(''); setAnswerSet([]); setUnknown(false); setMode('single')
  }

  function goToStep(index: number) {
    if (loading) return
    if (index === 0) { setStage('input'); return }
    if (!result) return
    if (index === 2) { setStage(result.status === 'resolved' && result.selected_ktru_code ? 'done' : hasPendingQuestion(result) ? 'question' : 'review'); return }
    if (question && !['code', 'conflict'].includes(question.kind)) setStage('question')
  }

  function primaryOnEnter(event: KeyboardEvent<HTMLElement>) {
    if (event.key !== 'Enter' || event.shiftKey || event.altKey || event.ctrlKey || event.metaKey || event.repeat || event.nativeEvent.isComposing) return
    const target = event.target as HTMLElement
    // Keep keyboard activation of focused links and buttons native.
    if (target.closest('button, a, summary, select')) return
    const button = event.currentTarget.querySelector<HTMLButtonElement>('.action-buttons .primary:not(:disabled)')
    if (!button || loading || busy.current) return
    event.preventDefault()
    button.click()
  }

  function invalidate() { busy.current = false; sequence.current++; controller.current?.abort(); setLoading(false); setError(''); setCopied(false) }
  useEffect(() => () => { sequence.current++; controller.current?.abort() }, [])
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
      setResult(response); setEditing(null); setAnswer(''); setAnswerSet([]); setAnswerUnit(response.next_question?.unit || ''); setUnknown(false)
      setStage(resultStage(response))
      if (csvLine !== null && response.status === 'resolved' && response.selected_ktru_code) {
        setCsvUpdate({ line: csvLine, draft: next, result: response, error: '' })
        setCsvLine(null); setMode('csv')
      }
    } catch (failure) {
      if (requestId !== sequence.current || abort.signal.aborted) return
      setError(failure instanceof Error ? failure.message : 'Не удалось связаться с API. Повторите запрос.')
    } finally { if (requestId === sequence.current) { busy.current = false; setLoading(false) } }
  }

  function rejectTypes() {
    invalidate(); setResult(null); setAnswer(''); setAnswerSet([]); setUnknown(false); setHistory([])
    setDraft(current => ({ ...current, confirmed_category_id: null, confirmed_ktru_code: null, answers: [],
      attributes: current.attributes.map(item => ({ ...item, replaces: null })) }))
    setDescriptionHint(true); setStage('input')
    requestAnimationFrame(() => document.getElementById('description')?.focus())
  }

  function changeDescription() {
    invalidate(); setResult(null)
    setDraft(current => ({ ...current, confirmed_ktru_code: null })); setStage('input')
  }
  function editDescription(value: string) {
    invalidate(); setResult(null); setDescriptionHint(false)
    const next: Draft = { ...draft, description: value, attributes: draft.attributes.map(item => ({ ...item, replaces: null })),
      confirmed_category_id: null, confirmed_ktru_code: null, answers: [] }
    setDraft(next)
  }
  function back() {
    invalidate()
    const previous = history.at(-1)
    if (previous) {
      setDraft(previous.draft); setResult(previous.result); setStage(previous.stage); setHistory(items => items.slice(0, -1))
    } else setStage('input')
  }
  async function edit(fact: Fact | null) {
    if (fact && draft.confirmed_category_id && result) {
      invalidate()
      const requestId = sequence.current
      setLoading(true)
      try {
        const response = await fetch(`/api/v1/catalog/categories/${encodeURIComponent(draft.confirmed_category_id)}/properties`)
        if (!response.ok) throw new Error('Не удалось загрузить варианты характеристики. Повторите попытку.')
        const schema = await response.json() as { properties: { name: string; aliases: string[]; subject: string; question_id: string | null; options: string[]; units: string[]; multiple: boolean }[] }
        if (requestId !== sequence.current) return
        const field = schema.properties.find(item => item.subject === fact.subject && [item.name, ...item.aliases].includes(fact.name))
        if (field?.question_id) {
          setHistory(items => [...items, { stage, draft, result }])
          setDraft(current => ({ ...current, confirmed_ktru_code: null }))
          setResult({ ...result, selected_ktru_code: null, next_question: {
            id: field.question_id, kind: 'property', title: `Укажите: ${field.name}`, property_name: field.name,
            reason: 'Измените ответ. После продолжения проверим подбор с новым значением.',
            options: field.options.map(value => ({ value, label: value })), unit: fact.unit || field.units[0] || null,
            multiple: field.multiple, allow_unknown: true,
          } })
          setEditing(fact); setAnswer(fact.unknown ? '' : fact.value); setAnswerSet(fact.values ?? (fact.value ? [fact.value] : [])); setAnswerUnit(fact.unit || field.units[0] || ''); setUnknown(fact.unknown)
          setStage('question')
          return
        }
      } catch (failure) {
        if (requestId === sequence.current) setError(failure instanceof Error ? failure.message : 'Не удалось загрузить характеристику.')
        return
      } finally { if (requestId === sequence.current) setLoading(false) }
    }
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
    const multiple = question.multiple && question.options.length > 0
    const filled = multiple ? answerSet.length > 0 : !!answer.trim()
    if (!unknown && !filled) { setError('Выберите ответ или укажите «Не знаю».'); return }
    if (editing && question.kind === 'property') {
      const existing = draft.attributes.find(item => item.replaces === editing.id || item.id === editing.id)
      const item = { id: existing?.id || `input:${crypto.randomUUID()}`, name: question.property_name || editing.name,
        value: unknown ? '' : answer, unit: answerUnit || null, unknown,
        subject: editing.subject === 'packaging' ? 'packaging' as const : 'product' as const,
        replaces: existing?.replaces || editing.id }
      void submit({ ...draft, confirmed_ktru_code: null, attributes: [...draft.attributes.filter(fact => fact.id !== item.id), item] })
      return
    }
    // Множественный выбор передаётся через values и БЕЗ value: контракт не допускает
    // одновременную передачу двух форм одного ответа (см. `multiple` выше).
    const next = { ...draft, confirmed_ktru_code: question.kind === 'correction' ? draft.confirmed_ktru_code : null, answers: [...draft.answers.filter(item => item.question_id !== question.id),
      multiple
        ? { question_id: question.id, values: unknown ? [] : answerSet, unit: answerUnit || question.unit || null, unknown }
        : { question_id: question.id, value: unknown ? null : answer, unit: answerUnit || question.unit || null, unknown }] }
    if (question.kind === 'category') next.confirmed_category_id = unknown ? null : answer
    void submit(next)
  }
  async function copy() {
    if (!result?.selected_ktru_code) return
    try { await navigator.clipboard.writeText(result.selected_ktru_code); setCopied(true) }
    catch { setError('Выделите код и скопируйте его вручную.') }
  }
  async function openCard(code: string) {
    setCardError('')
    try {
      const data = await catalogPosition(code)
      setCard({ code, data })
    } catch (failure) {
      setCardError(failure instanceof Error ? failure.message : 'Не удалось загрузить карточку КТРУ.')
    }
  }

  function reset() { setDescriptionHint(false); invalidate(); setDraft(emptyDraft()); setResult(null); setStage('input'); setHistory([]) }
  const noSimilar = !!result && result.categories.length === 0 && result.issues.some(issue => issue.code === 'no_similar_products')
  /** Правило каталога человеческим языком: вариант, интервал или «не ограничено». */
  function rule(item: { kind: string; value?: string | null; lower?: string | null; upper?: string | null; unit?: string | null }): string {
    if (item.kind === 'enum') return item.value || '—'
    const unit = item.unit ? ` ${item.unit}` : ''
    if (item.lower && item.upper) return `${item.lower}–${item.upper}${unit}`
    if (item.lower) return `от ${item.lower}${unit}`
    if (item.upper) return `до ${item.upper}${unit}`
    return item.value ? `${item.value}${unit}` : 'не ограничено'
  }

  const title = stage === 'input' ? 'Описание товара' : stage === 'question' ? question?.title || 'Уточните сведения'
    : stage === 'correction' ? 'Исправьте характеристику' : stage === 'done' ? 'Подбор завершён' : noSimilar ? 'Ничего похожего не найдено' : 'Проверьте результат'
  const currentStep = stage === 'input' ? 0 : stage === 'question' || stage === 'correction' ? 1 : 2

  return <><header className="portal-header">
    <a className="portal-brand" href="https://zakupki.mos.ru/" target="_blank" rel="noreferrer"><img src="/portal-logo.svg" alt="Портал поставщиков" /></a>
    <nav className="portal-nav" aria-label="Навигация портала"><a href="https://zakupki.mos.ru/knowledgebase/main" target="_blank" rel="noreferrer">Центр поддержки ↗</a></nav>
    <a className="portal-home" href="https://zakupki.mos.ru/" target="_blank" rel="noreferrer">На портал <span aria-hidden="true">↗</span></a>
  </header><main className="catalog-page">
    <nav className="page-context" aria-label="Хлебные крошки"><span aria-hidden="true">/</span><span className="breadcrumb-current">Подбор КТРУ</span></nav>
    <div className="page-heading"><div><h1>Подбор КТРУ</h1><p className="page-intro">От описания товара — к характеристикам и коду каталога</p></div></div>
    <div className="mode-switch" aria-label="Способ подбора"><button className={mode === 'single' && csvLine === null ? 'active' : ''} disabled={loading} onClick={() => mode === 'csv' ? showSingle() : csvLine !== null ? (showCsv(), showSingle()) : undefined}>Один товар</button><button className={mode === 'csv' || csvLine !== null ? 'active' : ''} disabled={loading} onClick={showCsv}>Загрузить CSV</button></div>
    <div hidden={mode !== 'csv'}><CsvWorkspace onOpen={openCsv} update={csvUpdate} onRemove={() => setCsvUpdate(null)} /></div>
    {csvLine !== null && <div className="csv-row-context"><button className="link-button" disabled={loading} onClick={showCsv}>← К списку товаров</button><span>Строка файла: {csvLine}</span></div>}
    <div hidden={mode !== 'single'}>
    <section onKeyDown={primaryOnEnter} className="picker" aria-label="Подбор кода КТРУ" aria-busy={loading}>
      <ol className="progress" aria-label="Этапы подбора">
        {['Описание', 'Уточнение', 'Результат'].map((label, index) => <li key={label} className={index === currentStep ? 'current' : ''} aria-current={index === currentStep ? 'step' : undefined}><button type="button" disabled={loading || (index === 2 && !result) || (index === 1 && (!question || ['code', 'conflict'].includes(question.kind)))} onClick={() => goToStep(index)} aria-label={`Перейти: ${label}`}><span>{index + 1}.</span>{label}</button></li>)}
      </ol>
      <div className="step-content">
        <h2 ref={titleRef} tabIndex={-1} className="step-title">{title}</h2>
        {stage === 'input' && <form id="description-form" onSubmit={event => { event.preventDefault(); void submit(draft) }}>
          {descriptionHint && <div className="data-notice" role="status"><strong>Предложенные типы не подошли. Уточните описание товара.</strong><p>Напишите точное название и назначение товара, добавьте материал или ключевые характеристики. Например: «набор контейнеров для заморозки пищевых продуктов, пластик, 3 штуки».</p></div>}
          <p className="help">Укажите товар, известные характеристики и единицы. Исходное описание сохранится между шагами.</p>
          <label className="sr-only" htmlFor="description">Описание товара</label>
          <textarea aria-describedby={error ? 'form-error' : undefined} id="description" value={draft.description} maxLength={12000} onChange={event => editDescription(event.target.value)} placeholder="Например: дрель электрическая; мощность: 650 Вт" aria-invalid={!!error} />
          <div className="example-links"><span>Примеры:</span>{examples.map(([label, text]) => <button type="button" key={label} className="link-button" onClick={() => editDescription(text)}>{label}</button>)}</div>
        </form>}
        {stage === 'question' && question && <form id="question-form" onSubmit={event => { event.preventDefault(); submitAnswer() }}>
          <p className="help">{userMessage(question.reason)}</p>
          {question.options.length > 0 ? question.multiple
            ? <fieldset className="radio-list" disabled={loading}><legend className="sr-only">{question.title}</legend>
                {question.options.map(option => <label className="radio-row" key={option.value}><input type="checkbox" name="answer" checked={!unknown && answerSet.includes(option.value)} onChange={() => { setAnswerSet(current => current.includes(option.value) ? current.filter(item => item !== option.value) : [...current, option.value]); setUnknown(false); setError('') }} /><span>{option.label}</span></label>)}
                <p className="help compact">Можно выбрать несколько значений.</p>
              </fieldset>
            : <fieldset className="radio-list" disabled={loading}><legend className="sr-only">{question.title}</legend>
                {question.options.map(option => <label className="radio-row" key={option.value}><input type="radio" name="answer" checked={!unknown && answer === option.value} onChange={() => { setAnswer(option.value); setUnknown(false); setError('') }} /><span>{option.label}</span></label>)}
              </fieldset> : <div className="measurement"><label className="sr-only" htmlFor="answer">Значение</label><input aria-describedby={error ? 'form-error' : undefined} aria-invalid={!!error} id="answer" value={answer} disabled={loading} onChange={event => { setAnswer(event.target.value); setUnknown(false) }} placeholder={question.ranges?.length ? 'Значение' : 'Значение'} inputMode="decimal" />
            {question.unit ? <span className="unit-fixed" aria-label="Единица измерения">{question.unit}</span> : null}</div>}
          {question.hint && <p className="help compact range-hint">{question.hint}</p>}
          {question.kind === 'category' && <button type="button" className="secondary reject-types" disabled={loading} onClick={rejectTypes}>Ни один тип не подходит — изменить запрос</button>}
          {question.allow_unknown && question.kind !== 'category' && <label className="radio-row"><input type="radio" name="answer" checked={unknown} disabled={loading} onChange={() => { setUnknown(true); setError('') }} /><span>Не знаю</span></label>}

        </form>}
        {stage === 'correction' && <form id="correction-form" onSubmit={event => { event.preventDefault(); applyEdit() }}>
          <p className="help">Правка запускает полную проверку. Остальные сведения сохранятся.</p>
          <div className="edit-fields"><label>Характеристика<input value={editName} onChange={event => setEditName(event.target.value)} maxLength={256} /></label>
            <label>Значение<input value={editValue} onChange={event => setEditValue(event.target.value)} maxLength={512} /></label>
            <label>Единица<input value={editUnit} onChange={event => setEditUnit(event.target.value)} maxLength={64} /></label></div>
        </form>}
        {result && stage !== 'input' && stage !== 'correction' && <div className="result">
          {stage === 'review' && noSimilar && <p className="data-notice" role="status">Ничего похожего не найдено. Уточните название и назначение товара, затем повторите подбор.</p>}
          {stage === 'review' && question && !noSimilar && <p className="help">{userMessage(question.reason)}</p>}
          {result.status === 'needs_data' && <p className="data-notice" role="status">Сейчас сервису недостаточно данных для проверенного подбора. Описание и характеристики сохранены; причина и следующие шаги указаны ниже.</p>}
          {result.status === 'ambiguous' && <p className="data-notice" role="status">Осталось несколько подходящих позиций. Однозначный код пока не определён — дополните или исправьте характеристики.</p>}
          {result.next_steps.map(step => <p className="help compact" key={userMessage(step)}>{userMessage(step)}</p>)}
          {result.facts.length > 0 && <><h3 className="section-label">Характеристики</h3><dl className="result-data">{result.facts.map(fact => <div key={fact.id}><dt>{fact.name || 'Неустановленное свойство'}</dt><dd>{`${fact.surface_value || fact.value} ${fact.unit || ''}`.trim() || 'Не знаю'}{fact.unknown && <span className="help compact"> · значение не подтверждено{fact.uncertainty_reason ? `: ${fact.uncertainty_reason}` : ''}</span>} {fact.normalized_value !== null && (fact.normalized_value !== fact.value || fact.normalized_unit !== fact.unit) && <small> → {fact.normalized_value} {fact.normalized_unit}</small>} <button type="button" className="link-button" onClick={() => edit(fact)}>Исправить</button></dd></div>)}</dl></>}
          {result.corrections.map((item, index) => <p className="unit-note" key={index}>{item.original} → {item.normalized}. {item.explanation}{item.confirmation_required && ' Требуется подтверждение — исходный факт пока не изменён.'}</p>)}
          {result.candidates.length > 0 && <details className="candidates" open={result.candidate_count <= 3}>
            <summary className="section-label">Конечные позиции: {result.candidate_count}{result.candidate_count > 3 && ' — нажмите, чтобы развернуть'}</summary>
            {result.candidates.map(candidate => <div className="candidate" key={candidate.ktru_code}><strong>{candidate.name}</strong><div className="result-code">{candidate.ktru_code}</div>
              <p className="help compact">{candidate.outcome === 'compatible' ? 'Значения совместимы' : candidate.outcome === 'incompatible' ? 'Значения несовместимы' : 'Проверка не завершена'}{candidate.missing_required.length > 0 && ` · Требуется: ${candidate.missing_required.join(', ')}`}</p>
              <button type="button" className="link-button" disabled={loading} onClick={() => void openCard(candidate.ktru_code)}>Показать карточку КТРУ</button></div>)}
          </details>}
          {result.unparsed.length > 0 && <><h3 className="section-label">Неразобранный текст</h3>
            <p className="help compact">Эти фрагменты сохранены как есть: система не назначила им характеристику.</p>
            <ul className="additional-list">{result.unparsed.map((fragment, index) => <li key={index}>{fragment.raw}</li>)}</ul></>}
          {result.additional.length > 0 && <><h3 className="section-label">Дополнительные сведения</h3>
            <ul className="additional-list">{result.additional.map((fragment, index) => <li key={index}>{fragment.raw}</li>)}</ul></>}
          {card && <div className="catalog-card"><h3 className="section-label">Карточка КТРУ {card.data.position.code}</h3>
            <p className="help compact">{card.data.position.name} · справочные правила (не сведения пользователя)</p>
            <dl className="result-data">{card.data.position.properties.map(property => <div key={property.id}>
              <dt>{property.name}{property.required && ' · обязательно'}{!property.verified && ' · не подтверждено'}</dt>
              <dd>{property.allowed.length === 0 ? 'значения не заданы' : property.allowed.map(rule).join('; ')}</dd></div>)}</dl>
            {card.data.blocking_reasons.length > 0 && <ul className="additional-list">{card.data.blocking_reasons.map((reason, index) => <li key={index}>{reason.explanation} ({reason.applicability}{reason.blocks_resolution ? ', блокирует подтверждение' : ''})</li>)}</ul>}
            <p className="help compact">Ваши сведения по этой позиции: {result.facts.filter(fact => fact.card_mappings?.length).map(fact => `${fact.name}: ${fact.unknown ? 'не подтверждено' : fact.value}`).join('; ') || 'нет'}</p>
            <button type="button" className="link-button" onClick={() => setCard(null)}>Закрыть карточку</button></div>}
          {cardError && <p className="form-error" role="alert">{cardError}</p>}
          {result.issues.map((issue, index) => <p className={issue.severity === 'error' ? 'form-error' : 'help compact'} key={index}>{userMessage(issue.explanation)} {userMessage(issue.suggested_action)}</p>)}
          {stage === 'done' && <div className="result-name"><h3 className="result-code">{result.selected_ktru_code}</h3><span className="confirmed">Подтверждено и проверено</span></div>}
          <div className="known-data"><span>Проверено позиций: {result.scope.evaluated_count}</span><span>{result.scope.complete ? 'Область проверена полностью' : 'Покрытие не подтверждено'}</span></div>
        </div>}
        {loading && <p className="help" role="status">Проверяем описание и характеристики… Предыдущий результат больше не действует.</p>}
        {error && <p className="form-error" id="form-error" role="alert">{error}</p>}
      </div>
      <div className="action-bar">
        {stage === 'input' ? <span className="action-hint">Enter — подобрать · Shift+Enter — новая строка</span> : <button className="link-button back-button" onClick={back}>Назад</button>}
        <div className="action-buttons">
          {draft.confirmed_category_id && stage !== 'input' && stage !== 'correction' && <button className="secondary" disabled={loading} onClick={() => void submit({ ...draft, confirmed_category_id: null, confirmed_ktru_code: null, answers: draft.answers.filter(item => item.question_id !== 'category') })}>Изменить категорию</button>}
          {stage === 'input' && <button className="primary" type="submit" form="description-form" disabled={loading}>Подобрать КТРУ</button>}
          {error && stage === 'question' && <button className="primary" disabled={loading} onClick={() => void submit(draft)}>Повторить проверку</button>}
          {stage === 'question' && question && <button className="primary" type="submit" form="question-form" disabled={loading}>Продолжить</button>}
          {stage === 'correction' && <button className="primary" type="submit" form="correction-form" disabled={loading}>Проверить правку</button>}
          {stage === 'review' && <><button className="secondary" onClick={changeDescription}>Изменить описание</button><button className="secondary" onClick={() => edit(null)}>Добавить характеристику</button>
            {question?.kind === 'property' && <button className="primary" disabled={loading} onClick={() => { setAnswer(''); setAnswerSet([]); setUnknown(false); setAnswerUnit(question.unit || ''); setStage('question') }}>Уточнить характеристики</button>}
            {question?.kind === 'correction' && <button className="primary" disabled={loading} onClick={() => { setAnswer(''); setAnswerSet([]); setUnknown(false); setStage('question') }}>Открыть уточнение: {question.title}</button>}
            {canConfirm && <button className="primary" disabled={loading} onClick={() => void submit({ ...draft, confirmed_ktru_code: question!.options.find(option => option.value === result!.candidates[0].ktru_code)!.value })}>Подтвердить код</button>}
            {(result?.status === 'needs_data' || !!error) && <button className="primary" disabled={loading} onClick={() => void submit(draft)}>Повторить проверку</button>}</>}
          {stage === 'done' && <><button className="secondary" onClick={changeDescription}>Изменить описание</button><button className="secondary" onClick={reset}>Новый подбор</button><button className="primary" onClick={() => void copy()}>{copied ? 'Скопировано' : 'Скопировать код'}</button></>}
        </div>
      </div>
    </section>
    </div>
    <footer className="page-footer"><span>Подбор по справочнику КТРУ</span><span>Итоговый код подтверждаете вы</span></footer>
  </main></>
}
export default App
