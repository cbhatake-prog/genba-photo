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
${extractConst(html, 'MAX_AUTO_COUNT')}
let currentType = 'wallpaper';
${extractFunction(html, 'normalize')}
${extractFunction(html, 'normalizeBareSizeForType')}
${extractFunction(html, 'splitFusedSizeCountSafe')}
${extractFunction(html, 'splitCompactFusedItemsSafe')}
${extractFunction(html, 'parseSpeechItemsSafe')}
return { normalize, splitFusedSizeCountSafe, parseSpeechItemsSafe };
`)();

function itemsEqual(actual, expected) {
  return JSON.stringify(actual.items) === JSON.stringify(expected);
}

function asItem(size, count) {
  return { size, count };
}

const sizes = [1, 2, 9, 38, 99, 100, 180, 250, 380, 420, 530, 999, 1000, 1200, 2400, 3200, 3400, 5300, 10000, 100000];
const counts = [1, 2, 3, 4, 5, 6, 10, 12, 20, 50, 100, 120, 200];
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
  return { id: `normal-${i}`, utterance, expected: [asItem(s1, c1), asItem(s2, c2)] };
}

function spacedCounterCase(i) {
  const s = sizes[(i * 11 + 5) % sizes.length];
  const c = counts[(i * 4 + 1) % counts.length];
  const utterance = `${s} ${c} 本`;
  return { id: `spaced-counter-${i}`, utterance, expected: [asItem(s, c)] };
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
  return { id: `fused-${i}`, utterance, expected: [asItem(size, count)] };
}

const fixedCases = [
  { id: 'kana-3200-4', utterance: 'さんぜんにひゃく 4本', expected: [asItem(3200, 4)] },
  { id: 'kana-5300-3', utterance: 'ごせんさんびゃく 3本', expected: [asItem(5300, 3)] },
  { id: 'kana-fused-3200-4', utterance: 'さんぜんにひゃくよんほん', expected: [asItem(3200, 4)] },
  { id: 'kana-fused-5300-3', utterance: 'ごせんさんびゃくさんぼん', expected: [asItem(5300, 3)] },
  { id: 'kana-fused-380-4', utterance: 'さんびゃくはちじゅうよんほん', expected: [asItem(380, 4)] },
  { id: 'kana-fused-38-4', utterance: 'さんじゅうはちよんほん', expected: [asItem(38, 4)] },
  { id: 'digit-kana-count-38-4', utterance: '38よんほん', expected: [asItem(38, 4)] },
  { id: 'digit-kana-count-3200-12', utterance: '3200じゅうにほん', expected: [asItem(3200, 12)] },
  { id: 'fast-kana-two-items', utterance: 'さんぜんにひゃくよんほんごせんさんびゃくさんぼん', expected: [asItem(3200, 4), asItem(5300, 3)] },
  { id: 'kanji-3200-4', utterance: '三千二百 4本', expected: [asItem(3200, 4)] },
  { id: 'fullwidth', utterance: '５３００ ３本 ３２００ １本', expected: [asItem(5300, 3), asItem(3200, 1)] },
  { id: 'comma', utterance: '5300、3本、3200、1本', expected: [asItem(5300, 3), asItem(3200, 1)] },
  { id: 'mixed-fused-normal', utterance: '53003本 3200 1本 4201本', expected: [asItem(5300, 3), asItem(3200, 1), asItem(420, 1)] },
  { id: 'small-mm-valid', utterance: '1 2本 2 3本 9 4本', expected: [asItem(1, 2), asItem(2, 3), asItem(9, 4)] },
  { id: 'max-mm-valid', utterance: '100000 1本', expected: [asItem(100000, 1)] },
  { id: 'large-count-guard', utterance: '99 999本 100 120本', expected: [asItem(100, 120)] },
  { id: 'video-bad-no-counter', utterance: '31001 401', expected: [] },
  { id: 'video-bad-chain-no-counter', utterance: '4303 32001 31001 401', expected: [] },
  { id: 'voice-compact-384', utterance: '384本', expected: [asItem(380, 4)] },
];

const cases = [...fixedCases];
for (let i = 0; cases.length < 100; i++) {
  if (i % 3 === 0) cases.push(normalCase(i));
  else if (i % 3 === 1) cases.push(spacedCounterCase(i));
  else cases.push(fusedCase(i));
}

let failures = 0;
for (const test of cases) {
  const actual = harness.parseSpeechItemsSafe(test.utterance);
  const ok = itemsEqual(actual, test.expected);
  if (!ok) {
    failures++;
    console.error(`FAIL ${test.id}`);
    console.error(`  input:    ${test.utterance}`);
    console.error(`  expected: ${JSON.stringify(test.expected)}`);
    console.error(`  actual:   ${JSON.stringify(actual.items)} pending=${actual.pendingSize}`);
  }
}

console.log(JSON.stringify({
  totalVirtualUsers: cases.length,
  failures,
  passed: cases.length - failures,
}, null, 2));

if (failures) process.exit(1);
