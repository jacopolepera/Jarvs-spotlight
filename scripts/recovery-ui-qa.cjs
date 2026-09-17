// Browser fixtures only: build desktop first; this does not validate native Tauri IPC.
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
    const page = await browser.newPage({viewport:{width:800,height:700}});
    const errors = [], requests = [], results = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      window.__TAURI_INTERNALS__ = {invoke:async()=>({httpUrl:'http://127.0.0.1:4190',wsUrl:'ws://127.0.0.1:4190/chat',token:'fixture'})};
      window.WebSocket = class { static OPEN=1; readyState=1; constructor(){setTimeout(()=>this.onopen?.({}),20);} send(){} close(){this.readyState=3;} };
    });
    const record = {run_id:'run-1',invocation_id:'operation-1',tool_name:'workspace.write_text',effect:'workspace_write',detected_at:'2026-09-17T10:00:00Z',resolution:null,acknowledged_at:null};
    let list = [record], loadFailure = true, ackMode = 'failure', detailFailure = true;
    await page.route('http://127.0.0.1:4190/**', async route => {
      const request = route.request(), pathname = new URL(request.url()).pathname;
      if (request.method() === 'OPTIONS') { await route.fulfill({status:204,headers:{'Access-Control-Allow-Origin':'*','Access-Control-Allow-Headers':'*','Access-Control-Allow-Methods':'GET,POST,OPTIONS'}}); return; }
      requests.push({path:pathname,method:request.method(),body:request.postData()});
      let status=200, data={enabled:false,available:false,capabilities:[],state:'disabled'};
      if(pathname==='/settings') data={models:[],theme:'Default',workspace_path:'C:/Workspace',execution_policy:'always_ask',cloud_workspace_egress:'deny',voice:{provider:'local',base_url:'http://127.0.0.1:8000/v1',model:'local',language:''}};
      if(pathname==='/memory') data={items:[]};
      if(pathname==='/routines') data={routines:[]};
      if(pathname==='/run-recovery') {status=loadFailure?503:200;data={recoveries:list};}
      if(pathname==='/runs/run-1') {status=detailFailure?503:200;data={run:{run_id:'run-1',status:'failed',started_at:record.detected_at,updated_at:record.detected_at},effects:[{invocation_id:'operation-1',status:'started',started_at:record.detected_at,completed_at:null}]};}
      if(pathname.endsWith('/acknowledge')) {
        assert.equal(request.headers().authorization,'Bearer fixture');
        status=ackMode==='failure'?503:ackMode==='conflict'?409:200;
        data={recovery:ackMode==='malformed'?record:{...record,resolution:request.postDataJSON().resolution,acknowledged_at:'2026-09-17T11:00:00Z'}};
        if(ackMode==='conflict') list=[];
      }
      await route.fulfill({status,json:data,headers:{'Access-Control-Allow-Origin':'*'}});
    });
    const panel=page.getByRole('region',{name:'Interrupted operations'});
    const check=async(name,fn)=>{await fn();results.push(name);};
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.getByRole('button',{name:'Open settings',exact:true}).click();
    await page.getByRole('button',{name:'Recovery',exact:true}).click();
    await check('load failure remains recoverable',async()=>{await panel.getByRole('alert').waitFor();loadFailure=false;await panel.getByRole('button',{name:'Retry',exact:true}).click();await panel.getByText('workspace.write_text',{exact:true}).waitFor();assert(await panel.getByText(/Workspace change/).count());});
    await check('inspect error and retry show recorded uncertainty',async()=>{await panel.getByRole('button',{name:'Inspect run'}).click();await panel.getByRole('button',{name:'Retry details'}).waitFor();detailFailure=false;await panel.getByRole('button',{name:'Retry details'}).click();await panel.getByText('Interrupted or failed',{exact:true}).waitFor();assert(await panel.getByText('Recorded status does not establish whether the effect occurred.',{exact:true}).count());});
    await check('confirmation requires explicit choice and backend success',async()=>{
      const confirm=panel.getByRole('button',{name:'Confirm',exact:true});assert(await confirm.isDisabled());
      await panel.getByLabel('Your verification').selectOption('confirmed_not_applied');await confirm.click();await panel.getByRole('alert').waitFor();assert.equal(await panel.getByLabel('Your verification').count(),1);
      ackMode='malformed';await confirm.click();await panel.getByText(/confirmation could not be verified/).waitFor();assert.equal(await panel.getByLabel('Your verification').count(),1);
      ackMode='success';await confirm.click();await panel.getByText(/^Confirmed not applied ·/).waitFor();assert.equal(await panel.getByLabel('Your verification').count(),0);
    });
    await check('409 refreshes stale list without claiming confirmation',async()=>{await panel.getByRole('button',{name:'Refresh',exact:true}).click();await panel.getByLabel('Your verification').selectOption('reviewed');ackMode='conflict';await panel.getByRole('button',{name:'Confirm',exact:true}).click();await panel.getByText('No operations need verification.',{exact:true}).waitFor();assert(await panel.getByText(/already reviewed elsewhere/).count());});
    await check('mobile layout fits with long identifiers',async()=>{list=[{...record,tool_name:'workspace.'+'long_tool_name_'.repeat(20)}];await page.setViewportSize({width:360,height:640});await panel.getByRole('button',{name:'Refresh',exact:true}).click();await panel.getByLabel('Your verification').waitFor();assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);const bounds=await panel.getByLabel('Your verification').boundingBox();assert(bounds&&bounds.x>=0&&bounds.x+bounds.width<=360);});
    assert.deepEqual(errors,[]);
    assert.equal(requests.filter(r=>r.method==='POST').length,4);
    assert(requests.filter(r=>r.method==='POST').every(r=>r.path.endsWith('/acknowledge')));
    console.log(JSON.stringify({surface:'Browser fixtures; not native Tauri',passed:results,errors},null,2));
  } finally {await browser?.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;});
