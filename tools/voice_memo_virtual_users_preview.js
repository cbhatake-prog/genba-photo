const fs = require('fs');
const path = require('path');

const repoTemplatePath = path.resolve(__dirname, '..', 'templates', 'voice_memo.html');
const localTemplatePath = path.resolve(__dirname, '..', 'voice_memo.html');
const htmlPath = fs.existsSync(repoTemplatePath) ? repoTemplatePath : localTemplatePath;
const html = fs.readFileSync(htmlPath, 'utf8');

function extractConst(src, name) {
  const re = new RegExp(`const\\s+${name}\\s*=\\s*([^;]+);`);
  const match = src.match(re);
  if (!match) throw new Error(`Missing const ${name}`);
  return `const ${name} = ${match[1]};`;
}

function extractFunction(src, name) {
  const start = src.indexOf(`function ${name}`);
  if (start < 0) throw new Error(`Missing function ${name}`);
  const brace = src.indexOf('{', start);
  let depth = 0;
  for (let i = brace; i < src.length; i++) {
    if (src[i] === '{') depth++;
    if (src[i] === '}') {
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error(`Unclosed function ${name}`);
}

const harness = new Function(`
${extractConst(html, 'STOPWORDS')}
${extractConst(html, 'ITEM_NOISE_WORDS')}
let currentType = 'wallpaper';
${extractFunction(html, 'normalize')}
${extractFunction(html, 'normalizeBareSizeForType')}
${extractFunction(html, 'splitFusedSizeCountSafe')}
${extractFunction(html, 'splitCompactFusedItemsSafe')}
${extractFunction(html, 'parseSpeechItemsSafe')}
return { normalize, splitFusedSizeCountSafe, parseSpeechItemsSafe };
`)();

function asItem(size, count) {
  return { size, count };
}

function sameItems(actual, expected) {
  return JSON.stringify(actual) === JSON.stringify(expected);
}

function totalM(items) {
  return items.reduce((sum, it) => sum + (it.size * it.count) / 1000, 0);
}

const sizes = [1, 2, 9, 38, 99, 100, 180, 250, 380, 420, 530, 999, 1000, 1200, 2400, 3200, 3400, 5300, 10000, 100000];
const counts = [1, 2, 3, 4, 5, 6, 10, 12, 20, 50, 100, 120, 999];
const counterWords = ['本', '枚', '個'];
const noise = ['えっと', '次', 'それから', 'あと', 'お願いします', 'まず', 'そして'];

function normalCase(i) {
  const s1 = sizes[i % sizes.length];
  const c1 = counts[(i * 3) % counts.length];
  const s2 = sizes[(i * 7 + 3) % sizes.length];
  const c2 = counts[(i * 5 + 2) % counts.length];
  const cw1 = counterWords[i % counterWords.length];
  const cw2 = counterWords[(i + 1) % counterWords.length];
  const utterance = `${noise[i % noise.length]} ${s1} ${c1}${cw1} ${noise[(i + 2) % noise.length]} ${s2} ${c2}${cw2}`;
  return { id: `normal-${i}`, type: '通常読み', utterance, expected: [asItem(s1, c1), asItem(s2, c2)] };
}

function spacedCounterCase(i) {
  const s = sizes[(i * 11 + 5) % sizes.length];
  const c = counts[(i * 4 + 1) % counts.length];
  const utterance = `${s} ${c} 本`;
  return { id: `spaced-counter-${i}`, type: '空白あり', utterance, expected: [asItem(s, c)] };
}

function fusedCase(i) {
  const fusedPairs = [
    [3200, 3, '32003本'],
    [380, 4, '3804本'],
    [5300, 3, '53003本'],
    [420, 1, '4201本'],
    [100000, 1, '1000001本'],
    [100, 120, '100120本'],
    [99, 100, '99100本'],
    [3200, 12, '320012本'],
    [5300, 20, '530020本'],
    [1200, 5, '12005本'],
  ];
  const [size, count, utterance] = fusedPairs[i % fusedPairs.length];
  return { id: `fused-${i}`, type: '数字結合', utterance, expected: [asItem(size, count)] };
}

const fixedCases = [
  { id: 'kana-3200-4', type: 'かな読み', utterance: 'さんぜんにひゃく 4本', expected: [asItem(3200, 4)] },
  { id: 'kana-5300-3', type: 'かな読み', utterance: 'ごせんさんびゃく 3本', expected: [asItem(5300, 3)] },
  { id: 'kana-fused-3200-4', type: '高速かな結合', utterance: 'さんぜんにひゃくよんほん', expected: [asItem(3200, 4)] },
  { id: 'kana-fused-5300-3', type: '高速かな結合', utterance: 'ごせんさんびゃくさんぼん', expected: [asItem(5300, 3)] },
  { id: 'kana-fused-380-4', type: '高速かな結合', utterance: 'さんびゃくはちじゅうよんほん', expected: [asItem(380, 4)] },
  { id: 'kana-fused-38-4', type: '高速かな結合', utterance: 'さんじゅうはちよんほん', expected: [asItem(38, 4)] },
  { id: 'digit-kana-count-38-4', type: '数字+かな本数', utterance: '38よんほん', expected: [asItem(38, 4)] },
  { id: 'digit-kana-count-3200-12', type: '数字+かな本数', utterance: '3200じゅうにほん', expected: [asItem(3200, 12)] },
  { id: 'fast-kana-two-items', type: '高速2件連続', utterance: 'さんぜんにひゃくよんほんごせんさんびゃくさんぼん', expected: [asItem(3200, 4), asItem(5300, 3)] },
  { id: 'kanji-3200-4', type: '漢字', utterance: '三千二百 4本', expected: [asItem(3200, 4)] },
  { id: 'fullwidth', type: '全角数字', utterance: '５３００ ３本 ３２００ １本', expected: [asItem(5300, 3), asItem(3200, 1)] },
  { id: 'comma', type: '読点区切り', utterance: '5300、3本、3200、1本', expected: [asItem(5300, 3), asItem(3200, 1)] },
  { id: 'mixed-fused-normal', type: '混在', utterance: '53003本 3200 1本 4201本', expected: [asItem(5300, 3), asItem(3200, 1), asItem(420, 1)] },
  { id: 'small-mm-valid', type: '小寸法', utterance: '1 2本 2 3本 9 4本', expected: [asItem(1, 2), asItem(2, 3), asItem(9, 4)] },
  { id: 'max-mm-valid', type: '最大寸法', utterance: '100000 1本', expected: [asItem(100000, 1)] },
  { id: 'large-count', type: '100本超', utterance: '99 999本 100 120本', expected: [asItem(99, 999), asItem(100, 120)] },
  { id: 'voice-compact-384', type: '実音声結合', utterance: '384本', expected: [asItem(380, 4)] },
];

const cases = [...fixedCases];
for (let i = 0; cases.length < 100; i++) {
  if (i % 3 === 0) cases.push(normalCase(i));
  else if (i % 3 === 1) cases.push(spacedCounterCase(i));
  else cases.push(fusedCase(i));
}

const results = cases.map((test, idx) => {
  const parsed = harness.parseSpeechItemsSafe(test.utterance);
  const normalized = harness.normalize(test.utterance);
  const ok = sameItems(parsed.items, test.expected);
  return {
    no: idx + 1,
    id: test.id,
    type: test.type,
    utterance: test.utterance,
    normalized,
    expected: test.expected,
    actual: parsed.items,
    pendingSize: parsed.pendingSize,
    ok,
    expectedTotalM: totalM(test.expected),
    actualTotalM: totalM(parsed.items),
  };
});

const passed = results.filter(r => r.ok).length;
const failed = results.length - passed;

function esc(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function itemsLabel(items) {
  if (!items.length) return '登録なし';
  return items.map(it => `${it.size} × ${it.count}`).join(' / ');
}

function fmt(n) {
  if (Math.abs(n - Math.round(n)) < 0.001) return Math.round(n).toLocaleString('ja-JP');
  return n.toLocaleString('ja-JP', { maximumFractionDigits: 2 });
}

const rows = results.map(r => `
  <article class="case ${r.ok ? 'pass' : 'fail'}" data-index="${r.no - 1}">
    <div class="case-head">
      <span class="no">#${r.no}</span>
      <span class="badge">${esc(r.type)}</span>
      <span class="state">${r.ok ? 'OK' : 'NG'}</span>
    </div>
    <div class="utter">${esc(r.utterance)}</div>
    <div class="grid">
      <div><b>正規化</b><span>${esc(r.normalized)}</span></div>
      <div><b>期待</b><span>${esc(itemsLabel(r.expected))}</span></div>
      <div><b>実際</b><span>${esc(itemsLabel(r.actual))}${r.pendingSize !== null ? ` / 保留 ${esc(r.pendingSize)}` : ''}</span></div>
      <div><b>合計</b><span>${fmt(r.actualTotalM)} m</span></div>
    </div>
  </article>
`).join('');

const preview = `<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>音声採寸メモ 100人仮想ユーザーデバッグ</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #17181b;
    --panel: #252a30;
    --panel2: #1f2328;
    --line: #3b4652;
    --text: #f2f0ee;
    --muted: #a8b0b8;
    --ok: #7E8B6C;
    --warn: #C9A75C;
    --bad: #C86B6B;
    --blue: #5C7993;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Yu Gothic", sans-serif;
    letter-spacing: 0;
  }
  header {
    position: sticky; top: 0; z-index: 5; background: rgba(23,24,27,.96);
    border-bottom: 1px solid var(--line); padding: 14px 16px 12px;
  }
  h1 { font-size: 20px; margin: 0 0 10px; }
  .summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
  .tile { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 10px; }
  .tile b { display: block; font-size: 11px; color: var(--muted); margin-bottom: 4px; }
  .tile span { font-size: 22px; font-weight: 800; font-family: "SF Mono", Menlo, monospace; }
  .tile.ok span { color: var(--ok); }
  .tile.bad span { color: var(--bad); }
  .controls { display: flex; gap: 8px; margin-top: 10px; }
  button {
    border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px;
    background: var(--panel); color: var(--text); font-weight: 800; cursor: pointer;
  }
  button.primary { background: var(--ok); border-color: var(--ok); color: #151713; }
  main { padding: 14px 16px 30px; }
  .case {
    border: 1px solid var(--line); background: var(--panel2); border-radius: 8px;
    padding: 12px; margin-bottom: 8px; transition: transform .16s, border-color .16s, background .16s;
  }
  .case.active {
    transform: translateX(3px); border-color: var(--warn); background: #302b21;
    box-shadow: 0 0 0 2px rgba(201,167,92,.18);
  }
  .case-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
  .no { font-family: "SF Mono", Menlo, monospace; color: var(--muted); font-weight: 700; }
  .badge { padding: 3px 8px; border-radius: 999px; background: #303843; color: #d8e2ec; font-size: 12px; }
  .state { margin-left: auto; font-weight: 900; color: var(--ok); }
  .case.fail .state { color: var(--bad); }
  .utter { font-size: 17px; font-weight: 800; margin-bottom: 10px; line-height: 1.5; }
  .grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
  .grid div { background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 8px; min-width: 0; }
  .grid b { display: block; color: var(--muted); font-size: 11px; margin-bottom: 4px; }
  .grid span { display: block; word-break: break-all; line-height: 1.45; font-family: "SF Mono", Menlo, monospace; }
  .note { color: var(--muted); line-height: 1.7; margin: 12px 0; font-size: 13px; }
  @media (max-width: 720px) {
    .summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .grid { grid-template-columns: 1fr; }
    h1 { font-size: 18px; }
  }
</style>
</head>
<body>
<header>
  <h1>音声採寸メモ 100人仮想ユーザーデバッグ</h1>
  <div class="summary">
    <div class="tile"><b>仮想ユーザー</b><span>${results.length}</span></div>
    <div class="tile ok"><b>成功</b><span>${passed}</span></div>
    <div class="tile bad"><b>失敗</b><span>${failed}</span></div>
    <div class="tile"><b>成功率</b><span>${Math.round((passed / results.length) * 100)}%</span></div>
  </div>
  <div class="controls">
    <button class="primary" id="playBtn" type="button">100人テスト再生</button>
    <button id="stopBtn" type="button">停止</button>
    <button id="failBtn" type="button">NGだけ表示</button>
  </div>
</header>
<main>
  <p class="note">実際の音声そのものではなく、スマホやPCの音声認識が返しがちな文字列を100パターン流し、登録結果が期待通りかを確認しています。危険な曖昧認識は勝手に登録せず「登録なし」にする方針です。</p>
  ${rows}
</main>
<script>
  const cards = Array.from(document.querySelectorAll('.case'));
  let timer = null;
  let index = 0;
  function activate(i) {
    cards.forEach(c => c.classList.remove('active'));
    const card = cards[i];
    if (!card) return;
    card.classList.add('active');
    card.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }
  document.getElementById('playBtn').onclick = () => {
    clearInterval(timer);
    index = 0;
    activate(index);
    timer = setInterval(() => {
      index += 1;
      if (index >= cards.length) { clearInterval(timer); return; }
      activate(index);
    }, 140);
  };
  document.getElementById('stopBtn').onclick = () => clearInterval(timer);
  document.getElementById('failBtn').onclick = () => {
    const onlyFail = document.body.classList.toggle('only-fail');
    cards.forEach(c => { c.style.display = onlyFail && !c.classList.contains('fail') ? 'none' : ''; });
    document.getElementById('failBtn').textContent = onlyFail ? '全件表示' : 'NGだけ表示';
  };
</script>
</body>
</html>`;

const outPath = path.resolve(__dirname, '..', 'voice_memo_virtual_users_preview.html');
fs.writeFileSync(outPath, preview, 'utf8');
console.log(JSON.stringify({ outPath, total: results.length, passed, failed }, null, 2));

if (failed) process.exitCode = 1;
