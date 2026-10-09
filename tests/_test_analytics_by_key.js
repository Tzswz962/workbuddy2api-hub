/* Drive the dashboard's per-API-key table in Node.
 *
 * The key table is the only place where the new axis meets the browser, and
 * the things that break silently there are (a) a <td> that loses its
 * data-label, which makes the row unreadable on a phone because the header
 * row is hidden at that breakpoint, (b) a key name that reaches innerHTML
 * unescaped - names are free text typed into the panel - and (c) a per-key
 * label that drifts onto another key's row, which a table-wide substring check
 * cannot see because the string is still somewhere in the table. None of those
 * shows up in a Python test, so this drives the real functions out of
 * dashboard.html.
 *
 * Requires node (no other dependency); the rest of the suite is Python only.
 *
 *   node _test_analytics_by_key.js
 */
const {dashboardScript} = require('./_dashboard_source.js');
const code = dashboardScript();

// One shared fake DOM for every dashboard suite: tests/_dom_stub.js.
const dom = require('./_dom_stub.js');
dom.installDom({
  fetch: () => Promise.resolve({ ok:true, status:200, json: () => Promise.resolve({}) }),
});

let api;
try {
  api = new Function(code + `
    ; return { renderKeyTable, keyRealmCell, keyModelPills };`)();
} catch (e) {
  console.log('LOAD ERROR:', e.message);
  process.exit(1);
}

const stat = (o) => Object.assign({
  requests: 0, errors: 0, prompt_tokens: 0, completion_tokens: 0, reasoning_tokens: 0,
  cached_tokens: 0, total_tokens: 0, credit: 0, cache_hit_pct: 0,
}, o);
const key = (o) => Object.assign({
  key: '', name: '', source: 'panel', enabled: true, realm: '', cross_realm: false,
  window: stat({}), all_time: stat({}), models: [], models_other: null,
}, o);

const keys = [
  key({ key: 'k1', name: '甲 · 生产', realm: 'cn',
        window: stat({ requests: 12, errors: 1, total_tokens: 3456, prompt_tokens: 2000,
                       completion_tokens: 1000, reasoning_tokens: 456, cache_hit_pct: 12.5, credit: 3.5 }),
        models: [{ model: 'glm-5.3', requests: 10, tokens: 3000, reasoning: 400 },
                 { model: 'kimi-k2', requests: 2, tokens: 456, reasoning: 56 }],
        models_other: { model: '(其他)', other: true, models: 2, requests: 5, tokens: 900, reasoning: 0 } }),
  key({ key: 'k2', name: '乙', enabled: false, realm: '', cross_realm: true,
        window: stat({ requests: 3, total_tokens: 100 }) }),
  key({ key: 'launcher', name: '启动参数', source: 'launcher', realm: 'intl',
        window: stat({ requests: 2, total_tokens: 50 }) }),
  key({ key: '__before_keys__', name: '(切换前)', source: 'bucket',
        window: stat({ requests: 9, total_tokens: 9999, credit: 12 }) }),
  key({ key: '__no_key__', name: '(无 key)', source: 'bucket', window: stat({ requests: 1 }) }),
  key({ key: 'evil', name: '<img src=x onerror=alert(1)>', realm: '',
        models: [{ model: '<b>bold</b>', requests: 1, tokens: 1 }] }),
];

let pass = 0, fail = 0;
const check = (label, cond, extra) => { if (cond) { pass++; console.log('  [PASS] ' + label); } else { fail++; console.log('  [FAIL] ' + label + (extra ? '  ' + extra : '')); } };

console.log('[1] the table renders one row per key');
api.renderKeyTable({ keys: keys });
let out = document.getElementById('analyticsKeyTbody').innerHTML;
check('six keys, six rows', (out.match(/<tr>/g) || []).length === 6, (out.match(/<tr>/g) || []).length);
check('the row count is announced next to the title',
      document.getElementById('analyticsKeyCount').textContent === '(6 把)',
      document.getElementById('analyticsKeyCount').textContent);
check('every cell carries a data-label (the phone layout depends on it)',
      (out.match(/<td/g) || []).length === (out.match(/data-label=/g) || []).length,
      (out.match(/<td/g) || []).length + ' vs ' + (out.match(/data-label=/g) || []).length);
check('a named key shows its name', out.includes('甲 · 生产'));

// --- row-bound helpers -------------------------------------------------
// The rendered table is the contract: "the CN key is labelled 国内版" only
// means something if that badge sits on the CN key's row. These helpers are
// deliberately tiny - split the rows, read one labelled cell out of one row -
// and they match on data-label and text only, never on classes, styles or
// attribute order, so a markup-only change stays tolerated.
const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const rowsOf = (html) => html.split(/<tr[^>]*>/).slice(1).map(r => r.split(/<\/tr>/)[0]);
const cellOf = (row, label) => {
  const m = row.match(new RegExp('<td[^>]*data-label="' + escapeRe(label) + '"[^>]*>([\\s\\S]*?)</td>'));
  return m ? m[1] : null;
};
const rowFor = (html, name) => {
  const hits = rowsOf(html).filter(r => (cellOf(r, 'API Key') || '').includes(name));
  return hits.length === 1 ? hits[0] : null;
};
const show = (row, label) => row === null ? '(no single row)' : JSON.stringify(cellOf(row, label));

console.log();
console.log('[1b] every per-key label is bound to the row it describes');
// k2 is both cross-exit and disabled, which is why one row answers two of
// these; the point is that each answer is read from that row and no other.
const cnRow = rowFor(out, '甲 · 生产');
const crossRow = rowFor(out, '乙');
const intlRow = rowFor(out, '启动参数');
const followRow = rowFor(out, '&lt;img');
const beforeRow = rowFor(out, '(切换前)');
const noKeyRow = rowFor(out, '(无 key)');

check('the cn-bound key is labelled 国内版 on its own row',
      cnRow !== null && cellOf(cnRow, '出口').includes('国内版'),
      show(cnRow, '出口'));
check('the intl-bound key is labelled 国际版 on its own row',
      intlRow !== null && cellOf(intlRow, '出口').includes('国际版'),
      show(intlRow, '出口'));
check('the key that used both exits is flagged 跟随 · 混合 on its own row',
      crossRow !== null && cellOf(crossRow, '出口').includes('跟随 · 混合'),
      show(crossRow, '出口'));
check('a key that only follows the model says 跟随 and not 混合, on its own row',
      followRow !== null && cellOf(followRow, '出口').includes('跟随')
      && !cellOf(followRow, '出口').includes('混合'),
      show(followRow, '出口'));
check('the disabled marker sits on the disabled key row',
      crossRow !== null && cellOf(crossRow, 'API Key').includes('已禁用'),
      show(crossRow, 'API Key'));
check('no other key row carries the disabled marker',
      rowsOf(out).filter(r => (cellOf(r, 'API Key') || '').includes('已禁用')).length === 1,
      rowsOf(out).filter(r => (cellOf(r, 'API Key') || '').includes('已禁用')).length + ' row(s)');
check('the pre-upgrade bucket row shows no exit instead of guessing one',
      beforeRow !== null && cellOf(beforeRow, '出口').includes('—'),
      show(beforeRow, '出口'));
check('the no-key bucket row shows no exit either',
      noKeyRow !== null && cellOf(noKeyRow, '出口').includes('—'),
      show(noKeyRow, '出口'));
check('the launcher key is attributed to the start-up argument', out.includes('启动参数'));
check('panel keys are marked as panel keys', out.includes('面板 Key'));
check('failures are surfaced next to the request count', out.includes('失败 1'));
check('the credit column is fixed to two decimals', out.includes('3.50'));
check('cache hit is rendered as a percentage', out.includes('12.5%'));

console.log();
console.log('[2] the unattributed rows stay distinguishable');
check('(切换前) is rendered', out.includes('(切换前)'));
check('(无 key) is rendered separately', out.includes('(无 key)'));
check('bucket rows show no exit instead of guessing one',
      (out.match(/—<\/span><\/td>/g) || []).length === 2,
      (out.match(/—<\/span><\/td>/g) || []).length);
// "a bucket row is never badged as disabled" used to live here as a
// (切换前)-then-within-400-chars regex. [1b] now states it as a property of the
// rows themselves, which is both stronger and not dependent on a magic width.

console.log();
console.log('[3] model pills');
check('each model gets a pill', out.includes('glm-5.3') && out.includes('kimi-k2'));
check('the overflow pill reports how many models it covers', out.includes('2 个模型'));
check('a key with no calls says so rather than rendering nothing',
      api.keyModelPills(key({ models: [], models_other: null })).includes('无调用'));

console.log();
console.log('[4] free text from the settings page cannot become markup');
check('a key name is escaped', !out.includes('<img src=x') && out.includes('&lt;img'));
check('a model name is escaped', !out.includes('<b>bold</b>') && out.includes('&lt;b&gt;'));

console.log();
console.log('[5] the footer explains what the reader cannot infer');
const note = document.getElementById('analyticsKeyNote').innerHTML;
check('the cross-exit count is called out', note.includes('1 把 Key 没有绑定出口'), note);
check('the upgrade cut-off is stated once', note.includes('(切换前)'));
api.renderKeyTable({ keys: [key({ name: 'x', realm: 'cn' })] });
check('the footer does not cry wolf about cross-exit keys when there are none',
      !document.getElementById('analyticsKeyNote').innerHTML.includes('没有绑定出口'));

console.log();
console.log('[6] empty and degraded payloads');
api.renderKeyTable({ keys: [] });
out = document.getElementById('analyticsKeyTbody').innerHTML;
check('an empty axis renders one full-width empty cell', out.includes('colspan="8"'), out);
check('the empty state does not claim a count',
      document.getElementById('analyticsKeyCount').textContent === '');
check('a panel with no keys is told so', document.getElementById('analyticsKeyNote').innerHTML.includes('还没有任何 API Key'));
api.renderKeyTable({});
check('a payload without the axis at all does not throw', document.getElementById('analyticsKeyTbody').innerHTML.includes('colspan="8"'));

console.log();
console.log('PASS=' + pass + ' FAIL=' + fail);
process.exit(fail ? 1 : 0);
