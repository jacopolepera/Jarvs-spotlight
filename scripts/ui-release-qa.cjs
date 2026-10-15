// Browser fixtures only: this does not validate Tauri IPC, shortcuts or native resizing.
// Build desktop first. Override JARVS_PLAYWRIGHT_PATH / JARVS_BROWSER_CHANNEL as needed.
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
  const browser = await chromium.launch({headless:true, channel:process.env.JARVS_BROWSER_CHANNEL || 'msedge'});
  const errors = [];
  const results = [];
  const page = await browser.newPage({viewport:{width:600,height:120}});
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {
    window.__TAURI_INTERNALS__ = {invoke:async()=>({httpUrl:'http://127.0.0.1:4190',wsUrl:'ws://127.0.0.1:4190/chat',token:'fixture'})};
    window.__qa = {sent:[], decisions:[], emit:event=>window.__qa.socket.onmessage?.({data:JSON.stringify({session_id:'qa',generation_id:window.__qa.generation,...event})})};
    class MockSocket {
      static OPEN = 1; readyState = 1;
      constructor() { window.__qa.socket=this; setTimeout(()=>{this.onopen?.({});window.__qa.emit({type:'session'});},40); }
      send(raw) {
        const request=JSON.parse(raw); window.__qa.sent.push(request); window.__qa.generation=request.generation_id;
        setTimeout(()=>{
          window.__qa.emit({type:'started'});
          if (request.prompt==='approval') window.__qa.emit({type:'approval_required', approval_id:'approval-1', title:'Write a note', tool_name:'workspace.write_text', arguments:{code:'example'}});
          else if (request.prompt==='long') window.__qa.emit({type:'chunk',channel:'answer',text:Array.from({length:80},(_,i)=>`Paragraph ${i}: a readable response for the scrolling check.\n\n`).join('')});
          else { window.__qa.emit({type:'chunk',channel:'answer',text:'Partial response'}); if (request.prompt!=='hold') window.__qa.emit({type:'done'}); }
        },20);
      }
      close(){this.readyState=3;}
    }
    window.WebSocket=MockSocket;
  });
  await page.route('http://127.0.0.1:4190/**', async route => {
    const pathname=new URL(route.request().url()).pathname;
    const model={id:'local',name:'Local model',provider:'local',model_string:'local',api_base:'http://127.0.0.1:1234/v1'};
    let data=pathname==='/settings'?{models:[model],active_model:'local',active_model_config:model,theme:'Default',workspace_path:'C:/Workspace',execution_policy:'always_ask',cloud_workspace_egress:'deny',voice:{provider:'local',base_url:'http://127.0.0.1:8000/v1',model:'Systran/faster-whisper-small',language:''}}:pathname==='/memory'?{items:[]}:pathname==='/routines'?{routines:[]}:{enabled:false,available:false,capabilities:[],state:'disabled'};
    if (pathname==='/session/cancel') { data={status:'ok',cancelled:true}; await page.evaluate(()=>setTimeout(()=>window.__qa.emit({type:'error',code:'generation_cancelled'}),40)); }
    if (pathname.includes('/approvals/')) { const decision=route.request().postDataJSON(); await page.evaluate(d=>window.__qa.decisions.push(d),decision); data={status:'ok'}; }
    await route.fulfill({json:data,headers:{'Access-Control-Allow-Origin':'*'}});
  });
  const check=async(name, fn)=>{try{await fn();results.push({name,passed:true});}catch(error){results.push({name,passed:false,error:error.message});}};
  const composer=page.getByRole('textbox',{name:'Message Jarvs'});
  const pause=()=>page.waitForTimeout(100);
  try {
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await composer.waitFor(); await pause();
    await check('600x120: ready composer gets keyboard focus',async()=>assert.equal(await composer.evaluate(el=>document.activeElement===el),true));
    await check('600x120: compact controls fit',async()=>{for(const locator of [composer,page.getByRole('button',{name:'Attach file',exact:true})]) {const box=await locator.boundingBox();assert(box&&box.x>=0&&box.y>=0&&box.x+box.width<=600&&box.y+box.height<=120);} });
    await page.exposeFunction('qaResizeWindow',async layout=>{
      const width=layout.mode==='expanded'?600:page.viewportSize().width;
      await page.setViewportSize({width,height:layout.mode==='expanded'?500:layout.height || 120});
    });
    await page.evaluate(()=>{
      window.isTauri=true;
      window.__qa.originalInvoke=window.__TAURI_INTERNALS__.invoke;
      window.__qa.activeResizes=0; window.__qa.maxActiveResizes=0; window.__qa.layouts=[];
      window.__TAURI_INTERNALS__.invoke=async (command,args)=>{
        if(command!=='set_main_window_mode') return window.__qa.originalInvoke(command,args);
        window.__qa.activeResizes++;
        window.__qa.maxActiveResizes=Math.max(window.__qa.maxActiveResizes,window.__qa.activeResizes);
        try {
          await new Promise(resolve=>setTimeout(resolve,args.mode==='expanded'?100:10));
          await window.qaResizeWindow(args);
          window.__qa.layouts.push(args);
        } finally {window.__qa.activeResizes--;}
      };
    });
    await check('fresh chat: wrapped draft grows only the composer window',async()=>{
      const draft='CIAO '.repeat(28).trim();
      assert(draft.length<160 && !draft.includes('\n'));
      await composer.fill(draft);
      await page.waitForFunction(()=>window.innerHeight>120 && window.__qa.activeResizes===0);
      assert(page.viewportSize().height<300);
      assert.equal(await page.locator('.app-shell').getAttribute('data-window-mode'),'compact');
      assert.equal(await composer.inputValue(),draft);
      const box=await composer.boundingBox(); assert(box && box.y+box.height<=page.viewportSize().height);
      assert.equal(await page.evaluate(()=>window.__qa.sent.length),0);
      await composer.fill('');
      await page.waitForFunction(()=>window.innerHeight===120 && window.__qa.activeResizes===0);
    });
    await check('fresh chat: narrowing remeasures, clearing restores compact height',async()=>{
      const draft='CIAO '.repeat(9).trim();
      await composer.fill(draft); await pause();
      await page.setViewportSize({width:300,height:120});
      await page.waitForFunction(()=>window.innerHeight>120 && window.__qa.activeResizes===0);
      assert.equal(await composer.inputValue(),draft);
      assert.equal(page.viewportSize().width,300);
      await composer.fill('');
      await page.waitForFunction(()=>window.innerHeight===120 && window.__qa.activeResizes===0);
      await page.setViewportSize({width:600,height:120});
    });
    await check('empty chat: closing settings cannot leave a stale expanded window',async()=>{
      await page.getByRole('button',{name:'Open settings',exact:true}).click();
      await page.keyboard.press('Escape');
      await page.waitForFunction(()=>window.__qa.activeResizes===0 && window.innerHeight===120);
      assert.equal(await page.locator('.app-shell').getAttribute('data-window-mode'),'compact');
      assert.equal(await page.evaluate(()=>window.__qa.maxActiveResizes),1);
      assert.equal(await composer.inputValue(),'');
    });
    await page.evaluate(()=>{window.isTauri=false;window.__TAURI_INTERNALS__.invoke=window.__qa.originalInvoke;});
    await page.setViewportSize({width:800,height:700});
    await page.getByRole('button',{name:'Open settings',exact:true}).click(); await pause();
    await check('settings: focus wraps and Escape restores trigger',async()=>{
      const dialog=page.getByRole('dialog',{name:'Jarvs Spotlight settings'});
      assert(await dialog.evaluate(el=>el.contains(document.activeElement)));
      await page.getByRole('button',{name:'Close settings',exact:true}).focus();
      await page.keyboard.press('Shift+Tab'); assert(await dialog.evaluate(el=>el.contains(document.activeElement)));
      await page.keyboard.press('Escape'); await pause();
      assert.equal(await page.getByRole('button',{name:'Open settings',exact:true}).evaluate(el=>document.activeElement===el),true);
    });
    if(await page.getByRole('dialog').count()) await page.keyboard.press('Escape');
    await composer.fill('first'); await composer.press('Shift+Enter'); await composer.press('End'); await composer.type('second');
    await check('composer: Shift+Enter keeps a draft',async()=>{assert.equal(await composer.inputValue(),'first\nsecond');assert.equal(await page.locator('.app-shell').getAttribute('data-window-mode'),'compact');assert.equal(await page.evaluate(()=>window.__qa.sent.length),0);});
    await composer.fill('hold'); await composer.press('Enter'); await pause();
    await composer.fill('next draft'); await page.getByRole('button',{name:'Stop response',exact:true}).click(); await pause();
    await check('Stop: partial answer, visible status and draft preserved',async()=>{assert.equal(await page.getByText('Response stopped',{exact:true}).count(),1);assert.equal(await composer.inputValue(),'next draft');assert(await page.getByText('Partial response',{exact:true}).count());});
    await composer.fill('long'); await composer.press('Enter'); await pause();
    const history=page.getByLabel('Conversation',{exact:true});
    await history.evaluate(el=>{el.scrollTop=0;el.dispatchEvent(new Event('scroll'));});await pause();
    await page.evaluate(()=>window.__qa.emit({type:'chunk',channel:'answer',text:'\n\nAnother streamed paragraph.'}));await pause();
    await check('scroll: reading position survives new chunks',async()=>{assert.equal(await history.evaluate(el=>el.scrollTop),0);await page.getByRole('button',{name:'Scroll to latest message'}).click();assert(await history.evaluate(el=>el.scrollHeight-el.scrollTop-el.clientHeight<64));});
    await page.evaluate(()=>window.__qa.emit({type:'done'}));await pause();
    await composer.fill('approval');await composer.press('Enter');await pause();
    await check('approval: safe initial focus and keyboard stays in dialog',async()=>{
      const dialog=page.getByRole('dialog',{name:'Confirm execution'});
      assert.equal(await page.getByRole('button',{name:'Deny',exact:true}).evaluate(el=>document.activeElement===el),true);
      await page.keyboard.press('Shift+Tab');assert.equal(await page.getByRole('button',{name:'Approve once',exact:true}).evaluate(el=>document.activeElement===el),true);
      await page.keyboard.press('Tab');assert.equal(await page.getByRole('button',{name:'Deny',exact:true}).evaluate(el=>document.activeElement===el),true);
      assert(await dialog.evaluate(el=>el.contains(document.activeElement)));
    });
    await page.getByRole('button',{name:'Deny',exact:true}).click();await pause();await page.evaluate(()=>window.__qa.emit({type:'error',code:'generation_cancelled'}));await pause();
    await page.setViewportSize({width:360,height:640});
    await page.getByRole('button',{name:'Open settings',exact:true}).click();await pause();
    await check('360x640: settings remain inside viewport',async()=>{const box=await page.getByRole('dialog').boundingBox();assert(box&&box.x>=0&&box.y>=0&&box.x+box.width<=361&&box.y+box.height<=641);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);});
    await page.keyboard.press('Escape');await pause();
    await check('360x640: composer fits after conversation',async()=>{const box=await composer.boundingBox();assert(box&&box.x>=0&&box.x+box.width<=360&&box.y+box.height<=640);});
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({surface:'Browser fixtures; not native Tauri',results,errors},null,2));
    if(results.some(result=>!result.passed))process.exitCode=1;
  } finally { await browser.close(); await new Promise(resolve=>server.close(resolve)); }
})().catch(error=>{console.error(error);process.exitCode=1;});
