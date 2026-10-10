/* 剩余用量估算区块（数据看板 → /usage/remaining）。
 *
 * 这个区块把「账号 × 模型」在上游 24 小时窗口里的剩余量画出来：预算来自撞线
 * 账号的样本，冷却中的组合剩余记 0 并给出恢复时刻，没有撞线记录的组合标
 * 「按 24h 估算」。套件钉住三件事：
 *
 *   1. 区块与表头真的在页面里（id 与列名，区块由侧栏导航现场生成，名字不能漂）；
 *   2. renderRemainingUsage() 对每种行都给出正确的数字与徽章：正常行、冷却行
 *      （剩余 0 + 恢复时刻）、估算行、没有预算的行（剩余显示 —）；
 *   3. loadRemainingUsage() 打的是 /usage/remaining，并把载荷交给渲染函数。
 *
 * Run with Node: node tests/_test_remaining_usage.js
 */
'use strict';

const assert = require('assert');
const {dashboardHtml, dashboardScript} = require('./_dashboard_source.js');

// One shared fake DOM for every dashboard suite: tests/_dom_stub.js.
const dom = require('./_dom_stub.js');

let checks = 0;
const check = (label, cond, extra) => {
  checks += 1;
  assert.ok(cond, label + (extra ? '  [' + extra + ']' : ''));
};

// ---- 1. 页面里真的有这个区块与表头 -----------------------------------------
const html = dashboardHtml();
check('区块容器在页面里', html.includes('id="remainingUsageSection"'));
check('表体在页面里', html.includes('id="remainingUsageTbody"'));
check('预算条容器在页面里', html.includes('id="remainingBudgets"'));
for (const th of ['窗口起点', '窗口内已用', '估算剩余', '用量占比']) {
  check('表头「' + th + '」在页面里', html.includes('>' + th + '</th>'));
}
check('说明文字在页面里', html.includes('预算 = 撞线账号在窗口内用量的平均值'));

// ---- 2. 渲染每种行 ---------------------------------------------------------
const requested = [];
dom.installDom({
  fetch: (url) => {
    requested.push(url);
    const payload = {window_seconds: 86400, generated_at: 1, budgets: [], rows: []};
    return Promise.resolve({
      status: 200, ok: true,
      json: () => Promise.resolve(payload),
      text: () => Promise.resolve(JSON.stringify(payload)),
    });
  },
});

const script = dashboardScript();
const api = new Function(script + `
  return { renderRemainingUsage, loadRemainingUsage };`)();

const tbody = document.getElementById('remainingUsageTbody');
const budgetsEl = document.getElementById('remainingBudgets');
const summaryEl = document.getElementById('remainingUsageSummary');

const row = (over) => Object.assign({
  uid: 'uid-1', nickname: 'meyadi', realm: 'intl', model: 'deepseek-v4.1-flash',
  used: 50000000, budget: 200000000, budget_min: 190000000, budget_max: 200000000,
  samples: 4, remaining: 150000000, used_pct: 25.0,
  window_start: 1, window_iso: '2026-10-09 23:05:35',
  reset_at: null, reset_iso: null, cooling: false, estimated: false,
}, over);

api.renderRemainingUsage({
  window_seconds: 86400,
  budgets: [{realm: 'intl', model: 'deepseek-v4.1-flash', avg: 200000000,
             min: 190000000, max: 200000000, n: 4}],
  rows: [
    row({}),
    row({uid: 'uid-2', nickname: 'm1328640', used: 200000000, remaining: 0,
         used_pct: 100.0, cooling: true, reset_iso: '2026-10-10 15:18:17'}),
    row({uid: 'uid-3', nickname: 'zh990859', used: 80000000, remaining: 120000000,
         used_pct: 40.0, estimated: true}),
    row({uid: 'uid-4', nickname: '183020947', model: 'glm-5.3', budget: null,
         remaining: null, used_pct: null, used: 42, estimated: true}),
  ],
});

const out = tbody.innerHTML;
check('正常行画出昵称与模型', out.includes('meyadi') && out.includes('deepseek-v4.1-flash'), out);
check('正常行剩余 150M', out.includes('150.0M'), out);
check('正常行已用 50M', out.includes('50.0M'), out);
check('正常行占比 25.0%', out.includes('25.0%'), out);
check('正常行窗口起点显示重置时刻', out.includes('10-09 23:05'), out);
check('冷却行剩余记 0', /冷却至[\s\S]{0,40}10-10 15:18/.test(out), out);
check('冷却行的百分比是 100.0%', out.includes('100.0%'), out);
check('估算行带「按 24h 估算」徽章', out.includes('按 24h 估算'), out);
check('估算行有下界说明的悬停提示', out.includes('是剩余量的下界'), out);
check('没有预算的行剩余显示 —', /<td style="text-align:right">—<\/td>/.test(out), out);
check('没有预算的行不画徽章冲突', out.includes('glm-5.3'), out);

// 预算条：区域 · 模型 · 预算 ≈ 均值 (n=样本数)，悬停给出样本区间
check('预算条列出模型与预算', budgetsEl.innerHTML.includes('deepseek-v4.1-flash')
      && budgetsEl.innerHTML.includes('200.0M'), budgetsEl.innerHTML);
check('预算条标注样本数', budgetsEl.innerHTML.includes('(n=4)'), budgetsEl.innerHTML);
check('预算条悬停给出样本区间',
      budgetsEl.innerHTML.includes('190.0M') && budgetsEl.innerHTML.includes('200.0M'),
      budgetsEl.innerHTML);
check('汇总行统计冷却与 ≥80%', summaryEl.textContent.includes('冷却中 1')
      && summaryEl.textContent.includes('4 项'), summaryEl.textContent);

// 空载荷：不是白板，给一句能看懂的空状态
api.renderRemainingUsage({window_seconds: 86400, budgets: [], rows: []});
check('空载荷给出空状态', tbody.innerHTML.includes('暂无数据'), tbody.innerHTML);
check('空载荷时预算条说明来源',
      budgetsEl.innerHTML.includes('暂无预算样本'), budgetsEl.innerHTML);

// ---- 3. loadRemainingUsage() 打的就是这个接口 ------------------------------
requested.length = 0;
api.loadRemainingUsage().then(() => {
  check('loadRemainingUsage 请求 /usage/remaining',
        requested.some(u => String(u).includes('/usage/remaining')), requested.join(','));
  console.log('remaining usage panel assertions passed (' + checks + ' checks)');
}).catch(err => {
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
});
