const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

async function test(healthy) {
  let offline = false;
  const bar = {style: {}, setAttribute() {}, innerHTML: '', textContent: ''};
  const doc = {head: {appendChild() {}}, body: {firstChild: null, insertBefore() {}, classList: {toggle(_, v) { offline = v; }}},
    createElement(name) {return name === 'style' ? {} : bar;}, addEventListener() {}};
  const fetch = async (url) => url === '/api/version' ? (healthy ? new Response('{}') : Promise.reject(Error('unreachable'))) :
    new Response('{}', {headers: {'x-nav-from-cache':'1', 'x-nav-cached-at':'123'}});
  const window = {fetch, addEventListener() {}};
  const context = {window, document:doc, navigator:{}, localStorage:{getItem(){return null;}, setItem(){}},
    Response, Date, Promise, setTimeout(){return 1;}, setInterval(){return 1;}, clearInterval(){}};
  vm.runInNewContext(fs.readFileSync('ccc-navigator/static/offline.js','utf8'), context);
  await window.fetch('/api/tree');
  for (let i=0; i<8; i++) await Promise.resolve();
  assert.equal(offline, !healthy, healthy ? 'slow data should not imply offline' : 'unreachable service should show offline');
}
(async () => { await test(true); await test(false); })().catch(e => {console.error(e);process.exit(1);});
