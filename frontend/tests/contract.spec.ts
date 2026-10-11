import { expect, test } from '@playwright/test'

test('multiple answers survive refresh and editing sends all values without scalar value', async ({ page }) => {
  let latest: any
  let baseline: any
  await page.route('**/api/v1/catalog/categories/tools/properties', route => route.fulfill({ json: { properties: [{ name: 'Материал', aliases: [], subject: 'product', question_id: 'materials', options: ['Сталь', 'Пластик'], units: [], multiple: true }] } }))
  await page.route('**/api/v1/analyses', async route => {
    latest = route.request().postDataJSON()
    if (!latest.confirmed_category_id) { const response = await route.fetch(); baseline = await response.json(); return route.fulfill({ response }) }
    const data = baseline
    const supplied = latest.attributes.at(-1) || latest.answers.find((a: any) => a.question_id === 'materials')
    await route.fulfill({ json: { ...data, candidate_count: supplied ? 1 : 2, status: 'needs_input', selected_ktru_code: null,
      facts: supplied ? [{ id: 'material', name: 'Материал', value: '', values: supplied.values, unit: null, raw: 'Сталь и Пластик', source: 'answer', subject: 'product', unknown: false, normalized_value: null, normalized_unit: null }] : [],
      next_question: supplied ? null : { id: 'materials', kind: 'property', title: 'Выберите материалы', reason: 'Допускается набор', options: ['Сталь', 'Пластик'].map(value => ({ value, label: value })), unit: null, multiple: true, allow_unknown: true } } })
  })
  await page.goto('/')
  await page.getByLabel('Описание товара', { exact: true }).fill('Дрель')
  await page.getByRole('button', { name: 'Подобрать КТРУ' }).click()
  await page.getByRole('radio', { name: 'Дрели электрические' }).check()
  await page.getByRole('button', { name: 'Продолжить' }).click()
  await page.getByRole('checkbox', { name: 'Сталь', exact: true }).check()
  await page.getByRole('checkbox', { name: 'Пластик', exact: true }).check()
  await page.getByRole('checkbox', { name: 'Не знаю', exact: true }).check()
  await expect(page.getByRole('checkbox', { name: 'Сталь', exact: true })).not.toBeChecked()
  await page.getByRole('checkbox', { name: 'Не знаю', exact: true }).uncheck()
  await expect(page.getByRole('checkbox', { name: 'Сталь', exact: true })).toBeChecked()
  await page.reload()
  await expect(page.getByRole('checkbox', { name: 'Сталь', exact: true })).toBeChecked()
  await expect(page.getByRole('checkbox', { name: 'Пластик', exact: true })).toBeChecked()
  await page.getByRole('button', { name: 'Продолжить' }).click()
  await expect(page.locator('.result-data')).toContainText('Сталь; Пластик')
  expect(latest.answers.at(-1).values).toEqual(['Сталь', 'Пластик'])
  expect(latest.answers.at(-1)).not.toHaveProperty('value')
  await page.getByRole('button', { name: 'Исправить', exact: true }).click()
  await expect(page.getByRole('checkbox', { name: 'Сталь', exact: true })).toBeChecked()
  await expect(page.getByRole('checkbox', { name: 'Пластик', exact: true })).toBeChecked()
  await page.getByRole('button', { name: 'Продолжить' }).click()
  await expect(page.locator('.result-data')).toContainText('Сталь; Пластик')
  expect(latest.attributes.at(-1).values).toEqual(['Сталь', 'Пластик'])
  expect(latest.attributes.at(-1)).not.toHaveProperty('value')
})

test('one candidate exposes correction action and preserves opaque choice', async ({ page }) => {
  let latest: any
  await page.route('**/api/v1/analyses', async route => {
    latest = route.request().postDataJSON()
    const response = await route.fetch(); const data = await response.json()
    await route.fulfill({ json: { ...data, candidate_count: 1, status: 'needs_input', selected_ktru_code: null,
      next_question: { id: 'opaque-question', kind: 'correction', title: 'Уточните исправление', reason: 'Нужно ваше решение', options: [{ value: 'opaque:accept/42', label: 'Принять исправление' }, { value: 'opaque:reject/42', label: 'Оставить исходное' }], unit: null, multiple: true, allow_unknown: false } } })
  })
  await page.goto('/')
  await page.getByLabel('Описание товара', { exact: true }).fill('Дрель')
  await page.getByRole('button', { name: 'Подобрать КТРУ' }).click()
  await page.getByRole('button', { name: 'Проверить предложенное исправление' }).click()
  await page.getByRole('radio', { name: 'Оставить исходное' }).check()
  await page.getByRole('button', { name: 'Продолжить' }).click()
  await expect(page.getByRole('button', { name: 'Проверить предложенное исправление' })).toBeVisible()
  expect(latest.answers.at(-1)).toMatchObject({ question_id: 'opaque-question', value: 'opaque:reject/42' })
  expect(latest.answers.at(-1)).not.toHaveProperty('values')
})

test('raw uncertain facts and extra text remain visible; catalog rules are separate', async ({ page }) => {
  await page.route('**/api/v1/analyses', async route => {
    const response = await route.fetch(); const data = await response.json()
    await route.fulfill({ json: { ...data, candidate_count: 1, next_question: null,
      candidates: [{ ktru_code: 'CARD', name: 'Дрель', outcome: 'unknown', missing_required: [] }],
      facts: [{ id: 'uncertain', name: 'Мощность', value: '', raw: 'примерно шестьсот', unknown: true, source: 'text', unit: null, normalized_value: null, uncertainty_reason: 'Точное значение не определено' }],
      unparsed: [{ raw: 'для ремонта' }], additional: [{ raw: 'красная' }] } })
  })
  await page.route('**/api/v1/catalog/positions/CARD', route => route.fulfill({ json: { position: { code: 'CARD', name: 'Дрель из каталога', properties: [{ id: 'power', name: 'Мощность', required: true, verified: true, subject: 'product', allowed: [{ kind: 'interval', lower: '500', upper: '900', lower_inclusive: true, upper_inclusive: false, unit: 'Вт' }] }] }, blocking_reasons: [{ explanation: 'Заполните мощность', blocks_resolution: true }] } }))
  await page.goto('/')
  await page.getByLabel('Описание товара', { exact: true }).fill('Дрель')
  await page.getByRole('button', { name: 'Подобрать КТРУ' }).click()
  await expect(page.locator('.result-data')).toContainText('примерно шестьсот')
  await expect(page.locator('.result-data')).toContainText('Точное значение не определено')
  await page.getByText('Что не удалось распознать (2)').click()
  await expect(page.getByText('для ремонта', { exact: true })).toBeVisible()
  await expect(page.getByText('красная', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Правила КТРУ', exact: true }).click()
  await expect(page.locator('.rules-panel')).toContainText('≥ 500 Вт и < 900 Вт')
  await expect(page.getByRole('heading', { name: 'Требования каталога' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Ваши характеристики' })).toBeVisible()
  await expect(page.locator('.rules-panel')).toContainText('Заполните мощность')
})
