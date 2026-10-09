import { useEffect, useRef, useState } from 'react'
import './App.css'

type Stage = 'input' | 'question' | 'review' | 'correction' | 'done'
type Field = 'product' | 'material' | 'volume' | 'purpose'
type Product = { product: boolean; material: string; volume: string; purpose: string; converted: string }
type Snapshot = { stage: Stage; field: Field; data: Product; answer: string }
const initial: Product = { product: false, material: '', volume: '', purpose: '', converted: '' }
const examples = [
  ['Полное описание', 'Бумажный стакан для горячего кофе, объём 0,2 л.'],
  ['Без характеристик', 'Стакан для напитков.'],
  ['Неопределённый товар', 'Нужно что-то для мероприятия.'],
]
const validVolume = (value: string) => /^\d+(?:[.,]\d+)?$/.test(value) && Number(value.replace(',', '.')) > 0
function extract(text: string): Product {
  const lower = text.toLowerCase()
  const ml = lower.match(/(\d+(?:[.,]\d+)?)\s*мл(?=\s|[.,;]|$)/u)
  const liters = lower.match(/(\d+(?:[.,]\d+)?)\s*(?:литр[а-я]*|л)(?=\s|[.,;]|$)/u)
  const volume = ml?.[1].replace(',', '.') || (liters ? String(Number(liters[1].replace(',', '.')) * 1000) : '')
  const paper = /бумаж|бумаг/u.test(lower), plastic = /пластик/u.test(lower)
  const hot = /горяч|кофе/u.test(lower), cold = /холод/u.test(lower)
  return {
    product: /стакан/u.test(lower),
    material: paper !== plastic ? (paper ? 'Бумага' : 'Пластик') : '',
    volume: validVolume(volume) ? volume : '',
    purpose: hot !== cold ? (hot ? 'Горячие напитки' : 'Холодные напитки') : '',
    converted: !ml && liters ? liters[1] + ' л → ' + volume + ' мл' : '',
  }
}
function missing(product: Product): Field | undefined {
  if (!product.product) return 'product'
  if (!product.material) return 'material'
  if (!product.volume) return 'volume'
  if (!product.purpose) return 'purpose'
}
const questions: Record<Field, { title: string; help: string }> = {
  product: { title: 'Укажите товар', help: 'Товар не определён. Выберите тип или измените описание.' },
  material: { title: 'Укажите материал', help: 'В описании не указан однозначный материал стакана.' },
  volume: { title: 'Укажите объём', help: 'Введите объём стакана в миллилитрах.' },
  purpose: { title: 'Укажите назначение', help: 'Выберите, для каких напитков предназначен стакан.' },
}
function App() {
  const [stage, setStage] = useState<Stage>('input')
  const [description, setDescription] = useState('')
  const [data, setData] = useState<Product>(initial)
  const [field, setField] = useState<Field>('product')
  const [answer, setAnswer] = useState('')
  const [correction, setCorrection] = useState<Field>('material')
  const [history, setHistory] = useState<Snapshot[]>([])
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)
  const [editing, setEditing] = useState(false)
  const titleRef = useRef<HTMLHeadingElement>(null)
  // Move keyboard focus without scrolling the viewport when a step changes.
  useEffect(() => { titleRef.current?.focus({ preventScroll: true }) }, [stage, field])
  function go(next: Stage, nextData = data, nextField = field, nextAnswer = '') {
    setHistory(items => [...items, { stage, data, field, answer }])
    setStage(next); setData(nextData); setField(nextField); setAnswer(nextAnswer)
    setError(''); setCopied(false)
  }
  function advance(nextData: Product) {
    const nextField = missing(nextData)
    go(nextField ? 'question' : 'review', nextData, nextField || field)
  }
  function analyze() {
    if (!description.trim()) { setError('Введите описание товара.'); return }
    setEditing(false); setError('')
    const nextData = extract(description), nextField = missing(nextData)
    setData(nextData); setField(nextField || 'product'); setAnswer('')
    setStage(nextField ? 'question' : 'review'); setCopied(false)
    setHistory([{ stage: 'input', data: initial, field: 'product', answer: '' }])
  }
  function back() {
    const previous = history.at(-1)
    if (!previous) return
    setStage(previous.stage); setField(previous.field); setData(previous.data); setAnswer(previous.answer)
    setHistory(items => items.slice(0, -1)); setError(''); setCopied(false); setEditing(false)
  }
  function submitAnswer() {
    if (!answer) { setError('Выберите ответ.'); return }
    if (field === 'volume' && !validVolume(answer)) { setError('Введите положительное число, например 200.'); return }
    const nextData = field === 'product' ? { ...data, product: true }
      : { ...data, [field]: field === 'volume' ? answer.replace(',', '.') : answer, ...(field === 'volume' ? { converted: '' } : {}) }
    setEditing(false); advance(nextData)
  }
  function reset() {
    setStage('input'); setDescription(''); setData(initial); setAnswer(''); setHistory([])
    setError(''); setCopied(false); setEditing(false)
  }
  function startCorrection() { setCorrection('material'); go('correction') }
  function applyCorrection() {
    if (correction === 'product') { go('input'); return }
    setEditing(true); go('question', data, correction, data[correction])
  }
  const code = 'DEMO-CUP-' + (data.material === 'Бумага' ? 'PAPER' : 'PLASTIC') + '-' + (data.purpose === 'Горячие напитки' ? 'HOT' : 'COLD')
  const title = stage === 'input' ? 'Описание товара' : stage === 'question' ? questions[field].title
    : stage === 'correction' ? 'Что нужно исправить?' : stage === 'done' ? 'Подбор завершён' : 'Проверьте результат'
  const currentStep = stage === 'input' ? 0 : stage === 'question' || stage === 'correction' ? 1 : 2
  async function copy() {
    try { await navigator.clipboard.writeText(code); setCopied(true); setError('') }
    catch { setError('Не удалось скопировать код. Выделите его и скопируйте вручную.') }
  }
  return <main className="catalog-page">
    <div className="page-context">Каталог товаров <span aria-hidden="true">/</span> Новая позиция</div>
    <div className="page-heading"><h1>Подбор КТРУ</h1><span className="preview-label">Демо</span></div>
    <section className="picker" aria-label="Подбор кода КТРУ">
      <ol className="progress" aria-label="Этапы подбора">
        {['Описание', 'Уточнение', 'Результат'].map((label, index) =>
          <li key={label} className={index === currentStep ? 'current' : index < currentStep ? 'complete' : ''} aria-current={index === currentStep ? 'step' : undefined}><span>{index + 1}.</span> {label}</li>)}
      </ol>
      <div className="step-content">
        <h2 ref={titleRef} tabIndex={-1} className="step-title">{title}</h2>
        {stage === 'input' && <form id="description-form" onSubmit={event => { event.preventDefault(); analyze() }}>
          <p className="help">Укажите название и известные характеристики: материал, размер, назначение.</p>
          <label className="sr-only" htmlFor="description">Описание товара</label>
          <textarea id="description" value={description} onChange={event => { setDescription(event.target.value); setError('') }} placeholder="Например: бумажный стакан для горячих напитков, 200 мл" aria-invalid={!!error} aria-describedby={error ? 'form-error' : undefined}/>
          <div className="example-links"><span>Примеры:</span>{examples.map(([label, text]) => <button type="button" key={label} className="link-button" onClick={() => { setDescription(text); setError('') }}>{label}</button>)}</div>
        </form>}
        {stage === 'question' && <form id="question-form" onSubmit={event => { event.preventDefault(); submitAnswer() }}>
          <p className="help">{editing ? 'Остальные характеристики останутся без изменений.' : questions[field].help}</p>
          {field === 'product' ? <div className="product-choice">
            <label className="radio-row"><input type="radio" name="product" value="Стакан" checked={answer === 'Стакан'} onChange={() => { setAnswer('Стакан'); setError('') }}/><span>Стакан</span></label>
            <p className="help compact">В демоверсии доступен только подбор стаканов.</p>
            <button type="button" className="link-button" onClick={() => go('input')}>Изменить описание товара</button>
          </div> : field === 'volume' ? <div className="volume-field">
            <label htmlFor="volume">Объём</label><div className="measurement"><input id="volume" value={answer} onChange={event => { setAnswer(event.target.value); setError('') }} inputMode="decimal" placeholder="200" aria-invalid={!!error} aria-describedby={error ? 'form-error' : undefined}/><span>мл</span></div>
          </div> : <fieldset className="radio-list"><legend className="sr-only">{questions[field].title}</legend>{(field === 'material' ? ['Бумага', 'Пластик'] : ['Горячие напитки', 'Холодные напитки']).map(value =>
            <label className="radio-row" key={value}><input type="radio" name={field} value={value} checked={answer === value} onChange={() => { setAnswer(value); setError('') }}/><span>{value}</span></label>)}</fieldset>}
          {field !== 'product' && <div className="known-data"><span>Товар: стакан</span>{field !== 'material' && data.material && <span>Материал: {data.material.toLowerCase()}</span>}{field !== 'volume' && data.volume && <span>Объём: {data.volume} мл</span>}{field !== 'purpose' && data.purpose && <span>{data.purpose}</span>}</div>}
        </form>}
        {stage === 'correction' && <form id="correction-form" onSubmit={event => { event.preventDefault(); applyCorrection() }}>
          <p className="help">Выберите характеристику или вернитесь к описанию товара.</p>
          <fieldset className="radio-list"><legend className="sr-only">Что исправить</legend>{([
            ['material', 'Материал', data.material], ['volume', 'Объём', data.volume + ' мл'],
            ['purpose', 'Назначение', data.purpose], ['product', 'Другой товар', 'Изменить описание'],
          ] as [Field, string, string][]).map(([key, label, value]) => <label className="radio-row correction-row" key={key}><input type="radio" name="correction" value={key} checked={correction === key} onChange={() => setCorrection(key)}/><span>{label}</span><small>{value}</small></label>)}</fieldset>
        </form>}
        {(stage === 'review' || stage === 'done') && <div className="result">
          <p className="help">{stage === 'review' ? 'Подходит ли эта позиция вашему товару?' : 'Вы подтвердили позицию и характеристики.'}</p>
          <div className="result-name"><h3>Стакан {data.material === 'Бумага' ? 'бумажный' : 'пластиковый'}</h3>{stage === 'done' && <span className="confirmed">Подтверждено</span>}</div>
          <dl className="result-data">
            <div><dt>Код КТРУ <span className="demo-code">(демо)</span></dt><dd className="result-code">{code}</dd></div>
            <div><dt>Материал</dt><dd>{data.material}</dd></div><div><dt>Объём</dt><dd>{data.volume} мл</dd></div><div><dt>Назначение</dt><dd>{data.purpose}</dd></div>
          </dl>
          {data.converted && <p className="unit-note">Пересчёт единиц: {data.converted}</p>}
        </div>}
        {error && <p className="form-error" id="form-error" role="alert">{error}</p>}
      </div>
      <div className="action-bar">
        {stage === 'input' ? <span className="action-hint">Описание сохранится на следующих шагах</span> : <button className="link-button back-button" onClick={back}>Назад</button>}
        <div className="action-buttons">
          {stage === 'input' && <button className="primary" type="submit" form="description-form">Подобрать КТРУ</button>}
          {stage === 'question' && <button className="primary" type="submit" form="question-form">Продолжить</button>}
          {stage === 'correction' && <button className="primary" type="submit" form="correction-form">Изменить</button>}
          {stage === 'review' && <><button className="secondary" onClick={startCorrection}>Не подходит</button><button className="primary" onClick={() => go('done')}>Подтвердить</button></>}
          {stage === 'done' && <><button className="secondary" onClick={reset}>Новый подбор</button><button className="primary" onClick={copy}>{copied ? 'Скопировано' : 'Скопировать код'}</button></>}
        </div>
      </div>
    </section>
    <div className="preview-note">Демоверсия интерфейса. Условные позиции, без подключения к API.</div>
  </main>
}
export default App
