const fs = require('fs');
const http = require('http');
const path = require('path');
const { spawn, spawnSync } = require('child_process');

const root = path.resolve(__dirname, '..');
const harnessHtml = fs.readFileSync(path.resolve(__dirname, 'voice_memo_speech_harness.html'), 'utf8');
const chromePath = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const userDataDir = path.resolve(root, '.chrome_voice_memo_speaker_profile');

const test = {
  id: 'speaker-3200-3-5300-1-380-4',
  text: process.argv.slice(2).join(' ') || 'さんぜんにひゃく さんぼん。ごせんさんびゃく いっぽん。さんびゃくはちじゅう よんほん。',
};

fs.mkdirSync(userDataDir, { recursive: true });

const events = [];
let server;
let chrome;

function wait(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function psQuote(value) {
  return `'${String(value).replace(/'/g, "''")}'`;
}

function speakOutLoud(text) {
  const command = [
    "$voice = New-Object -ComObject SAPI.SpVoice",
    "$voice.Rate = 1",
    "$voice.Volume = 100",
    `[void]$voice.Speak(${psQuote(text)}, 0)`,
  ].join('; ');
  return spawnSync('powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', command], {
    encoding: 'utf8',
    maxBuffer: 1024 * 1024,
  });
}

async function main() {
  server = http.createServer((req, res) => {
    if (req.method === 'GET') {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' });
      res.end(harnessHtml);
      return;
    }
    if (req.method === 'POST' && req.url === '/log') {
      let body = '';
      req.on('data', chunk => { body += chunk; });
      req.on('end', () => {
        try { events.push(JSON.parse(body)); } catch (_) {}
        res.writeHead(204);
        res.end();
      });
      return;
    }
    res.writeHead(404);
    res.end('not found');
  });

  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  const url = `http://127.0.0.1:${port}/?auto=1&id=${encodeURIComponent(test.id)}`;

  chrome = spawn(chromePath, [
    `--user-data-dir=${userDataDir}`,
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-features=Translate,OptimizationHints',
    '--use-fake-ui-for-media-stream',
    url,
  ], { detached: false, stdio: 'ignore' });

  await wait(2500);
  const speech = speakOutLoud(test.text);
  await wait(7000);
  try { chrome.kill(); } catch (_) {}
  await new Promise(resolve => server.close(resolve));

  const finalEvents = events.filter(e => e.event === 'end');
  const resultEvents = events.filter(e => e.event === 'result');
  const errors = events.filter(e => e.event === 'error' || e.event === 'unsupported' || e.event === 'start-error');
  const finalText = finalEvents.at(-1)?.payload?.finalText || resultEvents.at(-1)?.payload?.finalText || '';
  const out = {
    text: test.text,
    finalText,
    events: events.length,
    resultEvents: resultEvents.length,
    errors,
    speechStatus: speech.status,
    speechError: speech.error ? speech.error.message : null,
    speechStderr: speech.stderr || '',
    lastResult: resultEvents.at(-1)?.payload || null,
  };
  console.log(JSON.stringify(out, null, 2));
  if (speech.status !== 0 || speech.error) process.exitCode = 3;
  else if (errors.length) process.exitCode = 1;
  else if (!finalText) process.exitCode = 2;
}

main().catch(err => {
  try { if (chrome) chrome.kill(); } catch (_) {}
  try { if (server) server.close(); } catch (_) {}
  console.error(err.stack || err.message);
  process.exit(1);
});
