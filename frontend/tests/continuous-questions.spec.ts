import { expect, test } from '@playwright/test'

test('after voluntarily starting clarification, remaining properties continue without intermediate result', async ({ page }) => {
  let step = 0
  await page.route('**/api/v1/analyses', async route => {
    const response = await route.fetch(); const data = await response.json()
    if (step++ === 2) return route.fulfill({ json: { ...data, status: 'needs_input', candidate_count: 1, selected_ktru_code: null,
      next_question: { id: 'size', kind: 'property', title: 'Укажите размер', reason: 'Обязательная характеристика', property_name: 'Размер', options: [], unit: null, multiple: false, allow_unknown: true } } })
    await route.fulfill({ response })
  })
  await page.goto('/')
  await page.getByLabel('Описание товара', { exact: true }).fill('Дрель; Питание: от сети')
  await page.getByRole('button', { name: 'Подобрать КТРУ' }).click()
  await page.getByRole('radio', { name: 'Дрели электрические' }).check()
  await page.getByRole('button', { name: 'Продолжить' }).click()
  await expect(page.getByRole('heading', { name: 'Проверьте результат' })).toBeVisible()
  await page.getByRole('button', { name: 'Уточнить характеристики' }).click()
  await page.getByLabel('Значение', { exact: true }).fill('650')
  await page.getByRole('button', { name: 'Продолжить' }).click()
  await expect(page.getByRole('heading', { name: 'Укажите размер' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Уточнить характеристики' })).toHaveCount(0)
  await page.getByRole('radio', { name: 'Не знаю', exact: true }).check()
  await page.getByRole('button', { name: 'Продолжить' }).click()
  await expect(page.getByRole('heading', { name: 'Проверьте результат' })).toBeVisible()
})
