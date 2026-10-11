import { expect, test } from '@playwright/test'

const description = 'Дрель; Питание: от сети; Мощность:650 Вт; Мошность:900 Вт'
async function openCorrection(page: import('@playwright/test').Page) {
  await page.goto('/')
  await page.evaluate(() => sessionStorage.clear())
  await page.reload()
  await page.getByLabel('Описание товара', { exact: true }).fill(description)
  await page.getByRole('button', { name: 'Подобрать КТРУ' }).click()
  await page.getByRole('radio', { name: 'Дрели электрические' }).check()
  await page.getByRole('button', { name: 'Продолжить' }).click()
  await page.getByRole('button', { name: 'Перейти: Уточнение' }).click()
  await expect(page.getByRole('heading', { name: 'Подбор завершён' })).toHaveCount(0)
}

test('real test API: mapped typo retains invalid value; explicit edit reanalyzes', async ({ page }) => {
  await openCorrection(page)
  await expect(page.locator('.result-data')).toContainText('900')
  await expect(page.getByRole('heading', { name: 'Подбор завершён' })).toHaveCount(0)
  await page.locator('.result-data > div').filter({ hasText: '900' }).getByRole('button', { name: 'Исправить' }).click()
  await page.getByLabel('Значение', { exact: true }).fill('650')
  const edited = page.waitForRequest(r => r.url().endsWith('/api/v1/analyses'))
  await page.getByRole('button', { name: 'Продолжить' }).click()
  const updated = (await edited).postDataJSON()
  expect(updated.attributes.at(-1)).toMatchObject({ value: '650', unit: 'Вт' })
  expect(updated.confirmed_ktru_code).toBeNull()
  await expect(page.getByRole('button', { name: 'Подтвердить код' })).toBeVisible()
})

test('real test API: unknown answer stays pending and is not asked twice', async ({ page }) => {
  await openCorrection(page)
  await page.getByRole('radio', { name: 'Не знаю', exact: true }).check()
  const unknown = page.waitForRequest(r => r.url().endsWith('/api/v1/analyses'))
  await page.getByRole('button', { name: 'Продолжить' }).click()
  expect((await unknown).postDataJSON().answers.at(-1)).toMatchObject({ unknown: true, value: null })
  await expect(page.getByRole('heading', { name: 'Подбор завершён' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Подтвердить код' })).toHaveCount(0)
  await expect(page.locator('#question-form')).toHaveCount(0)
})

test('mock UI: renders every server option and sends exact opaque value for current question ID', async ({ page }) => {
  const values = ['accept:Серверное имя / 1', 'accept:Другое имя', 'reject']
  await page.route('**/api/v1/analyses', async route => {
    const response = await route.fetch()
    const result = await response.json()
    await route.fulfill({ json: { ...result, categories: [{ id: 'tools', name: 'Дрели', rank: 1 }], candidate_count: 2, status: 'needs_input', selected_ktru_code: null,
      next_question: { id: 'mapping:server-specific', kind: 'correction', title: 'Серверное исправление', reason: 'Серверное обоснование',
        options: values.map((value, i) => ({ value, label: `Вариант ${i + 1}` })), unit: null, allow_unknown: true } } })
  })
  await page.goto('/')
  await page.getByLabel('Описание товара', { exact: true }).fill('Товар')
  await page.getByRole('button', { name: 'Подобрать КТРУ' }).click()
  await expect(page.getByRole('heading', { name: 'Серверное исправление' })).toBeVisible()
  await expect(page.getByText('Серверное обоснование')).toBeVisible()
  for (let i = 1; i <= 3; i++) await expect(page.getByRole('radio', { name: `Вариант ${i}` })).toBeVisible()
  await page.getByRole('radio', { name: 'Вариант 2' }).check()
  const request = page.waitForRequest(r => r.url().endsWith('/api/v1/analyses'))
  const response = page.waitForResponse(r => r.url().endsWith('/api/v1/analyses'))
  await page.getByRole('button', { name: 'Продолжить' }).dblclick()
  await response
  const body = (await request).postDataJSON()
  expect(body.answers).toEqual([{ question_id: 'mapping:server-specific', value: values[1], unit: null, unknown: false }])
  await expect(page.getByRole('heading', { name: 'Подбор завершён' })).toHaveCount(0)
})

test('real test API: code confirmation cannot bypass invalid corrected value', async ({ request }) => {
  const body = { description, attributes: [], additional: [], answers: [], confirmed_category_id: 'tools', confirmed_ktru_code: 'TEST-DRILL-MAINS' }
  const first = await request.post('http://127.0.0.1:18001/api/v1/analyses', { data: body })
  const result = await first.json()
  expect(result.status).toBe('needs_input')
  expect(result.selected_ktru_code).toBeNull()
  expect(result.verification.value_verified).toBe(false)
  expect(result.next_question.property_name).toBe('Мощность')
})
