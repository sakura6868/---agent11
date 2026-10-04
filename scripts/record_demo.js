// Run inside playwright-cli on an authenticated, isolated demo database.
async (page) => {
  await page.setViewportSize({width:1440,height:1080});
  // Starting recording can recreate the page and clear its session storage.
  if(await page.locator('#login-overlay').isVisible()) {
    await page.locator('#demo-login-btn').click();
    await page.locator('#login-overlay').waitFor({state:'hidden'});
  }
  const scenes=[];
  const show=async(title,action)=>{
    const start=Date.now();
    await action();
    // Clearly identify the recording and the current chapter without obscuring the product.
    await page.evaluate((title)=>{
      let badge=document.querySelector('#demo-recording-label');
      if(!badge){badge=document.createElement('div');badge.id='demo-recording-label';document.body.appendChild(badge);}
      badge.style.cssText='position:fixed;bottom:12px;right:16px;z-index:9999;background:#162522;color:#fff;padding:10px 18px;border-radius:8px;font:15px sans-serif;box-shadow:0 2px 12px #0003';
      badge.textContent='v1.3 本地隔离演示 · '+title;
    },title);
    await page.waitForTimeout(Math.max(0,22000-(Date.now()-start)));
    scenes.push({title,duration_ms:Date.now()-start});
  };
  const route=async(name)=>{await page.getByRole('link',{name,exact:true}).click();await page.waitForTimeout(1000);};
  const ask=async(q)=>{
    await page.locator('#agent-q').fill(q);
    const response=page.waitForResponse(r=>r.url().includes('/api/agent/ask?'));
    await page.locator('#agent-send').click();
    const data=await(await response).json();
    await page.waitForFunction(()=>!document.querySelector('#agent-send').disabled);
    return data;
  };
  await show('首页与画像',async()=>{await route('首页');});
  await show('保存容量约束',async()=>{
    await route('能力画像');
    await page.locator('#f-hours').fill('40');await page.locator('#f-team').fill('3');
    await page.locator('#profile-form button[type=submit]').click();
  });
  await show('官方证据扩充',async()=>{
    await route('赛事大厅');await page.locator('#hall-search').fill('智能体互联');
    await page.waitForTimeout(1200);
    const link=page.locator('#hall-list a').first();
    if(await link.count()) await link.click();
  });
  await show('目标驱动规划',async()=>{
    await route('智能顾问');await page.locator('#agent-mode').selectOption('plan');
    await page.locator('#agent-cid').selectOption('');
    const data=await ask('每周40小时，最多参加两场比赛，比较机会并规划执行清单');
    if(data.planning?.status!=='completed')throw new Error('Demo planning failed');
  });
  await show('工具轨迹与执行清单',async()=>{
    const row=page.locator('.msg-row-agent').last();
    await row.locator('.planning-options').evaluate(el=>el.open=true);
    await row.locator('.decision-theater').evaluate(el=>el.open=true);
    await row.locator('.agent-planning-result').scrollIntoViewIfNeeded();
  });
  await show('零容量的停止条件',async()=>{
    const data=await ask('每周0小时，最多参加两场比赛');
    if(data.planning?.status!=='needs_input'||data.recommendations.length)throw new Error('Unsafe zero-capacity plan');
  });
  await show('行动路线与明确采用',async()=>{
    await route('行动路线');await page.locator('#portfolio-max').selectOption('2');
    await page.locator('#portfolio-hours').fill('40');await page.locator('#portfolio-run').click();
    await page.waitForTimeout(1500);
    const adopt=page.locator('.portfolio-apply:not([disabled])').first();
    if(!await adopt.count())throw new Error('No feasible adoptable portfolio');
    page.once('dialog',dialog=>dialog.accept());await adopt.click();await page.waitForTimeout(1000);
  });
  await show('项目看板与时间线',async()=>{
    await route('我的项目');await page.locator('[data-project-view=board]').click();
    await page.waitForTimeout(8000);await page.locator('[data-project-view=timeline]').click();
  });
  await show('来源变化与人工审核',async()=>{await route('机会提醒');});
  await show('可复核的交付边界',async()=>{await route('首页');});
  return {mode:'real_browser_recording_no_mocking',scenes};
}
