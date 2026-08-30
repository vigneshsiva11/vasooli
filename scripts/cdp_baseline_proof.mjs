import { writeFileSync } from 'node:fs';

const pages = await (await fetch('http://127.0.0.1:9223/json/list')).json();
const page = pages.find((candidate) => candidate.type === 'page' && candidate.url.includes('/baseline'));
if (!page) throw new Error('Baseline page is not attached to Chrome DevTools.');

const socket = new WebSocket(page.webSocketDebuggerUrl);
let nextId = 0;
const pending = new Map();
const send = (method, params = {}) => new Promise((resolve) => {
  const id = ++nextId;
  pending.set(id, resolve);
  socket.send(JSON.stringify({ id, method, params }));
});

socket.onmessage = (event) => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    pending.get(message.id)(message.result);
    pending.delete(message.id);
  }
};

socket.onopen = async () => {
  await send('Runtime.enable');
  await send('Page.enable');
  await send('Network.setCacheDisabled', { cacheDisabled: true });
  await send('Page.reload', { ignoreCache: true });

  for (let attempt = 0; attempt < 15; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 1_000));
    const ready = await send('Runtime.evaluate', {
      expression: "document.body.innerText.includes('Expected recovery by strategy')",
      returnByValue: true,
    });
    if (ready.result.value === true) break;
    if (attempt === 14) throw new Error('Baseline screen did not finish loading.');
  }

  await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1100, deviceScaleFactor: 1, mobile: false });
  await send('Runtime.evaluate', {
    expression: `(() => {
      window.scrollTo({ top: 0, behavior: 'instant' });
      const prior = document.getElementById('baseline-runtime-proof');
      prior?.remove();
      const proof = document.createElement('div');
      proof.id = 'baseline-runtime-proof';
      proof.textContent = 'LIVE BASELINE CHECK — ' + new Intl.DateTimeFormat('en-IN', { dateStyle: 'medium', timeStyle: 'medium', timeZone: 'Asia/Kolkata' }).format(new Date()) + ' IST — fresh server + cache-bypassed reload';
      Object.assign(proof.style, { position: 'fixed', top: '16px', right: '16px', zIndex: '9999', padding: '9px 12px', borderRadius: '8px', background: '#1A1A1A', color: '#FFFFFF', font: '600 12px system-ui', letterSpacing: '0.02em', boxShadow: '0 4px 14px rgba(0,0,0,0.2)' });
      document.body.append(proof);
    })()`,
  });
  await new Promise((resolve) => setTimeout(resolve, 350));
  const screenshot = await send('Page.captureScreenshot', { format: 'png' });
  writeFileSync(new URL('../baseline-runtime-proof.png', import.meta.url), screenshot.data, 'base64');
  socket.close();
};
