/* Presentation of already returned values only: no tool, SQL, or publication calls. */
(() => {
    const root=document.getElementById('report-templates');
    if(!root)return;
    const $=id=>document.getElementById(id);
    let report=null,body=null,layout=root.dataset.default||'comparison';
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
        reset(){report=null;body=null;root.hidden=true;$('template-bars').replaceChildren();const link=$('generate-superset-report');if(link){link.hidden=true;link.removeAttribute('href');$('superset-result-status').textContent='';}},
        show(value,response){
            if(!value||!Array.isArray(value.columns)||!Array.isArray(value.rows))return;
            report=value;body=response;root.hidden=false;
            const link=$('generate-superset-report');
            if(link){
                const token=typeof body.summary_token==='string'?body.summary_token:'';
                link.hidden=!token;
                if(token)link.href=link.dataset.studioUrl+'?report='+encodeURIComponent(token);
                else link.removeAttribute('href');
                $('superset-result-status').textContent=token?'Preview this report, choose a template, then explicitly create a private Superset snapshot.':'This result has no supported export snapshot. Run a structured report with supported numeric fields.';
            }
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
