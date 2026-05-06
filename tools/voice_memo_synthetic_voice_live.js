const fs = require('fs');
const http = require('http');
const path = require('path');
const { spawnSync } = require('child_process');

const root = path.resolve(__dirname, '..');
const htmlPath = fs.existsSync(path.resolve(root, 'templates', 'voice_memo.html'))
  ? path.resolve(root, 'templates', 'voice_memo.html')
  : path.resolve(root, 'voice_memo.html');
const appHtml = fs.readFileSync(htmlPath, 'utf8');
const audioDir = path.resolve(root, 'voice_memo_audio_tests_live');
const ttsScript = path.resolve(__dirname, 'voice_memo_generate_tts.ps1');
const recogScript = path.resolve(__dirname, 'voice_memo_recognize_wav.ps1');
const port = Number(process.env.VOICE_MEMO_LIVE_PORT || 17831);
const runOnce = process.argv.includes('--once');

fs.mkdirSync(audioDir, { recursive: true });

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

const parser = new Function(`
${extractConst(appHtml, 'STOPWORDS')}
${extractConst(appHtml, 'ITEM_NOISE_WORDS')}
${extractConst(appHtml, 'MAX_AUTO_COUNT')}
let currentType = 'wallpaper';
${extractFunction(appHtml, 'normalize')}
${extractFunction(appHtml, 'normalizeBareSizeForType')}
${extractFunction(appHtml, 'splitFusedSizeCountSafe')}
${extractFunction(appHtml, 'splitCompactFusedItemsSafe')}
${extractFunction(appHtml, 'parseSpeechItemsSafe')}
return { normalize, parseSpeechItemsSafe };
`)();

function asItem(size, count) {
  return { size, count };
}

function sameItems(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

function totalM(items) {
  return items.reduce((sum, it) => sum + (it.size * it.count) / 1000, 0);
}

function fmt(n) {
  if (Math.abs(n - Math.round(n)) < 0.001) return Math.round(n).toLocaleString('ja-JP');
  return n.toLocaleString('ja-JP', { maximumFractionDigits: 2 });
}

function itemLabel(items) {
  return items && items.length ? items.map(it => `${it.size}×${it.count}`).join(' / ') : '登録なし';
}

const spokenSizes = [
  [38, 'さんじゅうはち'],
  [99, 'きゅうじゅうきゅう'],
  [180, 'ひゃくはちじゅう'],
  [250, 'にひゃくごじゅう'],
  [380, 'さんびゃくはちじゅう'],
  [420, 'よんひゃくにじゅう'],
  [530, 'ごひゃくさんじゅう'],
  [999, 'きゅうひゃくきゅうじゅうきゅう'],
  [1000, 'せん'],
  [1200, 'せんにひゃく'],
  [2300, 'にせんさんびゃく'],
  [2400, 'にせんよんひゃく'],
  [3200, 'さんぜんにひゃく'],
  [3400, 'さんぜんよんひゃく'],
  [5300, 'ごせんさんびゃく'],
  [10000, 'いちまん'],
];
const spokenCounts = [
  [1, 'いっぽん'],
  [2, 'にほん'],
  [3, 'さんぼん'],
  [4, 'よんほん'],
  [5, 'ごほん'],
  [6, 'ろっぽん'],
  [10, 'じゅっぽん'],
  [12, 'じゅうにほん'],
  [20, 'にじゅっぽん'],
  [50, 'ごじゅっぽん'],
  [100, 'ひゃっぽん'],
];

const fixedCases = [
  { id: 'spoken-3200-3', spoken: 'さんぜんにひゃく さんぼん。', expected: [asItem(3200, 3)] },
  { id: 'spoken-5300-1', spoken: 'ごせんさんびゃく いっぽん。', expected: [asItem(5300, 1)] },
  { id: 'spoken-380-4', spoken: 'さんびゃくはちじゅう よんほん。', expected: [asItem(380, 4)] },
  { id: 'spoken-420-1', spoken: 'よんひゃくにじゅう いっぽん。', expected: [asItem(420, 1)] },
  { id: 'spoken-38-4', spoken: 'さんじゅうはち よんほん。', expected: [asItem(38, 4)] },
  { id: 'spoken-2300-2', spoken: 'にせんさんびゃく にほん。', expected: [asItem(2300, 2)] },
  { id: 'spoken-two-items', spoken: 'さんぜんにひゃく さんぼん。ごせんさんびゃく いっぽん。', expected: [asItem(3200, 3), asItem(5300, 1)] },
  { id: 'spoken-three-items', spoken: 'ごせんさんびゃく さんぼん。さんぜんにひゃく いっぽん。よんひゃくにじゅう いっぽん。', expected: [asItem(5300, 3), asItem(3200, 1), asItem(420, 1)] },
];

function generatedCase(i) {
  const [s1, sp1] = spokenSizes[i % spokenSizes.length];
  const [c1, cp1] = spokenCounts[(i * 3) % spokenCounts.length];
  if (i % 4 === 0) {
    const [s2, sp2] = spokenSizes[(i * 5 + 2) % spokenSizes.length];
    const [c2, cp2] = spokenCounts[(i * 7 + 1) % spokenCounts.length];
    return {
      id: `generated-two-${i}`,
      spoken: `${sp1} ${cp1}。${sp2} ${cp2}。`,
      expected: [asItem(s1, c1), asItem(s2, c2)],
    };
  }
  return {
    id: `generated-one-${i}`,
    spoken: `${sp1} ${cp1}。`,
    expected: [asItem(s1, c1)],
  };
}

const cases = [...fixedCases];
for (let i = 0; cases.length < 100; i++) cases.push(generatedCase(i));

const state = {
  startedAt: new Date().toISOString(),
  status: 'starting',
  total: cases.length,
  done: 0,
  passed: 0,
  failed: 0,
  current: null,
  results: [],
};

function saveState() {
  fs.writeFileSync(path.resolve(audioDir, 'state.json'), JSON.stringify(state, null, 2), 'utf8');
}

function ps(command) {
  const res = spawnSync('powershell.exe', [
    '-NoProfile',
    '-ExecutionPolicy', 'Bypass',
    '-Command',
    `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; ${command}`,
  ], { encoding: 'utf8', maxBuffer: 1024 * 1024 * 10 });
  return res;
}

function psQuote(value) {
  return `'${String(value).replace(/'/g, "''")}'`;
}

function generateAudio(test, index) {
  const wavPath = path.resolve(audioDir, `${String(index + 1).padStart(3, '0')}_${test.id}.wav`);
  const cmd = `& ${psQuote(ttsScript)} -Text ${psQuote(test.spoken)} -OutFile ${psQuote(wavPath)} -Rate 1`;
  const res = ps(cmd);
  if (res.status !== 0) {
    throw new Error(`TTS failed: ${res.stdout}\n${res.stderr}`);
  }
  return wavPath;
}

function recognizeAudio(wavPath) {
  const cmd = `& ${psQuote(recogScript)} -WavFile ${psQuote(wavPath)} -TimeoutSeconds 10`;
  const res = ps(cmd);
  const raw = (res.stdout || '').trim();
  if (!raw) {
    return { text: '', confidence: 0, alternates: [], error: res.stderr || 'no output' };
  }
  try {
    return JSON.parse(raw);
  } catch (error) {
    return { text: '', confidence: 0, alternates: [], error: `json parse error: ${raw}` };
  }
}

function repairAmbiguousNihonTranscript(text, alternatives) {
  const altText = alternatives.join(' ');
  if (!/(nippon|ニッポン)/i.test(altText)) return text;
  if (/(?:2|二)\s*(?:本|ほん|ぽん|ぼん)/.test(altText)) return text;
  return text.replace(/\b(\d{3,6})\s*(日本|にほん)(?=$|[\s、。,.])/ig, (m, size) => `${size}一本`);
}

function parseRecognized(recognition) {
  const candidates = [recognition.text || '', ...(recognition.alternates || [])].filter(Boolean);
  const parsedCandidates = candidates.map(text => {
    const repairedText = repairAmbiguousNihonTranscript(text, candidates);
    const parsed = parser.parseSpeechItemsSafe(repairedText);
    return {
      text: repairedText,
      normalized: parser.normalize(repairedText),
      items: parsed.items,
      pendingSize: parsed.pendingSize,
      totalM: totalM(parsed.items),
    };
  });
  function candidateScore(c) {
    let score = c.items.length * 30;
    if (c.pendingSize !== null) score -= 5;
    for (const item of c.items) {
      if (item.size >= 100 && item.size <= 100000) score += 4;
      if (item.size % 100 === 0) score += 6;
      else if (item.size % 10 === 0) score += 3;
      if (item.count >= 1 && item.count <= 100) score += 5;
      if (item.count > 999) score -= 10;
      if (item.size < 10) score -= 7;
    }
    return score;
  }
  const ranked = parsedCandidates
    .map((c, i) => ({ ...c, candidateIndex: i, score: candidateScore(c) }))
    .sort((a, b) => b.score - a.score || a.candidateIndex - b.candidateIndex);
  const first = ranked.find(c => c.candidateIndex === 0);
  let primary = ranked[0] || { text: '', normalized: '', items: [], pendingSize: null, totalM: 0, score: 0, candidateIndex: 0 };
  if (first && first.items.length && primary.score < first.score + 12) {
    primary = first;
  }
  return { primary, parsedCandidates };
}

async function runTests() {
  state.status = 'running';
  saveState();
  for (let i = 0; i < cases.length; i++) {
    const test = cases[i];
    state.current = { no: i + 1, id: test.id, spoken: test.spoken };
    saveState();
    const started = Date.now();
    let row;
    try {
      const wavPath = generateAudio(test, i);
      const recognition = recognizeAudio(wavPath);
      const parsed = parseRecognized(recognition);
      const ok = sameItems(parsed.primary.items, test.expected);
      row = {
        no: i + 1,
        id: test.id,
        spoken: test.spoken,
        expected: test.expected,
        expectedLabel: itemLabel(test.expected),
        expectedTotalM: fmt(totalM(test.expected)),
        audio: `/audio/${path.basename(wavPath)}`,
        recognition,
        primary: parsed.primary,
        alternatives: parsed.parsedCandidates.slice(1),
        actualLabel: itemLabel(parsed.primary.items),
        actualTotalM: fmt(parsed.primary.totalM),
        ok,
        ms: Date.now() - started,
      };
    } catch (error) {
      row = {
        no: i + 1,
        id: test.id,
        spoken: test.spoken,
        expected: test.expected,
        expectedLabel: itemLabel(test.expected),
        expectedTotalM: fmt(totalM(test.expected)),
        audio: null,
        recognition: { text: '', confidence: 0, alternates: [], error: error.message },
        primary: { text: '', normalized: '', items: [], pendingSize: null, totalM: 0 },
        alternatives: [],
        actualLabel: 'エラー',
        actualTotalM: '0',
        ok: false,
        ms: Date.now() - started,
      };
    }
    state.results.push(row);
    state.done = state.results.length;
    state.passed = state.results.filter(r => r.ok).length;
    state.failed = state.done - state.passed;
    saveState();
  }
  state.status = 'finished';
  state.current = null;
  state.finishedAt = new Date().toISOString();
  saveState();
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

const page = `<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>音声採寸メモ 合成音声ライブテスト</title>
<style>
  :root { color-scheme: dark; --bg:#16181b; --panel:#252a30; --panel2:#20242a; --line:#3b4652; --text:#f2f0ee; --muted:#a8b0b8; --ok:#7E8B6C; --bad:#C86B6B; --warn:#C9A75C; --blue:#5C7993; }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Yu Gothic", sans-serif; letter-spacing: 0; }
  header { position: sticky; top: 0; z-index: 10; background: rgba(22,24,27,.97); border-bottom: 1px solid var(--line); padding: 14px 16px; }
  h1 { margin: 0 0 10px; font-size: 20px; }
  .summary { display:grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; }
  .tile { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 10px; min-width:0; }
  .tile b { display:block; color:var(--muted); font-size:11px; margin-bottom:4px; }
  .tile span { font: 800 22px "SF Mono", Menlo, monospace; }
  .ok span { color: var(--ok); } .bad span { color: var(--bad); } .warn span { color: var(--warn); }
  .bar { height: 10px; border-radius:999px; background:#303741; overflow:hidden; margin-top:10px; }
  .bar div { height:100%; width:0%; background:var(--ok); transition:width .25s; }
  main { padding: 14px 16px 32px; }
  .note { color: var(--muted); font-size:13px; line-height:1.7; margin: 0 0 12px; }
  .case { border:1px solid var(--line); background:var(--panel2); border-radius:8px; padding:12px; margin-bottom:8px; }
  .case.fail { border-color: rgba(200,107,107,.7); }
  .case.current { border-color: var(--warn); box-shadow:0 0 0 2px rgba(201,167,92,.16); }
  .head { display:flex; gap:8px; align-items:center; margin-bottom:8px; }
  .no { color:var(--muted); font:700 13px "SF Mono", Menlo, monospace; }
  .state { margin-left:auto; font-weight:900; color:var(--ok); }
  .fail .state { color:var(--bad); }
  .spoken { font-size:17px; font-weight:800; line-height:1.5; margin-bottom:8px; }
  .grid { display:grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap:8px; }
  .cell { background:var(--panel); border:1px solid var(--line); border-radius:6px; padding:8px; min-width:0; }
  .cell b { display:block; color:var(--muted); font-size:11px; margin-bottom:4px; }
  .cell span { display:block; word-break:break-all; line-height:1.45; font-family:"SF Mono", Menlo, monospace; }
  .alts { margin-top:8px; color:var(--muted); font-size:12px; line-height:1.6; word-break:break-all; }
  audio { width: 100%; height: 32px; }
  @media (max-width: 900px) { .summary { grid-template-columns: repeat(2, minmax(0, 1fr)); } .grid { grid-template-columns: 1fr; } h1 { font-size:18px; } }
</style>
</head>
<body>
<header>
  <h1>音声採寸メモ 合成音声ライブテスト</h1>
  <div class="summary">
    <div class="tile"><b>状態</b><span id="status">-</span></div>
    <div class="tile"><b>進捗</b><span id="progress">0/0</span></div>
    <div class="tile ok"><b>OK</b><span id="passed">0</span></div>
    <div class="tile bad"><b>NG</b><span id="failed">0</span></div>
    <div class="tile warn"><b>現在</b><span id="current">-</span></div>
  </div>
  <div class="bar"><div id="bar"></div></div>
</header>
<main>
  <p class="note">これはテキスト投入ではなく、Windows日本語合成音声をWAVにして、Windows日本語音声認識に読ませた結果です。各行の音声は再生できます。</p>
  <div id="list"></div>
</main>
<script>
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function altLabel(row){
  if (!row.alternatives || !row.alternatives.length) return '';
  return '<div class="alts"><b>候補:</b> ' + row.alternatives.slice(0,5).map(a => esc(a.text) + ' → ' + esc((a.items&&a.items.length)?a.items.map(i=>i.size+'×'+i.count).join(' / '):'登録なし')).join(' / ') + '</div>';
}
function rowHtml(row){
  const err = row.recognition && row.recognition.error ? '<div class="alts"><b>error:</b> '+esc(row.recognition.error)+'</div>' : '';
  return '<article class="case '+(row.ok?'pass':'fail')+'">'+
    '<div class="head"><span class="no">#'+row.no+'</span><span>'+esc(row.id)+'</span><span class="state">'+(row.ok?'OK':'NG')+'</span></div>'+
    '<div class="spoken">'+esc(row.spoken)+'</div>'+
    '<div class="grid">'+
      '<div class="cell"><b>音声</b><span>'+(row.audio?'<audio controls src="'+esc(row.audio)+'"></audio>':'なし')+'</span></div>'+
      '<div class="cell"><b>認識文字</b><span>'+esc(row.recognition.text || '')+'</span></div>'+
      '<div class="cell"><b>期待</b><span>'+esc(row.expectedLabel)+'<br>'+esc(row.expectedTotalM)+' m</span></div>'+
      '<div class="cell"><b>実際</b><span>'+esc(row.actualLabel)+'<br>'+esc(row.actualTotalM)+' m</span></div>'+
    '</div>'+altLabel(row)+err+'</article>';
}
async function tick(){
  const res = await fetch('/state?ts=' + Date.now());
  const s = await res.json();
  document.getElementById('status').textContent = s.status;
  document.getElementById('progress').textContent = s.done + '/' + s.total;
  document.getElementById('passed').textContent = s.passed;
  document.getElementById('failed').textContent = s.failed;
  document.getElementById('current').textContent = s.current ? '#' + s.current.no : '-';
  document.getElementById('bar').style.width = s.total ? Math.round((s.done / s.total) * 100) + '%' : '0%';
  const list = document.getElementById('list');
  list.innerHTML = s.results.slice().reverse().map(rowHtml).join('');
  if (s.status !== 'finished') setTimeout(tick, 900);
}
tick();
</script>
</body>
</html>`;

const server = http.createServer((req, res) => {
  if (req.url === '/' || req.url.startsWith('/?')) {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' });
    res.end(page);
    return;
  }
  if (req.url.startsWith('/state')) {
    res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
    res.end(JSON.stringify(state));
    return;
  }
  if (req.url.startsWith('/audio/')) {
    const name = decodeURIComponent(req.url.replace('/audio/', ''));
    const file = path.resolve(audioDir, name);
    if (!file.startsWith(audioDir) || !fs.existsSync(file)) {
      res.writeHead(404);
      res.end('not found');
      return;
    }
    res.writeHead(200, { 'Content-Type': 'audio/wav' });
    fs.createReadStream(file).pipe(res);
    return;
  }
  res.writeHead(404);
  res.end('not found');
});

server.listen(port, '127.0.0.1', () => {
  state.status = 'ready';
  saveState();
  console.log(`VOICE_MEMO_SYNTHETIC_LIVE_URL=http://127.0.0.1:${port}/`);
  runTests().then(() => {
    if (runOnce) {
      server.close(() => process.exit(state.failed ? 1 : 0));
    }
  }).catch(error => {
    state.status = 'error';
    state.error = error.stack || error.message;
    saveState();
    if (runOnce) {
      server.close(() => process.exit(1));
    }
  });
});
