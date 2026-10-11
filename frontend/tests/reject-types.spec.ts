import { expect, test } from '@playwright/test'

test('rejecting suggested types returns to saved description and clears category context', async ({ page }) => {
  await page.goto('/')
  await page.getByLabel('Описание товара', { exact: true }).fill('Дрель')
  await page.getByRole('button', { name: 'Подобрать КТРУ' }).click()
  await page.getByRole('button', { name: 'Ни один тип не подходит — изменить запрос' }).click()
  const description = page.getByLabel('Описание товара', { exact: true })
  await expect(description).toHaveValue('Дрель')
  await expect(description).toBeFocused()
  await expect(page.getByRole('status')).toContainText('Уточните описание товара')
  await description.fill('Контейнеры для заморозки продуктов')
  const request = page.waitForRequest(r => r.url().endsWith('/api/v1/analyses'))
  await description.press('Enter')
  expect((await request).postDataJSON()).toMatchObject({ confirmed_category_id: null, confirmed_ktru_code: null, answers: [] })
})
