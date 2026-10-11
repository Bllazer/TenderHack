import { expect, test } from '@playwright/test'
test('one candidate opens result with missing fields and permits voluntary clarification', async ({ page }) => {
  await page.route('**/api/v1/analyses', async route => {
    const response = await route.fetch(); const data = await response.json()
    if (data.next_question?.kind === 'property') await route.fulfill({ json: { ...data, candidate_count: 1, candidates: data.candidates.slice(0, 1) } })
    else await route.fulfill({ response })
  })
  await page.goto('/')
  await page.getByLabel('Описание товара', { exact: true }).fill('Дрель; питание: от сети')
  await page.getByRole('button', { name: 'Подобрать КТРУ' }).click()
  await page.getByRole('radio', { name: 'Дрели электрические' }).check()
  await page.getByRole('button', { name: 'Продолжить' }).click()
  await expect(page.getByRole('heading', { name: 'Проверьте результат' })).toBeVisible()
  await expect(page.locator('.candidate')).toContainText('Мощность')
  await expect(page.getByRole('button', { name: 'Подтвердить код' })).toHaveCount(0)
  await page.getByRole('button', { name: 'Уточнить характеристики' }).click()
  await expect(page.getByLabel('Значение', { exact: true })).toBeVisible()
})
test('no similar categories shows helpful empty state instead of empty question', async ({ page }) => {
  await page.route('**/api/v1/analyses', async route => {
    const response = await route.fetch(); const data = await response.json()
    await route.fulfill({ json: { ...data, categories: [], candidates: [], candidate_count: 0,
      issues: [{ code: 'no_similar_products', severity: 'warning', explanation: 'Похожих товаров нет', suggested_action: 'Уточните назначение' }], next_steps: ['Добавьте название товара'] } })
  })
  await page.goto('/')
  await page.getByLabel('Описание товара', { exact: true }).fill('Неизвестный товар')
  await page.getByRole('button', { name: 'Подобрать КТРУ' }).click()
  await expect(page.getByRole('heading', { name: 'Ничего похожего не найдено' })).toBeVisible()
  await expect(page.getByText('Добавьте название товара')).toBeVisible()
  await expect(page.locator('#question-form')).toHaveCount(0)
  await page.locator('.edit-actions > summary').click()
  await page.getByRole('button', { name: 'Изменить описание' }).click()
  await expect(page.getByLabel('Описание товара', { exact: true })).toHaveValue('Неизвестный товар')
})
