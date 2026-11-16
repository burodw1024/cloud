const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const esbuild = require('esbuild');
const { JSDOM } = require('jsdom');

const root = path.resolve(__dirname, '..');
const output = fs.mkdtempSync(path.join(root, '.test-'));
let passed = 0;
const check = (name, test) => { test(); passed += 1; console.log('PASS', name); };
function bundle(name, options = {}) {
  const target = path.join(output, name + '.cjs');
  esbuild.buildSync({entryPoints: [path.join(root, 'src', name)], outfile: target, bundle: true,
    jsx: 'automatic', platform: 'node', format: 'cjs', loader: {'.css': 'empty'}, logLevel: 'silent', ...options});
  return require(target);
}

try {
  const model = bundle('model.ts');
  const options = {latitudeColumn: 'lat', longitudeColumn: 'lon', labelColumn: 'label', categoryColumn: 'category',
    sizeColumn: 'amount', iconColumn: 'icon', tooltipColumns: ['detail'], rowLimit: 1000};
  const rows = [{lat: 23.8103, lon: 90.4125, label: 'Demo Dhaka', category: 'A', amount: 100, icon: 'branch', detail: 'Synthetic'},
    {lat: 22.3569, lon: 91.7832, label: 'Demo Chattogram', category: 'B', amount: -25, icon: 'warning', detail: 'Synthetic'}];

  check('strict coordinates retain zeros and reject malformed/unsupported values', () => {
    const result = model.prepareMap([{lat: 0, lon: 0}, {lat: '', lon: 0}, {lat: null, lon: 1},
      {lat: false, lon: 1}, {lat: '23abc', lon: 90}, {lat: 90, lon: 90},
      {lat: 23, lon: 181}, {lat: '2.38103e1', lon: '90.4125'}], options);
    assert.equal(result.points.length, 2);
    assert.equal(result.invalidRows, 6);
    assert.equal(result.points[0].latitude, 0);
  });
  check('size uses absolute values while preserving signs, labels, and order', () => {
    const result = model.prepareMap(rows, options);
    assert.equal(result.points[1].value, -25);
    assert(result.points[0].size > result.points[1].size);
    assert.deepEqual(result.points.map(point => point.label), rows.map(row => row.label));
    assert(result.points[1].details.some(([key, value]) => key === 'amount' && value === '-25'));
  });
  check('icons and colors cannot inject markup or remote resources', () => {
    const result = model.prepareMap([{lat: 1, lon: 2, icon: '<img src=x onerror=alert(1)>'}],
      {...options, categoryColumn: undefined, fixedColor: 'url(https://bad.example)'});
    assert.equal(result.points[0].icon, 'pin');
    assert.equal(result.points[0].color, '#0072b2');
  });
  check('point cap and invalid/omitted counts are explicit', () => {
    const result = model.prepareMap(Array.from({length: 6001}, () => ({lat: 1, lon: 2})),
      {...options, rowLimit: 999999});
    assert.equal(result.points.length, 5000);
    assert.equal(result.omittedRows, 1001);
    assert.equal(result.atLimit, true);
  });
  check('date-line coordinates use the smallest longitude span', () => {
    const result = model.unwrapLongitudes([179, -179]);
    assert.equal(Math.max(...result) - Math.min(...result), 2);
    const second = model.unwrapLongitudes([-100, -20, 100]);
    assert(Math.max(...second) - Math.min(...second) <= 200);
  });
  check('tile URLs are explicit HTTP(S)/same-origin templates only', () => {
    assert.equal(model.tileUrl(''), '');
    assert.equal(model.tileUrl('javascript:alert(1)'), '');
    assert.equal(model.tileUrl('https://user:pass@tiles.example/{z}/{x}/{y}.png'), '');
    assert.equal(model.tileUrl('//external.example/{z}/{x}/{y}.png'), '');
    assert.equal(model.tileUrl('https://tiles.example/no-coordinates.png'), '');
    assert.equal(model.tileUrl('/tiles/{z}/{x}/{y}.png'), '/tiles/{z}/{x}/{y}.png');
  });
  check('only configured columns are selected, tooltips bounded and deduplicated', () => {
    const columns = model.selectedColumns({...options, tooltipColumns: ['lat', ...Array.from({length: 20}, (_, i) => 'field'+i)]});
    assert.equal(columns.filter(column => column === 'lat').length, 1);
    assert(!columns.includes('field19'));
    assert.deepEqual(model.selectedColumns({latitudeColumn: 'lat', longitudeColumn: 'lon', tooltipColumns: 'bad'}),
      ['lat', 'lon']);
  });

  const queryFile = path.join(output, 'buildQuery.cjs');
  const querySource = fs.readFileSync(path.join(root, 'src/buildQuery.ts'), 'utf8')
    .replace("import { buildQueryContext } from '@superset-ui/core';",
      'const buildQueryContext = (formData: unknown, callback: Function) => callback({filters: ["preserved"], time_range: "preserved"});');
  esbuild.buildSync({stdin: {contents: querySource, resolveDir: path.join(root, 'src'), loader: 'ts'},
    outfile: queryFile, bundle: true, jsx: 'automatic', platform: 'node', format: 'cjs', logLevel: 'silent'});
  const query = require(queryFile).default;
  check('query keeps Superset filter context, caps rows, and requires explicit coordinates', () => {
    const result = query({latitude_column: 'lat', longitude_column: 'lon', label_column: 'label', row_limit: 999999})[0];
    assert.deepEqual(result.columns, ['lat', 'lon', 'label']);
    assert.deepEqual(result.filters, ['preserved']);
    assert.deepEqual(result.metrics, []);
    assert.equal(result.row_limit, 5000);
    assert.throws(() => query({latitude_column: '', longitude_column: 'lon'}), /Choose latitude/);
  });

  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>',
    {url: 'https://superset.test', pretendToBeVisual: true});
  Object.assign(global, {window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement,
    Element: dom.window.Element, SVGElement: dom.window.SVGElement, Node: dom.window.Node,
    requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
    cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window)});
  Object.defineProperty(global, 'navigator', {value: dom.window.navigator, configurable: true});
  Object.defineProperty(dom.window.HTMLElement.prototype, 'clientWidth', {get() { return 800; }});
  Object.defineProperty(dom.window.HTMLElement.prototype, 'clientHeight', {get() { return 400; }});
  // React 16's scheduler otherwise keeps Node 24's MessageChannel alive after tests.
  global.MessageChannel = undefined;
  const React = require('react');
  const ReactDOM = require('react-dom');
  const { act } = require('react-dom/test-utils');
  require.extensions['.css'] = () => {};
  const Component = bundle('IconMap.tsx', {external: ['react', 'react-dom', 'leaflet']}).default;
  const mount = document.getElementById('root');
  const hostile = [{...rows[0], label: '<img src=x onerror=alert(1)>', detail: '<svg onload=alert(1)>'}, rows[1]];
  check('offline map renders markers without fetching image tiles', () => {
    act(() => { ReactDOM.render(React.createElement(Component, {...options, width: 800, height: 500, rows: hostile}), mount); });
    assert.equal(mount.querySelectorAll('.leaflet-marker-icon').length, 2);
    assert.equal(mount.querySelectorAll('img').length, 0);
    assert.match(mount.textContent, /Offline coordinate grid/);
  });
  check('tooltips render untrusted labels and values as text', () => {
    act(() => { mount.querySelector('.leaflet-marker-icon').dispatchEvent(new dom.window.MouseEvent('click', {bubbles: true})); });
    const popup = mount.querySelector('.buro-map-popup');
    assert(popup);
    assert.match(popup.textContent, /<img src=x onerror/);
    assert.equal(popup.querySelectorAll('img,svg,script').length, 0);
  });
  check('legend filtering changes local markers and reports visible count', () => {
    act(() => { mount.querySelector('.buro-map-legend button').click(); });
    assert.equal(mount.querySelectorAll('.leaflet-marker-icon').length, 1);
    assert.match(mount.textContent, /1 plotted locations/);
  });
  check('empty data and unmount clean up markers and map instance', () => {
    act(() => { ReactDOM.render(React.createElement(Component, {...options, width: 800, height: 500, rows: []}), mount); });
    assert.equal(mount.querySelectorAll('.leaflet-marker-icon').length, 0);
    assert.match(mount.textContent, /No valid coordinates/);
    act(() => { ReactDOM.unmountComponentAtNode(mount); });
    assert.equal(mount.childElementCount, 0);
  });
  dom.window.close();
  console.log(passed + ' Icon Map checks passed.');
} finally {
  fs.rmSync(output, {recursive: true, force: true});
}
