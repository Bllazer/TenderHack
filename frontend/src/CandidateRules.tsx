import { useEffect, useState } from 'react'
import type { Fact } from './api'
import { userMessage } from './api'
type Allowed = { kind: string; value: string | null; lower: string | null; upper: string | null; lower_inclusive: boolean; upper_inclusive: boolean; unit: string | null }
type Card = { position: { name: string; code: string; properties: { id: string; name: string; required: boolean | null; verified: boolean; subject: string; allowed: Allowed[] }[] }; blocking_reasons: { explanation: string; blocks_resolution: boolean }[] }
export default function CandidateRules({ code, facts }: { code: string; facts: Fact[] }) {
  const [open, setOpen] = useState(false)
  const [card, setCard] = useState<Card | null>(null)
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  useEffect(() => {
    if (!open) return
    const abort = new AbortController()
    fetch(`/api/v1/catalog/positions/${encodeURIComponent(code)}`, { signal: abort.signal }).then(async response => {
      if (!response.ok) throw new Error('Не удалось загрузить правила КТРУ.')
      return response.json() as Promise<Card>
    }).then(data => { if (!abort.signal.aborted) setCard(data) }).catch(failure => { if (!abort.signal.aborted) setError(failure instanceof Error ? failure.message : 'Не удалось загрузить карточку.') })
    return () => abort.abort()
  }, [open, code, retry])
  return <div className="candidate-rules"><button type="button" className="link-button" aria-expanded={open} onClick={() => { setCard(null); setError(''); setOpen(value => !value) }}>{open ? 'Скрыть правила КТРУ' : 'Правила КТРУ'}</button>{open && <div className="rules-panel">
    {error ? <p role="alert">{error} <button className="link-button" onClick={() => { setError(''); setRetry(value => value + 1) }}>Повторить</button></p> : !card ? <p role="status">Загружаем карточку…</p> : <>
      <h4>{card.position.name}</h4><p>{card.position.code}</p><h4>Требования каталога</h4>
      {card.position.properties.length === 0 && <p>В карточке нет характеристик.</p>}
      <dl>{card.position.properties.map(prop => <div key={prop.id}><dt>{prop.name}{prop.required && ' · обязательная'}{prop.subject === 'packaging' && ' · упаковка'}</dt><dd>{prop.allowed.length ? prop.allowed.map((value, i) => <div key={i}>{allowed(value)}</div>) : 'Допустимые значения не указаны'}{!prop.verified && <small>Правило не подтверждено справочником.</small>}</dd></div>)}</dl>
      {card.blocking_reasons.length > 0 && <><h4>Ограничения проверки</h4>{card.blocking_reasons.map((reason, i) => <p key={i}>{userMessage(reason.explanation)}{reason.blocks_resolution && <small>До устранения этого ограничения код нельзя подтвердить.</small>}</p>)}</>}
      <h4>Ваши характеристики</h4>{facts.length ? <dl>{facts.map(fact => <div key={fact.id}><dt>{fact.name || 'Неустановленная характеристика'}</dt><dd>{fact.unknown ? fact.raw || 'Значение неизвестно' : `${fact.values?.join('; ') || fact.value} ${fact.unit || ''}`}</dd></div>)}</dl> : <p>Характеристики пока не введены.</p>}
    </>}
  </div>}</div>
}
function allowed(value: Allowed) {
  const unit = value.unit ? ` ${value.unit}` : ''
  if (value.kind !== 'interval') return `${value.value || value.lower || 'Не указано'}${unit}`
  return [value.lower !== null ? `${value.lower_inclusive ? '≥' : '>'} ${value.lower}${unit}` : '', value.upper !== null ? `${value.upper_inclusive ? '≤' : '<'} ${value.upper}${unit}` : ''].filter(Boolean).join(' и ')
}
