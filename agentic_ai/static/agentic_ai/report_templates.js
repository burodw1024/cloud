/* Display controls and explicit Superset publication for the current result. */
(() => {
    const root=document.getElementById('report-templates');
    if(!root)return;
    const $=id=>document.getElementById(id);
    let report=null,body=null,layout=root.dataset.default||'comparison';
    const publishButton=$('quick-superset-publish');
    const guidedLink=$('generate-superset-report');
    const dashboardLink=$('open-superset-result');
    const reviewLink=$('review-superset-export');
    const publishStatus=$('superset-result-status');
    const readyPrompt=$('report-ready-prompt');
    const promptPreview=$('prompt-preview-report');
    let promptedToken='';
    function closePrompt() {
        readyPrompt?.hidePopover?.();
        if(readyPrompt)readyPrompt.hidden=true;
    }
    $('dismiss-report-ready')?.addEventListener('click', closePrompt);
    $('prompt-publish-report')?.addEventListener('click', () => {
        closePrompt();
        publishButton.click();
        publishStatus.focus();
    });
    promptPreview?.addEventListener('click', closePrompt);
    let publishToken='', resultVersion=0;
    const publishing=new Set();
    function configurePublishing(token) {
        if(!publishButton)return;
        publishToken=token;
        resultVersion++;
        publishButton.hidden=guidedLink.hidden=!token;
        publishButton.disabled=publishing.has(token);
        dashboardLink.hidden=reviewLink.hidden=true;
        dashboardLink.removeAttribute('href');
        if(token) guidedLink.href=guidedLink.dataset.studioUrl+'?report='+encodeURIComponent(token);
        else guidedLink.removeAttribute('href');
        closePrompt();
        if(token && token!==promptedToken && !publishing.has(token)) {
            promptedToken=token;
            promptPreview.href=guidedLink.href;
            readyPrompt.hidden=false;
            readyPrompt.showPopover?.();
        }
        publishStatus.textContent=token
            ? 'Publish with the default template, or customize your dashboard first.'
            : 'This result has no exportable snapshot.';
    }
    publishButton?.addEventListener('click', async () => {
        const token=publishToken, version=resultVersion;
        if(!token||publishing.has(token))return;
        closePrompt();
        publishing.add(token);
        publishButton.disabled=true;
        reviewLink.hidden=true;
        publishStatus.textContent='Building and publishing your private dashboard…';
        try {
            const csrf=document.querySelector('#agent-query-form [name="csrfmiddlewaretoken"]').value;
            const response=await fetch(publishButton.dataset.endpoint, {
                method:'POST', credentials:'same-origin',
                headers:{'Content-Type':'application/json','X-CSRFToken':csrf},
                body:JSON.stringify({action:'quick_publish', report_token:token}),
            });
            const result=await response.json();
            if(version!==resultVersion)return;
            if(!response.ok||!result.ok){
                reviewLink.hidden=!result.export_id;
                throw new Error(result.error||'Superset publishing could not complete.');
            }
            const url=new URL(result.url);
            if(!['https:','http:'].includes(url.protocol))throw new Error('The dashboard returned an invalid URL.');
            dashboardLink.href=url.href;
            dashboardLink.hidden=false;
            const focused=document.activeElement===publishButton||document.activeElement===publishStatus;
            publishButton.hidden=true;
            if(focused)dashboardLink.focus();
            publishStatus.textContent=result.message+(result.fallback?' The default template did not fit this result, so comparison was used.':'');
        } catch(error) {
            if(version===resultVersion)publishStatus.textContent=error.message||'Superset publishing could not complete.';
        } finally {
            publishing.delete(token);
            if(token===publishToken)publishButton.disabled=false;
        }
    });
    const numeric=value=>{
        if(typeof value!=='number' && (typeof value!=='string'||! /^-?\d+(?:\.\d+)?$/.test(value)))return null;
        const n=Number(value);return Number.isFinite(n)?n:null;
    };
    function fields(){return report.columns.filter(key=>!/(?:CODE|NAME|DATE|PHONE|RANK|FREQUENCY|(?:^|_)ID(?:$|_)|ACCOUNT)/i.test(key)&&report.rows.some(row=>numeric(row[key])!==null)&&report.rows.every(row=>row[key]==null||numeric(row[key])!==null));}
    function chart(){
        const key=$('template-measure').value,rows=report.rows.slice(0,20);
        const max=Math.max(1,...rows.map(row=>Math.abs(numeric(row[key])??0)));
        $('template-bars').replaceChildren();
        rows.forEach((row,index)=>{
            const line=document.createElement('div');line.className='template-bar';
            const label=document.createElement('span');label.textContent='Row '+(index+1);
            const track=document.createElement('span');track.className='template-bar-track';track.setAttribute('aria-hidden','true');
            const bar=document.createElement('i');const value=numeric(row[key]);bar.style.width=(value===null?0:Math.abs(value)/max*50)+'%';bar.dataset.negative=String(value<0);track.append(bar);
            const exact=document.createElement('span');exact.textContent=row[key]==null?'Not provided':String(row[key]);
            line.append(label,track,exact);$('template-bars').append(line);
        });
    }
    function apply(){
        if(!report)return;
        root.querySelectorAll('[data-report-template]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.reportTemplate===layout)));
        $('report-template-heading').hidden=layout!=='management';
        $('template-comparison').hidden=layout==='table'||!fields().length;
        $('report-template-notice').textContent='Layout changes use this result only; they do not rerun the query or publish to Superset.'+(layout!=='table'&&!fields().length?' No suitable numeric comparison fields were returned; the source table is shown.':'');
        if(!$('template-comparison').hidden)chart();
    }
    root.querySelectorAll('[data-report-template]').forEach(button=>button.addEventListener('click',()=>{layout=button.dataset.reportTemplate;apply();}));
    $('template-measure').addEventListener('change',chart);
    $('print-current-report').addEventListener('click',()=>window.print());
    window.BuroReportTemplates={
        reset(){report=null;body=null;root.hidden=true;$('template-bars').replaceChildren();configurePublishing('');},
        show(value,response){
            if(!value||!Array.isArray(value.columns)||!Array.isArray(value.rows))return;
            report=value;body=response;root.hidden=false;
            configurePublishing(typeof body.summary_token==='string'?body.summary_token:'');
            $('report-template-facts').replaceChildren();
            [report.rows.length+' displayed rows',report.columns.length+' fields',report.report_date||body.plan?.params?.report_date||body.plan?.params?.reportDate||'Date not provided'].forEach(text=>{const item=document.createElement('span');item.textContent=text;$('report-template-facts').append(item);});
            $('report-template-scope').textContent=(typeof report.scope==='string'?report.scope:'Displayed authorized rows only.')+(report.truncated?' The source result is limited.':'');
            $('template-measure').replaceChildren();
            fields().forEach(key=>{const option=document.createElement('option');option.value=key;option.textContent=key.replace(/_/g,' ');$('template-measure').append(option);});
            if(!['table','comparison','management'].includes(layout))layout='comparison';
            apply();
        }
    };
})();
