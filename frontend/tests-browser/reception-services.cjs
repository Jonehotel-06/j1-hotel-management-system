/* Optional real-browser layout smoke test. Run with a local static frontend
   server on :5500, `npm install --no-save playwright`, and `npx playwright
   install chromium`. All API traffic is mocked test data, never live hotel data.
   API/database integration is covered separately by Django tests. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const origin=process.env.FRONTEND_ORIGIN || 'http://127.0.0.1:5500';
const evidence=process.env.EVIDENCE_DIR;
(async()=>{
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  const results=[];
  try {
    for (const width of [1440,768,390]) {
      for (const pageName of ['amenities','breakfast','minimart']) {
        console.log('Checking '+pageName+' at '+width);
        const context=await browser.newContext({viewport:{width,height:1000},serviceWorkers:'block'});
        const page=await context.newPage(),errors=[];
        await page.addInitScript(()=>{
          localStorage.setItem('jone.auth',JSON.stringify({id:1,email:'browser-test@example.test',full_name:'Test receptionist',role:'RECEPTIONIST'}));
          // Local test credential only. Network API calls are intercepted below.
          sessionStorage.setItem('jone.session',JSON.stringify({access:'browser-test-token'}));
        });
        page.on('pageerror',err=>errors.push(err.message));
        page.on('console',msg=>{if(msg.type()==='error')errors.push(msg.text());});
        await page.route('**/api/**',async route=>{
          const url=route.request().url();let data={results:[],count:0,unread_count:0};
          if(url.includes('capabilities'))data={capabilities:['guest_amenity.issue','breakfast.issue','payment.capture']};
          if(url.includes('reception/guests/'))data={results:[{id:3,full_name:'Test Guest',bookings:[{id:9,reference:'TEST-BOOKING',check_in:'2026-11-16',check_out:'2026-11-18',rooms:['206'],status:'CHECKED_IN'}]}]};
          if(url.includes('breakfast/config'))data={is_enabled:true,service_start:'07:00',service_end:'10:00'};
          if(url.includes('reception/amenities/stock'))data={location:{id:1,code:'RECEPTION',name:'Reception shelf'},balances:[{item_id:5,sku:'TEST-SOAP',name:'Test Guest Soap',base_unit:'PIECE',quantity:'12',low_stock:false}]};
          if(url.includes('minimart/products'))data={results:[{id:1,name:'Test Bottled Water',category:'Drinks',sku:'TEST-WATER',price:'150.00',quantity:'8',unit:'BOTTLE',location:'Reception shelf',low_stock:false,out_of_stock:false}],cash_sessions:[{reference:'TEST-CASH-SESSION'}]};
          if(url.includes('minimart/sales'))data={results:[],count:0,has_next:false,summary:{transactions:0,items_sold:0,total_sales:'0',best_selling:[],low_stock:[]}};
          await route.fulfill({status:200,contentType:'application/json',headers:{'Access-Control-Allow-Origin':'*'},body:JSON.stringify({success:true,data})});
        });
        await page.goto(origin+'/dashboard/'+pageName+'.html');
        await page.waitForFunction(()=>window.ReceptionServices && window.Auth && Auth.state.user);
        await page.locator('[name=guest_id] option[value="3"]').first().waitFor({state:"attached"});
        await page.waitForLoadState('networkidle');
        assert.ok(page.url().endsWith(pageName+'.html'),'Staff shell must not redirect an authorized session: '+page.url());
        const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1);
        assert.equal(overflow,false,pageName+' page must not overflow at width '+width);
        const style=await page.locator('.card-header').first().evaluate(el=>getComputedStyle(el).display);
        assert.equal(style,'flex','Scoped page styling must be loaded');
        if(pageName==='minimart'){
          await page.locator('[data-add]').click();
          await page.locator('[data-mart-total]').filter({hasText:'150'}).waitFor();
          await page.selectOption('#mart-customer','GUEST');
          await page.selectOption('#mart-guest','3');
          assert.match(await page.locator('[data-guest-context]').textContent(),/Test Guest.*206/);
        }
        if(pageName==='amenities'){
          await page.locator('[data-tab="issue"]').click();
          await page.locator('[data-panel="issue"]:not(.hidden)').waitFor();
          await page.selectOption('[name=guest_id]','3');
          assert.equal(await page.locator('[name=room_number]').inputValue(),'206');
        }
        if(pageName==='breakfast'){
          await page.selectOption('[data-breakfast-guest-select]','3');
          assert.match(await page.locator('[data-breakfast-issue-form] [data-guest-context]').textContent(),/206/);
        }
        assert.deepEqual(errors,[],pageName+' must have no console/runtime errors');
        if(evidence){fs.mkdirSync(evidence,{recursive:true});await page.screenshot({path:evidence+'/'+pageName+'-'+width+'.png',fullPage:true});}
        results.push({page:pageName,width,overflow,consoleErrors:errors.length,status:'PASS'});
        await context.close();
      }
    }
    console.log(JSON.stringify(results,null,2));
  } finally {await browser.close();}
})().catch(err=>{console.error(err);process.exit(1);});
