const target = 'ws://127.0.0.1:9222/devtools/page/5CF4361B08352A34A7677D1A94FCF4F7';
import { writeFileSync } from 'node:fs';

const expression = `(() => {
  const heading = Array.from(document.querySelectorAll('h3'))
    .find((element) => element.textContent?.trim() === 'Recovery Payment Link');
  const card = heading?.closest('.bg-white.rounded-xl');
  const label = Array.from(card?.querySelectorAll('div') ?? [])
    .find((element) => element.textContent?.trim() === 'Executed');
  let box = label;
  while (box && !(box.firstElementChild instanceof HTMLElement && box.firstElementChild.style.height)) {
    box = box.parentElement;
  }
  const fill = box?.firstElementChild;
  return {
    intervention: heading?.textContent?.trim(),
    executedText: box?.innerText,
    inlineStyle: fill?.getAttribute('style'),
    fillBackground: getComputedStyle(fill).backgroundColor,
    computedHeight: getComputedStyle(fill).height,
    parentHeight: getComputedStyle(box).height,
    renderedHeight: fill?.getBoundingClientRect().height,
  };
})()`;

const socket = new WebSocket(target);
let nextId = 0;
const pending = new Map();

const send = (method, params = {}) => new Promise((resolve) => {
  const id = ++nextId;
  pending.set(id, resolve);
  socket.send(JSON.stringify({ id, method, params }));
});

socket.onmessage = (event) => {
  const message = JSON.parse(event.data);
  if (message.method === 'Runtime.consoleAPICalled') {
    const logged = message.params.args
      .map((argument) => argument.value ?? argument.description)
      .join(' ');
    if (logged.includes('intervention=recovery_payment_link')) {
      console.log(`BROWSER CONSOLE: ${logged}`);
    }
  }
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
  let pageReady = false;
  for (let attempt = 0; attempt < 15; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 1_000));
    const readiness = await send('Runtime.evaluate', {
      expression: "Array.from(document.querySelectorAll('h3')).some((element) => element.textContent?.trim() === 'Recovery Payment Link')",
      returnByValue: true,
    });
    pageReady = readiness.result.value === true;
    if (pageReady) break;
  }
  if (!pageReady) {
    console.error('The page did not finish loading the Recovery Payment Link card.');
    socket.close();
    return;
  }

  await send('Emulation.setDeviceMetricsOverride', {
    width: 1440,
    height: 900,
    deviceScaleFactor: 1,
    mobile: false,
  });

  const evaluated = await send('Runtime.evaluate', {
    expression,
    returnByValue: true,
  });
  console.log(JSON.stringify(evaluated.result.value, null, 2));

  await send('Runtime.evaluate', {
    expression: `(() => {
      const heading = Array.from(document.querySelectorAll('h3'))
        .find((element) => element.textContent?.trim() === 'Payment Method Update Link');
      heading?.closest('.bg-white')?.scrollIntoView({ block: 'start' });
      document.getElementById('executed-fill-runtime-proof')?.remove();
      const proof = document.createElement('div');
      proof.id = 'executed-fill-runtime-proof';
      proof.textContent = 'LIVE RUNTIME CHECK — ' + new Intl.DateTimeFormat('en-IN', {
        dateStyle: 'medium', timeStyle: 'medium', timeZone: 'Asia/Kolkata',
      }).format(new Date()) + ' IST — cache-bypassed reload';
      Object.assign(proof.style, {
        position: 'fixed', top: '16px', right: '16px', zIndex: '9999',
        padding: '9px 12px', borderRadius: '8px', background: '#1A1A1A',
        color: '#FFFFFF', font: '600 12px system-ui', letterSpacing: '0.02em',
        boxShadow: '0 4px 14px rgba(0,0,0,0.2)',
      });
      document.body.append(proof);
    })()`,
  });

  await new Promise((resolve) => setTimeout(resolve, 300));
  const screenshot = await send('Page.captureScreenshot', { format: 'png' });
  writeFileSync(new URL('../executed-fill-runtime-proof.png', import.meta.url), screenshot.data, 'base64');
  socket.close();
};

setTimeout(() => socket.close(), 30_000);
