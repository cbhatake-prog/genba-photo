const fs = require('fs');
const http = require('http');
const path = require('path');
const { spawn, spawnSync } = require('child_process');

const root = path.resolve(__dirname, '..');
const harnessHtml = fs.readFileSync(path.resolve(__dirname, 'voice_memo_speech_harness.html'), 'utf8');
const chromePath = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const ttsScript = path.resolve(__dirname, 'voice_memo_generate_tts.ps1');
const audioDir = path.resolve(root, 'voice_memo_audio_tests');
const userDataDir = path.resolve(root, '.chrome_voice_memo_test_profile');

const test = {
  id: 'tts-3200-3-5300-1-380-4',
  text: 'さんぜんにひゃく さんぼん。ごせんさんびゃく いっぽん。さんびゃくはちじゅう よんほん。',
};

fs.mkdirSync(audioDir, { recursive: true });
fs.mkdirSync(userDataDir, { recursive: true });
const wavPath = path.resolve(audioDir, `${test.id}.wav`);

function runPowerShell(args) {
  const res = spawnSync('powershell.exe', args, { encoding: 'utf8' });
  if (res.status !== 0) {
    throw new Error(`PowerShell failed\n${res.stdout}\n${res.stderr}`);
  }
  return res;
}

runPowerShell([
  '-NoProfile',
  '-ExecutionPolicy', 'Bypass',
  '-File', ttsScript,
  '-Text', test.text,
  '-OutFile', wavPath,
  '-Rate', '1',
]);

const events = [];
let server;
let chrome;

function wait(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function main() {
  server = http.createServer((req, res) => {
    if (req.method === 'GET') {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
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

  const args = [
    `--user-data-dir=${userDataDir}`,
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-features=Translate,OptimizationHints',
    '--use-fake-ui-for-media-stream',
    '--use-fake-device-for-media-stream',
    `--use-file-for-fake-audio-capture=${wavPath}`,
    url,
  ];

  chrome = spawn(chromePath, args, { detached: false, stdio: 'ignore' });
  await wait(22000);
  try { chrome.kill(); } catch (_) {}
  await new Promise(resolve => server.close(resolve));

  const finalEvents = events.filter(e => e.event === 'end');
  const resultEvents = events.filter(e => e.event === 'result');
  const errors = events.filter(e => e.event === 'error' || e.event === 'unsupported' || e.event === 'start-error');
  const finalText = finalEvents.at(-1)?.payload?.finalText || resultEvents.at(-1)?.payload?.finalText || '';
  const out = {
    wavPath,
    text: test.text,
    finalText,
    events: events.length,
    resultEvents: resultEvents.length,
    errors,
    lastResult: resultEvents.at(-1)?.payload || null,
  };
  fs.writeFileSync(path.resolve(audioDir, `${test.id}.json`), JSON.stringify(out, null, 2), 'utf8');
  console.log(JSON.stringify(out, null, 2));
  if (!finalText && !errors.length) process.exitCode = 2;
  if (errors.length) process.exitCode = 1;
}

main().catch(err => {
  try { if (chrome) chrome.kill(); } catch (_) {}
  try { if (server) server.close(); } catch (_) {}
  console.error(err.stack || err.message);
  process.exit(1);
});
