/* Charts use displayed report values only. No inference, aggregation or tool call. */
(function(root) {
    'use strict';
    const metrics = {
        DUEAMOUNT: 'পরিশোধযোগ্য বকেয়া', OUTSTANDINGP: 'অপরিশোধিত মূলধন',
        OUTSTANDING_PI: 'সুদসহ অপরিশোধিত স্থিতি', 'Outstanding(P+I)': 'সুদসহ অপরিশোধিত স্থিতি',
        DUE_DAYS: 'বকেয়ার দিন', LOAN_COUNT: 'ঋণের সংখ্যা'
    };
    function available(report) {
        return Object.keys(metrics).filter(key => (report?.columns || []).includes(key) &&
            (report?.rows || []).some(row => numeric(row[key]) !== null));
    }
    function numeric(value) {
        if (typeof value !== 'number' && typeof value !== 'string') return null;
        if (typeof value === 'string' && !/^\d+(?:\.\d+)?$/.test(value)) return null;
        const n = Number(value);
        return Number.isFinite(n) && n >= 0 ? n : null;
    }
    function series(report, key) {
        if (!available(report).includes(key)) return [];
        return report.rows.slice(0, 20).map((row, i) => ({
            label: 'সারি ' + (i + 1), value: numeric(row[key]),
            exact: row[key] == null ? 'তথ্য নেই' : String(row[key]),
            rank: row.RANK ?? row.RANK_NO ?? null
        }));
    }
    const api = {metrics, available, numeric, series};
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    else root.BuroReportChart = api;
})(typeof window !== 'undefined' ? window : globalThis);
