const test = require('node:test');
const assert = require('node:assert/strict');
const chart = require('../../static/agentic_ai/report_chart.js');
test('preserves original sequence, exact values and rank on metric changes', () => {
    const report = {columns:['DUEAMOUNT','OUTSTANDINGP'], rows:[
        {RANK_NO:1,DUEAMOUNT:'574528',OUTSTANDINGP:'448688'},
        {RANK_NO:2,DUEAMOUNT:'900000',OUTSTANDINGP:'247360'}]};
    assert.deepEqual(chart.available(report), ['DUEAMOUNT','OUTSTANDINGP']);
    assert.equal(chart.series(report,'DUEAMOUNT')[0].exact,'574528');
    assert.equal(chart.series(report,'DUEAMOUNT')[1].value,900000);
    assert.equal(chart.series(report,'DUEAMOUNT')[0].rank,1);
});
test('missing values are not zero and invalid numbers cannot create bars', () => {
    for (const value of [null, undefined, '', ' ', true, {}, 'NaN', Infinity, -1, '1e999']) assert.equal(chart.numeric(value),null);
    assert.equal(chart.numeric('0'),0);
    assert.equal(chart.numeric('2749.25'),2749.25);
    const report={columns:['DUEAMOUNT'],rows:[{DUEAMOUNT:null},{DUEAMOUNT:'0'}]};
    assert.equal(chart.series(report,'DUEAMOUNT')[0].value,null);
});
test('bounds charts to twenty displayed rows without aggregating or inventing a time series', () => {
    const report={columns:['DUEAMOUNT'],rows:Array.from({length:100}, (_,i)=>({DUEAMOUNT:String(i)}))};
    assert.equal(chart.series(report,'DUEAMOUNT').length,20);
    assert.deepEqual(chart.series(report,'PHONENO'),[]);
});
