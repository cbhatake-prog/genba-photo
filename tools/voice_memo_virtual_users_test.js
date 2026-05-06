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
  { id: 'small-38-spaced-kept', utterance: '38 4本', expected: [asItem(38, 4)] },
  { id: 'small-530-spaced-kept', utterance: '530 3本', expected: [asItem(530, 3)] },
  { id: 'max-mm-valid', utterance: '100000 1本', expected: [asItem(100000, 1)] },
  { id: 'large-count-guard', utterance: '99 999本 100 120本', expected: [asItem(100, 120)] },
  { id: 'video-bad-no-counter', utterance: '31001 401', expected: [] },
  { id: 'video-bad-chain-no-counter', utterance: '4303 32001 31001 401', expected: [] },
  { id: 'voice-compact-384', utterance: '384本', expected: [asItem(380, 4)] },
  { id: 'user-voice-asr-count-words', utterance: '2100\u4e00\u672c 550\u5c71\u9580 130\u306b\u4e00\u672c 999\u5b6b\u6587 930\u3092\u4e00\u672c 530\u672c', expected: [asItem(2100, 1), asItem(550, 3), asItem(130, 1), asItem(999, 3), asItem(930, 1), asItem(530, 1)] },
  { id: 'user-voice-asr-fused-context', utterance: '1504 6300\u65e5\u672c', expected: [asItem(1500, 4), asItem(6300, 2)] },
  { id: 'size-with-counter-ending-zero', utterance: '530\u672c', expected: [asItem(530, 1)] },
  { id: 'android-2300-leading-zero-ok', utterance: '02300 2\u672c', expected: [asItem(2300, 2)] },
  { id: 'android-2300-leading-zero-shifted', utterance: '0230 2\u672c', expected: [asItem(2300, 2)] },
  { id: 'android-2300-spaced-drop-zero', utterance: '230 2\u672c', expected: [asItem(2300, 2)] },
  { id: 'android-2300-fused-drop-zero', utterance: '2302\u672c', expected: [asItem(2300, 2)] },
  { id: 'android-2300-fused-leading-zero', utterance: '02302\u672c', expected: [asItem(2300, 2)] },
  { id: 'size-230-counter-alone-valid', utterance: '230\u672c', expected: [asItem(230, 1)] },
  { id: 'android-8900-spaced-drop-zero', utterance: '890 2\u672c', expected: [asItem(8900, 2)] },
  { id: 'android-8900-fused-drop-zero', utterance: '8902\u672c', expected: [asItem(8900, 2)] },
  { id: 'android-8900-same-item-repeated-in-one-final', utterance: '8902\u672c 8902\u672c 8902\u672c', expected: [asItem(8900, 2), asItem(8900, 2), asItem(8900, 2)] },
  { id: 'video-5400-fused-5403', utterance: '5403\u672c', expected: [asItem(5400, 3)] },
  { id: 'video-5400-fused-5402', utterance: '5402\u672c', expected: [asItem(5400, 2)] },
  { id: 'video-3200-extra-ni-fused', utterance: '32023\u672c', expected: [asItem(3200, 3)] },
  { id: 'video-5400-extra-ni-fused', utterance: '54023\u672c', expected: [asItem(5400, 3)] },
  { id: 'video-9800-extra-ichi-fused', utterance: '98018\u672c', expected: [asItem(9800, 8)] },
  { id: 'video-9800-extra-noise-fused', utterance: '98213\u672c', expected: [asItem(9800, 3)] },
  { id: 'video-3200-spaced-tail-noise', utterance: '3202 3\u672c', expected: [asItem(3200, 3)] },
  { id: 'video-2400-spaced-mid-tail-noise', utterance: '2423 2\u672c', expected: [asItem(2400, 2)] },
  { id: 'video-5400-spaced-tail-noise', utterance: '5402 3\u672c', expected: [asItem(5400, 3)] },
  { id: 'video-9800-spaced-tail-noise', utterance: '9801 8\u672c', expected: [asItem(9800, 8)] },
  { id: 'video-9800-spaced-mid-tail-noise', utterance: '9821 3\u672c', expected: [asItem(9800, 3)] },
  { id: 'video-402-spaced-left-raw', utterance: '402 2\u672c', expected: [asItem(402, 2)] },
  { id: 'asr-999-q-noise', utterance: '190Q3\u672c', expected: [asItem(999, 3)] },
  { id: 'asr-999-12-nihon', utterance: '990 90\u65e5\u672c', expected: [asItem(999, 12)] },
  { id: 'asr-530-yaku30-kanji-count', utterance: '\u7d0430\u4e09\u672c', expected: [asItem(530, 3)] },
  { id: 'asr-530-yaku35-fused', utterance: '\u7d0435\u672c', expected: [asItem(530, 5)] },
  { id: 'asr-530-yaku35-tens', utterance: '\u7d0435\u5341\u672c', expected: [asItem(530, 50)] },
  { id: 'asr-530-yaku30-nihon12', utterance: '\u7d0430 10\u65e5\u672c', expected: [asItem(530, 12)] },
  { id: 'asr-10000-fused-count', utterance: '10006\u672c', expected: [asItem(10000, 6)] },
  { id: 'asr-5300-fused-1304', utterance: '1304', expected: [asItem(5300, 4)] },
  { id: 'asr-5300-fused-1320', utterance: '1320\u672c', expected: [asItem(5300, 20)] },
  { id: 'asr-5300-100-shaon', utterance: '1300\u793e\u3092\u3093', expected: [asItem(5300, 100)] },
  { id: 'asr-180-drop-leading-hundred', utterance: '80\u4e00\u672c', expected: [asItem(180, 1)] },
  { id: 'asr-1000-1-senion', utterance: '\u7e4a\u7dadON 2300\u65e5\u672c', expected: [asItem(1000, 1), asItem(2300, 2)] },
  { id: 'asr-1000-100-2300-fused', utterance: '90000002300\u5341\u672c', expected: [asItem(1000, 100), asItem(2300, 10)] },
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
