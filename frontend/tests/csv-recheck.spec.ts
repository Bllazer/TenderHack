import { expect, test } from '@playwright/test'
test('recheck sends every row again and uses edited description', async ({ page }) => {
 await page.route('**/api/v1/analyses/csv', route => route.fulfill({ json: {rows:[{line:2,description:'Дрель'},{line:3,description:'Стол'}],notes:[],rows_skipped:[],truncated:false} }))
 const descriptions: string[]=[]
 page.on('request', r=>{if(r.url().endsWith('/api/v1/analyses')) descriptions.push(r.postDataJSON().description)})
 await page.goto('/')
 await page.getByRole('button',{name:'Загрузить CSV',exact:true}).click()
 await page.locator('input[type=file]').setInputFiles({name:'test.csv',mimeType:'text/csv',buffer:Buffer.from('Дрель\nСтол')})
 await page.getByRole('button',{name:'Проверить товары',exact:true}).click()
 await expect(page.getByRole('button',{name:'Перепроверить товары',exact:true})).toBeEnabled()
 expect(descriptions).toEqual(['Дрель','Стол'])
 await page.getByRole('button',{name:'Перепроверить товары',exact:true}).click()
 await expect(page.getByRole('button',{name:'Перепроверить товары',exact:true})).toBeEnabled()
 expect(descriptions).toEqual(['Дрель','Стол','Дрель','Стол'])
 await page.getByLabel('Описание строки 2').fill('Дрель; мощность: 650 Вт')
 await page.getByRole('button',{name:'Перепроверить товары',exact:true}).click()
 await expect(page.getByRole('button',{name:'Перепроверить товары',exact:true})).toBeEnabled()
 expect(descriptions.slice(-2)).toEqual(['Дрель; мощность: 650 Вт','Стол'])
})
