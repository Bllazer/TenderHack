import { expect, test } from '@playwright/test'

test('full API flow, confirmation, correction and category change', async ({ page }) => {
  await page.goto('/')
  await page.getByLabel('Описание товара', { exact: true }).fill('Дрель электрическая; питание: от сети')
  await page.getByRole('button', { name: 'Подобрать КТРУ' }).click()
  await page.getByRole('radio', { name: 'Дрели электрические' }).check()
  await page.getByRole('button', { name: 'Продолжить' }).click()
  await page.locator('#answer').fill('650')
  await expect(page.locator('#answer-unit')).toHaveValue('Вт')
  await page.getByRole('button', { name: 'Продолжить' }).click()
  await expect(page.getByRole('button', { name: 'Подтвердить код' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Подбор завершён' })).toHaveCount(0)
  await page.locator('.result-data > div').filter({ hasText: 'Мощность' }).getByRole('button', { name: 'Исправить' }).click()
  await page.getByLabel('Значение', { exact: true }).fill('700')
  await page.getByRole('button', { name: 'Проверить правку' }).click()
  await expect(page.locator('.result-data')).toContainText('700')
  const changedCategory = page.waitForRequest(r => r.url().endsWith('/api/v1/analyses'))
  await page.getByRole('button', { name: 'Изменить категорию' }).click()
  expect((await changedCategory).postDataJSON().confirmed_ktru_code).toBeNull()
  await page.getByRole('radio', { name: 'Дрели электрические' }).check()
  await page.getByRole('button', { name: 'Продолжить' }).click()
  await expect(page.locator('.result-data')).toContainText('700')
  await page.getByRole('button', { name: 'Подтвердить код' }).click()
  await expect(page.getByRole('heading', { name: 'Подбор завершён' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'TEST-DRILL-MAINS', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Новый подбор' }).click()
  await expect(page.getByLabel('Описание товара', { exact: true })).toHaveValue('')
})

test('stale HTTP response cannot overwrite edited description', async ({ page }) => {
  let release!: () => void
  const gate = new Promise<void>(resolve => { release = resolve })
  let requests = 0
  await page.route('**/api/v1/analyses', async route => {
    const response = await route.fetch()
    if (++requests === 1) await gate
    await route.fulfill({ response }).catch(() => undefined)
  })
  await page.goto('/')
  await page.getByLabel('Описание товара', { exact: true }).fill('Дрель старая')
  await page.getByRole('button', { name: 'Подобрать КТРУ' }).click()
  await expect(page.getByText('Проверяем описание и характеристики…', { exact: false })).toBeVisible()
  await page.getByLabel('Описание товара', { exact: true }).fill('Стол новый; высота: 75 см')
  release()
  await page.getByRole('button', { name: 'Подобрать КТРУ' }).click()
  await page.getByRole('radio', { name: 'Столы', exact: true }).check()
  await page.getByRole('button', { name: 'Продолжить' }).click()
  await page.getByRole('button', { name: 'Подтвердить код' }).click()
  await expect(page.getByRole('heading', { name: 'TEST-TABLE', exact: true })).toBeVisible()
})

test('HTTP errors remain visible without fake result', async ({ page }) => {
  await page.route('**/api/v1/analyses', route => route.fulfill({ status: 503, contentType: 'application/json',
    body: JSON.stringify({ detail: { code: 'analysis_unavailable', message: 'Повторите запрос' } }) }))
  await page.goto('/')
  await page.getByLabel('Описание товара', { exact: true }).fill('Товар')
  await page.getByRole('button', { name: 'Подобрать КТРУ' }).click()
  await expect(page.getByRole('alert')).toContainText('Повторите запрос')
  await expect(page.getByRole('heading', { name: 'Подбор завершён' })).toHaveCount(0)
  await expect(page.getByLabel('Описание товара', { exact: true })).toHaveValue('Товар')
})

for (const failure of ['422', 'network']) {
  test(`${failure} keeps user input and offers a readable error`, async ({ page }) => {
    await page.route('**/api/v1/analyses', route => failure === 'network' ? route.abort('failed') : route.fulfill({ status: 422, json: { detail: [{ loc: ['body', 'description'], msg: 'invalid' }] } }))
    await page.goto('/')
    await page.getByLabel('Описание товара', { exact: true }).fill('Мой товар')
    await page.getByRole('button', { name: 'Подобрать КТРУ' }).click()
    await expect(page.getByRole('alert')).toContainText(failure === 'network' ? 'Не удалось связаться' : 'Проверьте заполнение')
    await expect(page.getByLabel('Описание товара', { exact: true })).toHaveValue('Мой товар')
    await expect(page.getByRole('heading', { name: 'Подбор завершён' })).toHaveCount(0)
  })
}

test('needs_data preserves full state and retry sends it unchanged', async ({ page }) => {
  await page.route('**/api/v1/analyses', async route => {
    const response = await route.fetch()
    const result = await response.json()
    await route.fulfill({ json: { ...result, status: 'needs_data', next_question: null, selected_ktru_code: null,
      issues: [{ code: 'search_index_missing', severity: 'error', explanation: 'Поисковый индекс недоступен', suggested_action: 'Подключите индекс' }], next_steps: [] } })
  })
  await page.goto('/')
  await page.getByLabel('Описание товара', { exact: true }).fill('Дрель; Мощность: 650 Вт')
  await page.getByRole('button', { name: 'Подобрать КТРУ' }).click()
  await expect(page.getByText('Поисковый индекс недоступен', { exact: false })).toBeVisible()
  const retry = page.waitForRequest(r => r.url().endsWith('/api/v1/analyses'))
  const retryResponse = page.waitForResponse(r => r.url().endsWith('/api/v1/analyses') && r.request().method() === 'POST')
  await page.getByRole('button', { name: 'Повторить проверку' }).click()
  expect((await retry).postDataJSON()).toMatchObject({ description: 'Дрель; Мощность: 650 Вт', attributes: [], additional: [], answers: [], confirmed_category_id: null, confirmed_ktru_code: null })
  // The request event precedes route.fetch()/json()/fulfill(). Wait for the
  // retry response and its rendering before Playwright disposes the context.
  const response = await retryResponse
  expect(response.status()).toBe(200)
  await response.finished()
  expect((await response.json()).status).toBe('needs_data')
  await expect(page.getByRole('region', { name: 'Подбор кода КТРУ' })).toHaveAttribute('aria-busy', 'false')
  await expect(page.getByText('Поисковый индекс недоступен', { exact: false })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Повторить проверку' })).toBeEnabled()
  await expect(page.getByRole('heading', { name: 'Подбор завершён' })).toHaveCount(0)
})

test('editing description waits for explicit submit, including pauses and examples', async ({ page }) => {
  await page.goto('/')
  await page.getByLabel('Описание товара', { exact: true }).fill('Стол; Высота: 750 мм')
  await page.getByRole('button', { name: 'Подобрать КТРУ' }).click()
  await page.getByRole('radio', { name: 'Столы', exact: true }).check()
  await page.getByRole('button', { name: 'Продолжить' }).click()
  await page.getByRole('button', { name: 'Подтвердить код' }).click()
  await expect(page.getByRole('heading', { name: 'Подбор завершён' })).toBeVisible()
  await page.getByRole('button', { name: 'Изменить описание' }).click()
  let analysisRequests = 0
  page.on('request', r => { if (r.url().endsWith('/api/v1/analyses')) analysisRequests++ })
  await page.getByLabel('Описание товара', { exact: true }).fill('Др')
  await page.waitForTimeout(1100)
  expect(analysisRequests).toBe(0)
  await expect(page.getByLabel('Описание товара', { exact: true })).toHaveValue('Др')
  await page.getByRole('button', { name: 'Инструмент', exact: true }).click()
  await page.waitForTimeout(1100)
  expect(analysisRequests).toBe(0)
  await page.getByLabel('Описание товара', { exact: true }).fill('Дрель; Мощность: 650 Вт')
  const request = page.waitForRequest(r => r.url().endsWith('/api/v1/analyses'))
  await page.getByRole('button', { name: 'Подобрать КТРУ' }).click()
  expect((await request).postDataJSON()).toMatchObject({ confirmed_category_id: null, confirmed_ktru_code: null, answers: [] })
  await expect(page.getByRole('radio', { name: 'Дрели электрические' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Подбор завершён' })).toHaveCount(0)
})
