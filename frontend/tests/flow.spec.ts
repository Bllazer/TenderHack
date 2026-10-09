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
  await page.getByRole('button', { name: 'Изменить категорию' }).click()
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
  await expect(page.getByText('Проверяем описание и характеристики…')).toBeVisible()
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
