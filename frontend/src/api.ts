export type InputFact = { id: string; name: string; value?: string; values?: string[] | null; unit?: string | null; unknown?: boolean; subject?: 'product' | 'packaging' | 'unspecified'; replaces?: string | null }
export type Answer = { question_id: string; value?: string | null; values?: string[] | null; unit?: string | null; unknown?: boolean }
export type Draft = { description: string; attributes: InputFact[]; additional: string[]; confirmed_category_id: string | null; answers: Answer[]; confirmed_ktru_code: string | null }
export type FactAssignment = { name: string; value: string; unit: string | null; subject: string; evidence: string }
export type Fact = { id: string; name: string; value: string; values?: string[] | null; unit: string | null; raw: string; span?: [number, number] | null; source: 'text' | 'input' | 'answer'; subject: string; unknown: boolean; negated?: boolean; normalized_value: string | null; normalized_unit: string | null; surface_value?: string | null; extraction_evidence?: string[]; card_mappings?: string[]; assignment_options?: FactAssignment[]; uncertainty_reason?: string | null; negation_interpretation?: 'canonical_absence' | null }
export type Issue = { code: string; severity: 'info' | 'warning' | 'error'; explanation: string; suggested_action: string; fact_id: string | null; property_id: string | null; candidate_code: string | null; rule_source: string | null; confirmation_required: boolean }
export type BlockingReason = { code: string; explanation: string; property_id: string | null; property_name: string | null; kind: string | null; units: (string | null)[]; unsupported_reasons: string[]; source_issues: string[]; resolve_blocked_reason: string[]; rule_source: string | null; applicability: 'source' | 'required' | 'provided' | 'potential'; blocks_resolution: boolean }
// Excluded candidates retain proven contradictions; their diagnostics do not block remaining candidates.
export type Candidate = { ktru_code: string; name: string; outcome: 'compatible' | 'incompatible' | 'unknown'; missing_required: string[]; checks: { name: string; outcome: string; reason: string; elements: { value: string; outcome: 'compatible' | 'incompatible' | 'unknown'; reason: string; value_codes: string[] }[] }[]; blocking_reasons: BlockingReason[]; exclusion_reasons: Issue[] }
export type Question = { id: string; kind: 'category' | 'property' | 'conflict' | 'code' | 'correction'; title: string; reason: string; property_name: string | null; options: { value: string; label: string }[]; unit: string | null; allow_unknown: boolean; multiple: boolean; ranges?: { lower: number | null; upper: number | null; unit: string | null }[]; hint?: string | null }
export type Result = {
  status: 'needs_data' | 'needs_input' | 'ambiguous' | 'resolved'; original_text: string; normalized_text: string;
  facts: Fact[]; original_facts?: Fact[]; unparsed: { id: string | null; raw: string; span?: [number, number] | null }[]; additional: { id: string | null; raw: string; span?: [number, number] | null }[];
  categories: { id: string; name: string; rank: number }[]; candidates: Candidate[]; excluded: Candidate[];
  candidate_count: number; excluded_count: number; scope: { category_id: string | null; complete: boolean; evaluated_count: number };
  issues: { code: string; severity: string; explanation: string; suggested_action: string }[];
  corrections: { fact_id: string; original: string; normalized: string; explanation: string; confirmation_required: boolean }[];
  next_question: Question | null; next_steps: string[];
  verification: { type_verified: boolean; ktru_unique: boolean; fields_complete: boolean; value_verified: boolean; catalog_scope_complete: boolean; blocking_issues: boolean; value_indistinguishable: boolean; confirmed_by_user: boolean };
  selected_ktru_code: string | null; catalog_version: string; engine_version: string;
  card_schema_version: string | null; card_schema_evidence: string[];
}

export const emptyDraft = (): Draft => ({ description: '', attributes: [], additional: [], confirmed_category_id: null, answers: [], confirmed_ktru_code: null })

export async function analyze(draft: Draft, signal: AbortSignal): Promise<Result> {
  let response: Response
  try { response = await fetch('/api/v1/analyses', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(draft), signal }) }
  catch (error) {
    if (signal.aborted) throw error
    throw new Error('Не удалось связаться с сервисом. Проверьте подключение и повторите проверку.')
  }
  const body = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = body?.detail
    throw new Error(Array.isArray(detail) ? 'Проверьте заполнение полей и длину описания.' : (detail?.message ? userMessage(detail.message) : '') || 'Сервис временно недоступен. Повторите запрос.')
  }
  if (!body || !['needs_data', 'needs_input', 'ambiguous', 'resolved'].includes(body.status)) throw new Error('Сервис вернул неполный ответ. Повторите проверку.')
  return body as Result
}

export function userMessage(text: string): string {
  const labels: Record<string, string> = {
    invalid_value: 'значение не соответствует справочнику',
    resources_invalid: 'справочник временно недоступен',
    unit_missing: 'укажите единицу измерения',
    unit_unknown: 'единица измерения не распознана',
    conflicting_facts: 'характеристики противоречат друг другу',
    mapping_ambiguous: 'уточните, к какой характеристике относится значение',
    search_index_missing: 'поиск по справочнику временно недоступен',
    missing_required: 'заполните обязательную характеристику',
  }
  return text.replace(/\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b/g, code => labels[code] || 'требуется дополнительная проверка')
}
