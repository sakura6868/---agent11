// Run with playwright-cli after demo login on the isolated local HTTP preview.
async (page) => {
  const checks = [], errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const check = (id, passed, detail) => checks.push({ id, passed, detail });
  await page.setViewportSize({ width: 1440, height: 1080 });
  await page.getByRole('link', { name: '能力画像', exact: true }).click();
  await page.locator('#f-hours').fill('12');
  await page.locator('#f-team').fill('3');
  await page.locator('#profile-form button[type=submit]').click();
  await page.waitForTimeout(500);
  await page.getByRole('link', { name: '智能顾问', exact: true }).click();
  await page.locator('#agent-mode').selectOption('plan');
  await page.locator('#agent-cid').selectOption('');
  const ask = async (question) => {
    await page.locator('#agent-q').fill(question);
    const response = page.waitForResponse(r => r.url().includes('/api/agent/ask?'));
    await page.locator('#agent-send').click();
    const data = await (await response).json();
    await page.waitForFunction(() => !document.querySelector('#agent-send').disabled);
    return data;
  };
  let data = await ask('每周40小时，最多参加两场比赛，比较机会并规划执行清单');
  check('PLAN-B01', data.planning?.status === 'completed' && data.trace.length === 6,
    'Real HTTP planning loop completed with six tool observations');
  check('PLAN-B02', data.recommendations.length <= 2 && data.citations.length > 0,
    'Plan respects requested capacity and count, with official citations');
  check('PLAN-B03', await page.locator('.msg-row-agent').last().getByRole('link', { name: '确认能力画像', exact: true }).count() === 1,
    'Temporary constraints require explicit profile confirmation before adoption');
  await page.locator('.msg-row-agent').last().locator('.planning-options').evaluate(el => { el.open = true; });
  await page.locator('.msg-row-agent').last().locator('.decision-theater').evaluate(el => { el.open = true; });
  await page.screenshot({ path: 'output/playwright/planning-desktop.png' });
  data = await ask('每周0小时，最多参加两场比赛');
  check('PLAN-B04', data.planning?.status === 'needs_input' && data.recommendations.length === 0,
    'Zero capacity yields a request to adjust constraints, not an invented plan');
  await page.locator('#agent-cid').selectOption('china_softcup_2026');
  data = await ask('帮我规划这个比赛');
  check('PLAN-B05', data.planning?.decisions.length === 2 && data.recommendations.length === 0,
    'Expired target terminates after source/time verification');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('#agent-context-clear').click();
  data = await ask('每周40小时，最多参加两场比赛');
  check('PLAN-B06', await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
    '390px mobile has no horizontal overflow after adding planning selector');
  check('PLAN-B07', await page.locator('#agent-mode').isVisible() && await page.locator('#agent-send').isVisible(),
    'Planning selector and send action remain accessible on mobile');
  await page.screenshot({ path: 'output/playwright/planning-mobile.png' });
  check('PLAN-B08', errors.length === 0, 'No page errors in the planning workflow');
  return { executed_at: new Date().toISOString(), mode: 'real_browser_local_http_no_response_mocking',
    checks, errors, summary: { passed: checks.filter(c => c.passed).length, total: checks.length } };
}
