// Uses the frontend's installed Playwright, but never changes frontend sources or mocks HTTP.
const { chromium } = require('../../frontend/node_modules/@playwright/test');
const fs = require('node:fs');
const cases = JSON.parse(fs.readFileSync('.tools/real-runtime-http.json', 'utf8')).map(x => x.case).filter(x => x.description);
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const reports = [];
  const next = async action => {
    const pending = page.waitForResponse(r => r.url().endsWith('/api/v1/analyses') && r.request().method() === 'POST', {timeout: 120000});
    await action();
    const response = await pending;
    if (response.status() !== 200) throw new Error(await response.text());
    const result = await response.json();
    await page.locator('.picker').getAttribute('aria-busy');
    await page.waitForFunction(() => document.querySelector('.picker')?.getAttribute('aria-busy') === 'false');
    return result;
  };
  for (const item of cases) {
    await page.goto(process.env.SMOKE_FRONTEND || 'http://127.0.0.1:8080');
    await page.locator('#description').fill(item.description);
    let result = await next(() => page.locator('button[form="description-form"]').click());
    const option = result.next_question.options.findIndex(x => x.value === item.category);
    if (option < 0) throw new Error('Real type is not offered: ' + item.category);
    await page.locator('input[name="answer"]').nth(option).check();
    result = await next(() => page.locator('button[form="question-form"]').click());
    if (result.next_question?.kind === 'property') {
      const index = result.next_question.options.findIndex(x => x.value === item.value);
      if (index >= 0) await page.locator('input[name="answer"]').nth(index).check();
      else await page.locator('#answer').fill(item.value);
      result = await next(() => page.locator('button[form="question-form"]').click());
    }
    if (result.next_question?.kind !== 'code') throw new Error(JSON.stringify(result));
    result = await next(() => page.locator('button.primary').filter({hasText: 'Подтвердить код'}).click());
    if (result.status !== 'resolved' || result.selected_ktru_code !== item.code) throw new Error(JSON.stringify(result));
    await page.locator('.result-name').waitFor();
    reports.push({description:item.description, code:result.selected_ktru_code, verification:result.verification});
    console.log(item.description + ': real browser resolved ' + result.selected_ktru_code);
    await page.screenshot({path: '.tools/real-' + item.code + '.png', fullPage:true});
    await next(() => page.getByRole('button', {name:'Изменить категорию', exact:true}).click());
    if (!(await page.locator('input[name="answer"]').count())) throw new Error('Cannot change category');
  }
  const begin = async (description, category = cases[0].category) => {
    await page.goto(process.env.SMOKE_FRONTEND || 'http://127.0.0.1:8080');
    await page.locator('#description').fill(description);
    let result = await next(() => page.locator('button[form="description-form"]').click());
    const index = result.next_question.options.findIndex(x => x.value === category);
    if (index < 0) throw new Error('Category missing: ' + category);
    await page.locator('input[name="answer"]').nth(index).check();
    return next(() => page.locator('button[form="question-form"]').click());
  };
  for (const decision of ['accept:Марка', 'reject']) {
    let result = await begin('Битум нефтяной строительный; Марка: БН 70/30; Маарка: БН 90/10');
    const index = result.next_question.options.findIndex(x => x.value === decision);
    if (index < 0) throw new Error('Correction option missing');
    await page.locator('input[name="answer"]').nth(index).check();
    result = await next(() => page.locator('button[form="question-form"]').click());
    if (decision.startsWith('accept')) {
      if (result.status === 'resolved' || !result.issues.some(x => x.code === 'conflicting_facts')) throw new Error('Ignored conflict');
      const index = result.facts.findIndex(x => x.value === 'БН 90/10');
      await page.locator('.result-data > div').nth(index).getByRole('button', {name:'Исправить',exact:true}).click();
      await page.getByLabel('Значение', {exact:true}).fill('БН 70/30');
      result = await next(() => page.locator('button[form="correction-form"]').click());
    }
    if (result.next_question?.kind !== 'code') throw new Error(JSON.stringify(result));
    result = await next(() => page.getByRole('button', {name:'Подтвердить код',exact:true}).click());
    if (result.status !== 'resolved') throw new Error('Cannot continue after correction');
    reports.push({scenario:decision,status:result.status});
    console.log('Real browser correction ' + decision + ': resolved after explicit decisions');
  }
  let result = await begin('Битум нефтяной строительный');
  await page.getByText('Не знаю', {exact:true}).click();
  result = await next(() => page.locator('button[form="question-form"]').click());
  if (result.status === 'resolved') throw new Error('Unknown became proof');
  const index = result.facts.findIndex(x => x.unknown);
  await page.locator('.result-data > div').nth(index).getByRole('button', {name:'Исправить',exact:true}).click();
  await page.getByLabel('Значение', {exact:true}).fill('БН 70/30');
  result = await next(() => page.locator('button[form="correction-form"]').click());
  if (result.next_question?.kind !== 'code') throw new Error('Unknown fact cannot be corrected: ' + JSON.stringify(result));
  reports.push({scenario:'unknown then fact edit',status:result.status});
  console.log('Real browser unknown -> edit -> code confirmation available');
  for (const [scenario, description, category, expected] of [
    ['ambiguity', 'Газ горючий природный (газ естественный); Назначение: Промышленное и коммунально-бытовое; Объем баллона: 11 л', '06.20.10.110-00000009', 'ambiguous'],
    ['no rules', 'Нефть сырая', '06.10.10.100-00000001', 'needs_data'],
    ['unsupported constraints', 'Контур дыхательный, одноразового использования', '32.50.13.190-00009', 'needs_data'],
    ['wrong unit', 'Дрель ручная электрическая; Тип питания: От сети; Мощность: 650 кг', '28.24.11.000-00000003', 'needs_input'],
  ]) {
    const response = await begin(description, category);
    if (response.status !== expected || response.selected_ktru_code !== null) throw new Error(JSON.stringify(response));
    if (await page.getByRole('button', {name:'Подтвердить код',exact:true}).count()) throw new Error('Confirmation offered without proof');
    reports.push({scenario,status:response.status,candidate_count:response.candidate_count});
    console.log('Real browser ' + scenario + ': ' + response.status);
  }
  for (const description of ['лопата совковая', 'ловпата совкавая', 'Лопата; Тип лопаты: Совкавая']) {
    for (const decision of description === 'лопата совковая' ? [null] : ['accept', 'reject']) {
      let response = await begin(description, '25.99.29.120-00004');
      if (decision) {
        if (response.next_question?.kind !== 'correction') throw new Error('Value correction not offered');
        const index = response.next_question.options.findIndex(x => decision === 'reject' ? x.value === 'reject' : x.label.includes('Совковая'));
        if (index < 0) throw new Error('Value correction option missing');
        await page.locator('input[name="answer"]').nth(index).check();
        response = await next(() => page.locator('button[form="question-form"]').click());
      }
      if (response.status !== 'needs_data' || response.next_question !== null || response.selected_ktru_code !== null)
        throw new Error(JSON.stringify(response));
      if (!response.candidates.every(x => x.blocking_reasons.some(b => b.unsupported_reasons.includes('multiple_choice_not_supported'))))
        throw new Error('Missing source diagnostics');
      reports.push({description, decision, status:response.status, facts:response.facts});
      console.log('Real browser ' + description + ' / ' + decision + ': needs_data with exact diagnostics');
      await page.screenshot({path:'.tools/real-shovel-' + reports.length + '.png',fullPage:true});
    }
  }
  fs.writeFileSync('.tools/real-runtime-browser.json', JSON.stringify(reports,null,2));
  await browser.close();
})().catch(error => { console.error(error); process.exit(1); });
