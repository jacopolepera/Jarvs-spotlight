// Browser fixtures only; build desktop first. Uses the same harness approach as ui-release-qa.cjs.
const { chromium } = require(process.env.JARVS_PLAYWRIGHT_PATH || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');

(async () => {
  const dist = path.resolve(__dirname, '../desktop/dist');
  const server = http.createServer((req, res) => {
    const file = path.join(dist, req.url === '/' ? 'index.html' : new URL(req.url, 'http://local').pathname);
    if (!file.startsWith(dist + path.sep) || !fs.existsSync(file)) { res.writeHead(404).end(); return; }
    res.setHeader('Content-Type', ({'.html':'text/html', '.js':'text/javascript', '.css':'text/css'})[path.extname(file)] || 'application/octet-stream');
    fs.createReadStream(file).pipe(res);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({headless:true, channel:process.env.JARVS_BROWSER_CHANNEL || 'msedge'});
    const page = await browser.newPage({viewport:{width:600,height:700}});
    const errors = [];
    const results = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      window.__TAURI_INTERNALS__ = {invoke:async()=>({httpUrl:'http://127.0.0.1:4190',wsUrl:'ws://127.0.0.1:4190/chat',token:'fixture'})};
      window.__qa = {decisions:[], emit:event=>window.__qa.socket.onmessage?.({data:JSON.stringify({session_id:'qa',generation_id:window.__qa.generation,...event})})};
      class MockSocket {
        static OPEN = 1; readyState = 1;
        constructor() { window.__qa.socket=this; setTimeout(()=>{this.onopen?.({});window.__qa.emit({type:'session'});},40); }
        send(raw) {
          const request=JSON.parse(raw); window.__qa.generation=request.generation_id;
          setTimeout(()=>{
            window.__qa.emit({type:'started'});
            const edits = request.prompt==='large'
              ? Array.from({length:9},()=>({old_text:'a'.repeat(1400),new_text:'line\n'.repeat(50)}))
              : [{old_text:'<img src=x onerror="window.__unsafe=true">',new_text:'Updated note',expected_count:2},{old_text:'old title',new_text:'new title'}];
            const args = request.prompt==='script' ? {language:'python',code:'print("script preview")'}
              : request.prompt==='generic' ? {path:'notes.txt',text:'Generic write details'}
              : {path:'notes/weekly.txt',expected_sha256:'a'.repeat(64),edits};
            window.__qa.emit({type:'approval_required',approval_id:request.prompt,title:'Confirm changes',tool_name:request.prompt==='script'?'script.run':request.prompt==='generic'?'workspace.write_text':'workspace.edit_text',arguments:args});
          },20);
        }
        close(){this.readyState=3;}
      }
      window.WebSocket=MockSocket;
    });
    await page.route('http://127.0.0.1:4190/**', async route => {
      const pathname=new URL(route.request().url()).pathname;
      const model={id:'local',name:'Local model',provider:'local',model_string:'local'};
      let data=pathname==='/settings'?{models:[model],active_model:'local',active_model_config:model,theme:'Default',execution_policy:'always_ask',voice:{provider:'local'}}:pathname==='/memory'?{items:[]}:pathname==='/routines'?{routines:[]}:{enabled:false,available:false,capabilities:[],state:'disabled'};
      if(pathname.includes('/approvals/')) {
        const decision=route.request().postDataJSON();
        await page.evaluate(d=>window.__qa.decisions.push(d),decision);
        data={status:'ok'};
      }
      await route.fulfill({json:data,headers:{'Access-Control-Allow-Origin':'*'}});
    });
    const check=async(name,fn)=>{await fn();results.push({name,passed:true});};
    const composer=page.getByRole('textbox',{name:'Message Jarvs'});
    const dialog=page.getByRole('dialog',{name:'Confirm execution'});
    const preview=page.getByRole('region',{name:'File edit preview'});
    const open=async(prompt)=>{await composer.fill(prompt);await composer.press('Enter');await dialog.waitFor();};
    const decide=async(action)=>{
      await page.getByRole('button',{name:action==='deny'?'Deny':'Approve once',exact:true}).click();
      await dialog.waitFor({state:'hidden'});
      await page.evaluate(()=>window.__qa.emit({type:'done'}));
      await composer.waitFor({state:'visible'});
    };
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await composer.waitFor();
    await open('edit');
    await check('file edit: path, version, two changes and counts',async()=>{
      assert.match(await preview.innerText(),/notes\/weekly.txt/);
      assert.match(await preview.innerText(),/2 edits · 3 expected replacements/);
      assert.match(await preview.innerText(),/Expected matches: 2/);
      assert.match(await preview.innerText(),/Expected matches: 1 \(default\)/);
      assert.equal(await preview.getByRole('heading',{name:/Edit \d/}).count(),2);
      assert.match(await preview.innerText(),/a{64}/);
    });
    await check('file edit: untrusted HTML remains literal text',async()=>{
      assert.match(await preview.innerText(),/<img src=x onerror="window.__unsafe=true">/);
      assert.equal(await preview.locator('img').count(),0);
      assert.equal(await page.evaluate(()=>window.__unsafe),undefined);
    });
    await check('file edit: safe focus and keyboard-accessible details',async()=>{
      const deny=page.getByRole('button',{name:'Deny',exact:true});
      await deny.waitFor();
      await page.waitForFunction(()=>document.activeElement?.textContent==='Deny');
      await page.keyboard.press('Shift+Tab');
      assert.equal(await page.getByRole('region',{name:'Operation details'}).evaluate(el=>document.activeElement===el),true);
      await page.keyboard.press('Shift+Tab');
      assert.equal(await page.getByRole('button',{name:'Approve once',exact:true}).evaluate(el=>document.activeElement===el),true);
      await page.keyboard.press('Tab');assert(await dialog.evaluate(el=>el.contains(document.activeElement)));
    });
    await decide('deny');
    await check('file edit: deny retains decision contract and returns focus',async()=>{
      const [decision]=await page.evaluate(()=>window.__qa.decisions);
      assert.equal(decision.decision,'deny');assert.equal(decision.session_id,'qa');assert.equal(typeof decision.generation_id,'string');
      assert.equal(await composer.evaluate(el=>document.activeElement===el),true);
    });
    await open('large');
    await check('file edit: large payload has explicit limits and usable controls on mobile',async()=>{
      await page.setViewportSize({width:360,height:640});
      assert.equal(await preview.getByRole('heading',{name:/Edit \d/}).count(),6);
      assert.match(await preview.innerText(),/3 additional edits omitted/);
      assert.match(await preview.innerText(),/Approval applies to the entire operation/);
      assert.match(await preview.innerText(),/Text omitted after 1200 characters or 16 lines/);
      assert((await preview.innerText()).length<12000);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      const button=await page.getByRole('button',{name:'Approve once',exact:true}).boundingBox();
      assert(button && button.y+button.height<=640);
    });
    await decide('approve');
    await check('file edit: approve retains decision contract',async()=>assert.equal((await page.evaluate(()=>window.__qa.decisions))[1].decision,'approve'));
    await open('script');
    await check('other approvals: script code preserved',async()=>assert.match(await dialog.innerText(),/print\("script preview"\)/));
    await decide('deny');
    await open('generic');
    await check('other approvals: generic arguments preserved',async()=>assert.match(await dialog.innerText(),/Generic write details/));
    await decide('deny');
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({surface:'Browser fixtures; not native Tauri',results,errors},null,2));
  } finally {
    if(browser) await browser.close();
    await new Promise(resolve=>server.close(resolve));
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
