import { expect, test } from '@playwright/test'

test('completed CSV row can be clarified while later rows process without losing confirmation', async ({ page }) => {
  let release!: () => void
  const waiting = new Promise<void>(resolve => { release = resolve })
  let secondStarted = false
  await page.route('**/api/v1/analyses', async route => {
    if (route.request().postDataJSON().description === 'Стол') { secondStarted = true; await waiting }
    await route.continue()
  })
  try {
    await page.goto('/')
    await page.getByRole('button', { name: 'Загрузить CSV', exact: true }).click()
    await page.locator('input[type=file]').first().setInputFiles({ name: 'background.csv', mimeType: 'text/csv', buffer: Buffer.from('Описание\n"Дрель; Питание: от сети; Мощность: 650 Вт"\nСтол') })
    await expect.poll(() => secondStarted).toBe(true)
    const rows = page.locator('.csv-table tbody tr')
    await expect(rows.nth(0).getByRole('button', { name: 'Уточнить', exact: true })).toBeEnabled()
    await expect(rows.nth(1).getByRole('button', { name: 'Открыть', exact: true })).toBeDisabled()
    await rows.nth(0).getByRole('button', { name: 'Уточнить', exact: true }).click()
    await page.getByRole('button', { name: 'Подтвердить код' }).click()
    await expect(rows.nth(0)).toContainText('Код подтверждён')
    await expect(page.getByRole('button', { name: 'Остановить', exact: true })).toBeVisible()
    release()
    await expect(page.getByRole('button', { name: 'Перепроверить товары', exact: true })).toBeVisible()
    await expect(rows.nth(0)).toContainText('TEST-DRILL-MAINS')
    await expect(rows.nth(0)).toContainText('Код подтверждён')
    await expect(rows.nth(1).getByRole('button', { name: 'Уточнить', exact: true })).toBeEnabled()
  } finally { release() }
})
