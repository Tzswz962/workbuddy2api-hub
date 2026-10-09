const assert = require('assert');
const { dashboardHtml } = require('./_dashboard_source.js');

const html = dashboardHtml();
const start = html.indexOf('async function copyLoginLink(){');
const end = html.indexOf('async function pollLogin(){', start);
assert.ok(start > 0 && end > start);
const source = html.slice(start, end);
assert.ok(html.includes('onclick="copyLoginLink()">复制登录链接</button>'));
assert.ok(html.includes("'复制登录链接': 'Copy sign-in link'"));

(async () => {
  let url = 'https://example.com/auth?state=first&redirect=%2Flogin';
  let link = { getAttribute: name => { assert.strictEqual(name, 'href'); return url; } };
  let failure = null;
  const copied = [], toasts = [];
  const copy = new Function('document', 'writeClipboard', 'toast',
    source + '\nreturn copyLoginLink;')(
    { getElementById: id => { assert.strictEqual(id, 'loginLink'); return link; } },
    async value => { if (failure) throw failure; copied.push(value); },
    (message, kind) => toasts.push({ message, kind })
  );

  await copy();
  assert.deepStrictEqual(copied, [url]);
  assert.deepStrictEqual(toasts.pop(), { message: '已复制到剪贴板', kind: 'ok' });

  // A regenerated link must replace the value copied on the next click.
  url = 'https://example.com/auth?state=second&redirect=%2Flogin';
  await copy();
  assert.strictEqual(copied[1], url);
  toasts.length = 0;

  failure = new Error('clipboard denied');
  await copy();
  assert.strictEqual(copied.length, 2);
  assert.deepStrictEqual(toasts.pop(), { message: '复制失败: clipboard denied', kind: 'bad' });

  link = null;
  await copy();
  assert.strictEqual(copied.length, 2);
  assert.strictEqual(toasts.length, 0);
  console.log('login link copy assertions passed');
})().catch(err => { console.error(err); process.exit(1); });
