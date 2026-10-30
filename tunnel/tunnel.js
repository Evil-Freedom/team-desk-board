/**
 * 把本机 8900 暴露到公网（localtunnel）
 *
 * 启动：node tunnel.js
 * 地址：写入 tunnel-url.txt，并推送到飞书（通过本地 /api/announce）
 *
 * 免费版不支持自定义子域名，每次重启地址都会变；
 * 变一次就推一条飞书消息，同事点消息里的链接即可，不用记地址。
 */
const fs = require('fs');
const path = require('path');
const http = require('http');
const localtunnel = require('localtunnel');

const PORT = Number(process.env.PORT || 8900);
const OUT = path.join(__dirname, 'tunnel-url.txt');

function save(url) {
  fs.writeFileSync(OUT, url + '\n' + new Date().toISOString() + '\n', 'utf8');
}

/** 通知本地服务，让它推到飞书。失败不影响隧道本身。 */
function announce(url) {
  return new Promise((resolve) => {
    const body = JSON.stringify({ url });
    const req = http.request({
      host: '127.0.0.1',
      port: PORT,
      path: '/api/announce',
      method: 'POST',
      timeout: 8000,
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(body),
      },
    }, (res) => {
      let buf = '';
      res.on('data', (d) => (buf += d));
      res.on('end', () => {
        console.log('announce =>', res.statusCode, buf.slice(0, 120));
        resolve();
      });
    });
    req.on('error', (e) => {
      console.log('announce failed:', e.message);
      resolve();
    });
    req.write(body);
    req.end();
  });
}

async function start() {
  const tunnel = await localtunnel({ port: PORT });
  console.log('PUBLIC_URL=' + tunnel.url);
  save(tunnel.url);
  await announce(tunnel.url);

  tunnel.on('close', () => {
    console.log('closed, reconnecting in 5s…');
    setTimeout(start, 5000);
  });

  tunnel.on('error', (err) => {
    console.error('tunnel error:', err.message);
  });
}

start().catch((e) => {
  console.error('start failed:', e.message);
  setTimeout(start, 8000);
});

setInterval(() => {
  console.log('alive ' + new Date().toISOString());
}, 120000);
