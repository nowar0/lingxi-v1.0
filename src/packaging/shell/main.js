'use strict';

/**
 * 零汐桌面外壳。
 *
 * 启动顺序：
 *   1. 找一个空闲回环端口，把本地服务（resources/backend/LingxiServer.exe）拉起来；
 *      stdio 全部接管，窗口隐藏，任务管理器里只有一个「零汐」窗口；
 *   2. 轮询 /health 直到服务真的可用，再创建主窗口并载入 http://127.0.0.1:<端口>/ui/；
 *   3. 关闭时选择托盘运行或退出；退出时结束服务进程树。
 *
 * 想改外观或行为，直接编辑本文件 / splash.html：无需重新编译。
 * 启动过程会写进 %APPDATA%\LingxiV1\logs\启动跟踪.log，排查问题先看它。
 */

const { app, BrowserWindow, Tray, Menu, dialog, shell, session, ipcMain, screen } = require('electron');
const { spawn } = require('node:child_process');
const http = require('node:http');
const net = require('node:net');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { randomBytes } = require('node:crypto');
const UI_BOOTSTRAP = randomBytes(32).toString('base64url');
const SOURCE_BRIDGE_SECRET = randomBytes(32).toString('base64url');

const APP_TITLE = '零汐';
const APP_DIR = app.getAppPath();                        // ...\resources\app
const RES_DIR = path.dirname(APP_DIR);                   // ...\resources
let BACKEND_EXE = path.join(RES_DIR, 'backend', 'LingxiServer.exe');
const ICON_ICO = path.join(APP_DIR, 'assets', 'lingxi.ico');
const SPLASH_HTML = path.join(APP_DIR, 'splash.html');

// 数据独立于程序；两端用同一绝对目录，避免“隔离外壳却打开正式数据库”。
const explicitDataRoot = process.env.LINGXI_DATA_DIR || process.env.LINGXI_SHELL_HOME;
const DATA_ANCHOR_ROOT = path.resolve(explicitDataRoot || path.join(process.env.APPDATA || app.getPath('appData'), 'LingxiV1'));
let DATA_ROOT;
try {
  DATA_ROOT = require('./data-location').readLocation(DATA_ANCHOR_ROOT);
} catch (error) {
  dialog.showErrorBox('零汐无法打开数据目录', `${error.message}\n\n本次启动已停止，不会切换到其他数据目录。`);
  app.exit(6);
  return;
}
process.env.LINGXI_DATA_DIR = DATA_ROOT;
process.env.LINGXI_BUNDLED_MODEL_ROOT = path.resolve(RES_DIR, '../models');
// An explicitly isolated shell must isolate Chromium preferences and storage too.
{
  const isolatedBrowserData = path.join(DATA_ROOT, 'chromium');
  fs.mkdirSync(isolatedBrowserData, { recursive: true });
  app.setPath('userData', isolatedBrowserData);
}
const LOG_DIR = path.join(DATA_ROOT, 'logs');
const LOG_FILE = path.join(LOG_DIR, '窗口.log');
const TRACE_FILE = path.join(LOG_DIR, '启动跟踪.log');
const STATE_FILE = path.join(DATA_ROOT, 'shell', 'window-state.json');
const SMOKE = process.env.LINGXI_SMOKE === '1' || process.argv.includes('--smoke');
const SMOKE_OUT = process.env.LINGXI_SMOKE_OUT || path.join(DATA_ROOT, 'smoke.png');

let backend = null;
let backendPort = 0;
let logStream = null;
let mainWindow = null;
let splashWindow = null;
let quitting = false;
let backendLostDialog = false;
let shuttingDown=false, shutdownFinished=false;
const recoveryStore=require('./recovery-store')({ipcMain,BrowserWindow,getWindow:()=>mainWindow,getOrigin:()=>`http://127.0.0.1:${backendPort}`,dataRoot:DATA_ROOT});
require('./host-session')({ipcMain,BrowserWindow,getOrigin:()=>`http://127.0.0.1:${backendPort}`,getBootstrap:()=>UI_BOOTSTRAP});
require('./data-location').register({app,ipcMain,getWindow:()=>mainWindow,getPort:()=>backendPort,secret:SOURCE_BRIDGE_SECRET,anchor:DATA_ANCHOR_ROOT,current:DATA_ROOT,onQuit:()=>{quitting=true;app.quit()}});
const closeBehavior=require('./close-behavior')({app,BrowserWindow,Tray,Menu,ipcMain,dialog,getWindow:()=>mainWindow,dataRoot:DATA_ROOT,appDir:APP_DIR,icon:ICON_ICO,isQuitting:()=>quitting,onQuit:()=>{quitting=true;app.quit()},getOrigin:()=>`http://127.0.0.1:${backendPort}`});
const windowChrome=require('./window-chrome')({BrowserWindow,ipcMain,getOrigin:()=>`http://127.0.0.1:${backendPort}`});
require('./message-actions')({ BrowserWindow, getWindow: () => mainWindow, getPort: () => backendPort, dataRoot: DATA_ROOT });

// Folder names for the app-owned workspace picker. Only the trusted top-level UI can browse.
ipcMain.handle('lingxi:folder:browse', async (event, requestedPath) => {
  const owner = BrowserWindow.fromWebContents(event.sender);
  const base = `http://127.0.0.1:${backendPort}`;
  if (!owner || owner.isDestroyed() || !backendPort || event.senderFrame !== event.sender.mainFrame ||
      new URL(event.senderFrame.url).origin !== base || typeof requestedPath !== 'string' || requestedPath.length > 2000) {
    throw new Error('文件夹选择请求无效');
  }
  if (!requestedPath.trim()) {
    const roots = process.platform === 'win32'
      ? await Promise.all('ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split('').map(async letter => {
          const drive = `${letter}:\\`;
          try {
            const available = await Promise.race([
              fs.promises.stat(drive),
              new Promise((_, reject) => setTimeout(() => reject(new Error('磁盘响应超时')), 1500)),
            ]);
            return available.isDirectory() ? { name: drive, path: drive } : null;
          } catch { return null; }
        }))
      : [{ name: '/', path: '/' }];
    return { path: '', parent: '', items: roots.filter(Boolean), truncated: false };
  }
  const raw = requestedPath.trim();
  if (!path.isAbsolute(raw)) throw new Error('请输入完整的文件夹路径');
  const folder = path.resolve(raw);
  try {
    if (!(await fs.promises.stat(folder)).isDirectory()) throw new Error('请选择文件夹');
    const items = [];
    let scanned = 0;
    const entries = await fs.promises.opendir(folder);
    for await (const entry of entries) {
      scanned++;
      if (scanned > 5000 || items.length >= 1000) break;
      if (entry.isDirectory() && !entry.isSymbolicLink()) items.push({ name: entry.name, path: path.join(folder, entry.name) });
    }
    items.sort((a, b) => a.name.localeCompare(b.name, 'zh-CN'));
    return { path: folder, parent: path.dirname(folder) === folder ? '' : path.dirname(folder),
      items: items.slice(0, 300), truncated: items.length > 300 || scanned > 5000 };
  } catch (error) {
    if (error && error.message === '请选择文件夹') throw error;
    throw new Error('无法读取这个文件夹，请选择其他目录');
  }
});

// ---------------------------------------------------------------- 痕迹记录

/** 最早的启动痕迹：此时 Electron 还没就绪，任何 API 都可能抛错，所以全部吞掉异常。 */
function trace(message) {
  const line = `[${new Date().toISOString()}] ${message}\n`;
  const targets = [TRACE_FILE, path.join(path.dirname(process.execPath), '启动跟踪.log')];
  for (const file of targets) {
    try {
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.appendFileSync(file, line);
      return;
    } catch { /* 换下一个落点 */ }
  }
}

function log(text) {
  if (logStream) logStream.write(text + '\n');
  trace(text);
}

process.on('uncaughtException', (err) => {
  trace('未捕获异常: ' + (err && err.stack ? err.stack : String(err)));
});
process.on('unhandledRejection', (err) => {
  trace('未处理的 Promise: ' + (err && err.stack ? err.stack : String(err)));
});

trace(`外壳载入 execPath=${process.execPath}`);
trace(`electron=${process.versions.electron} chrome=${process.versions.chrome} node=${process.versions.node}`);
trace(`argv=${JSON.stringify(process.argv.slice(1))}`);
trace(`APPDATA=${process.env.APPDATA || '(空)'} ELECTRON_RUN_AS_NODE=[${process.env.ELECTRON_RUN_AS_NODE || ''}]`);

// ---------------------------------------------------------------- 窗口日志

function openLog() {
  try {
    fs.mkdirSync(LOG_DIR, { recursive: true });
    try {
      if (fs.statSync(LOG_FILE).size > 512 * 1024) fs.truncateSync(LOG_FILE, 0);
    } catch { /* 首次运行没有日志文件 */ }
    logStream = fs.createWriteStream(LOG_FILE, { flags: 'a' });
    logStream.write(`\n===== ${new Date().toISOString()} 启动 =====\n`);
  } catch (err) {
    logStream = null;
    trace('窗口日志打不开: ' + err.message);
  }
}

// ---------------------------------------------------------------- 本地服务

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

function probe(port) {
  return new Promise((resolve) => {
    const req = http.get({ host: '127.0.0.1', port, path: '/health', timeout: 900 }, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on('timeout', () => { req.destroy(); resolve(false); });
    req.on('error', () => resolve(false));
  });
}

/** 拉起服务进程；优先接管 stdio 写日志，管道不可用时退回静默模式。 */
async function spawnProcess(piped) {
  const child = spawn(BACKEND_EXE, ['--port', String(backendPort), '--no-browser'], {
    cwd: path.dirname(BACKEND_EXE),
    windowsHide: true,
    env: { ...process.env, LINGXI_UI_BOOTSTRAP: UI_BOOTSTRAP, LINGXI_SOURCE_BRIDGE_SECRET: SOURCE_BRIDGE_SECRET },
    stdio: piped ? ['ignore', 'pipe', 'pipe'] : 'ignore',
  });
  const alive = await new Promise((resolve) => {
    child.once('error', (err) => { log(`[spawn] ${err.code || ''} ${err.message}`); resolve(false); });
    child.once('spawn', () => resolve(true));
  });
  if (!alive) return null;
  if (piped) {
    // 只留异常与启动信息，不把每次 HTTP 访问都写进日志。
    const isAccessLog = /"(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) [^"]* HTTP\/1\.1" \d{3}/;
    const keep = (chunk) => chunk.toString().split('\n')
      .filter((line) => line.trim() && !isAccessLog.test(line))
      .forEach((line) => log('[服务] ' + line.trimEnd()));
    child.stdout.on('data', keep);
    child.stderr.on('data', keep);
  }
  return child;
}

async function startBackend() {
  BACKEND_EXE = require('./backend-update')(BACKEND_EXE, log);
  if (!fs.existsSync(BACKEND_EXE)) {
    throw new Error(`找不到本地服务：${BACKEND_EXE}`);
  }
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    backendPort = await freePort();
    log(`----- 启动服务 127.0.0.1:${backendPort}（第 ${attempt} 次） -----`);
    let child = await spawnProcess(true);
    if (!child) child = await spawnProcess(false);
    if (!child) throw new Error('本地服务进程无法启动');

    backend = child;
    child.on('exit', (code, signal) => {
      log(`[退出] code=${code} signal=${signal}`);
      if (backend === child) backend = null;
      if (!quitting) onBackendLost(code);
    });

    const ok = await waitForBackend(90000);
    if (ok) return backendPort;
    log('服务未在时限内就绪，重试');
    stopBackend();
  }
  throw new Error('本地服务多次启动失败');
}

async function waitForBackend(timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!backend) return false;
    if (await probe(backendPort)) {
      log('服务已就绪');
      return true;
    }
    await sleep(250);
  }
  return false;
}

function stopBackend() {
  const child = backend;
  backend = null;
  if (!child || child.exitCode !== null) return;
  try {
    // /T 连同工具子进程一起结束，避免留下后台残留。
    spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], {
      windowsHide: true,
      stdio: 'ignore',
    });
  } catch {
    try { child.kill(); } catch { /* 已经退出 */ }
  }
}

function showError(message, detail) {
  const options = { type: 'error', title: APP_TITLE, message, detail, buttons: ['退出'] };
  if (mainWindow && !mainWindow.isDestroyed()) dialog.showMessageBoxSync(mainWindow, options);
  else dialog.showMessageBoxSync(options);
}

async function onBackendLost(code) {
  if (backendLostDialog || shuttingDown) return;
  backendLostDialog=true;
  log(`本地服务退出：${code}`);
  if(SMOKE){app.exit(3);return}
  try{
    const choice=await dialog.showMessageBox(mainWindow,{type:'error',title:APP_TITLE,message:'本地服务已断开',detail:'可以重新连接。已保存的对话保留，任务不会自动重新执行。',buttons:['重新连接','退出'],cancelId:1});
    if(choice.response===0){try{await startBackend();await mainWindow.loadURL(`http://127.0.0.1:${backendPort}/ui/#lingxi-bootstrap=${UI_BOOTSTRAP}`)}catch(error){showError('重新连接失败',error.message);quitting=true;app.quit()}}
    else{quitting=true;app.quit()}
  }finally{backendLostDialog=false}
}

async function shutdownBackend(){
  recoveryStore.flush();
  const child=backend;if(!child||child.exitCode!==null)return;
  await new Promise(resolve=>{
    const request=http.request({host:'127.0.0.1',port:backendPort,path:'/v11/desktop/shutdown',method:'POST',timeout:2000,headers:{'X-Lingxi-Source-Bridge':SOURCE_BRIDGE_SECRET}},response=>{response.resume();response.on('end',resolve)});
    request.on('error',resolve);request.on('timeout',()=>{request.destroy();resolve()});request.end();
  });
  await Promise.race([new Promise(resolve=>{if(child.exitCode!==null)resolve();else child.once('exit',resolve)}),sleep(12000)]);
  if(child.exitCode===null)stopBackend();
}

// Native source selection returns only backend-held source IDs to the renderer.
let selectingStageSource = false;
ipcMain.handle('lingxi:plugin-stage:select', async (event, request) => {
  if (selectingStageSource || !mainWindow || event.sender !== mainWindow.webContents || !backendPort) throw new Error('本机选择器暂不可用');
  const base = `http://127.0.0.1:${backendPort}`;
  if (new URL(event.senderFrame.url).origin !== base || !request || !['candidate', 'compatibility_evidence'].includes(request.kind) || !/^[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9_-]*)+$/.test(request.plugin_id)) throw new Error('选择请求无效');
  selectingStageSource = true;
  try {
    const choice = request.kind === 'candidate'
      ? await dialog.showOpenDialog(mainWindow, { title: '选择迁移候选插件包（不执行）', properties: ['openFile', 'openDirectory'], filters: [{ name: '插件 ZIP', extensions: ['zip'] }] })
      : await dialog.showOpenDialog(mainWindow, { title: '选择兼容性审计证据（只读）', properties: ['openFile'], filters: [{ name: '审计证据', extensions: ['txt', 'md', 'json'] }] });
    if (choice.canceled || choice.filePaths.length !== 1) return null;
    const cookies = await event.sender.session.cookies.get({ url: base, name: 'lingxi_ui_session' });
    if (cookies.length !== 1) throw new Error('本地会话未认证，请重新打开零汐');
    const payload = JSON.stringify({ path: choice.filePaths[0], kind: request.kind });
    return await new Promise((resolve, reject) => {
      const connection = http.request(`${base}/v11/desktop-source/register`, { method: 'POST', timeout: 30000,
        headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload),
          Origin: base, Cookie: `lingxi_ui_session=${cookies[0].value}`, 'X-Lingxi-Source-Bridge': SOURCE_BRIDGE_SECRET } }, response => {
        const chunks = []; let length = 0;
        response.on('data', chunk => { length += chunk.length; if (length > 32768) connection.destroy(new Error('选择结果超限')); else chunks.push(chunk); });
        response.on('end', () => { if (response.statusCode !== 200) { reject(new Error('宿主拒绝来源登记；没有执行迁移')); return; } try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))); } catch { reject(new Error('来源登记响应无效')); } });
      });
      connection.on('timeout', () => connection.destroy(new Error('来源登记超时；不自动重试')));
      connection.on('error', () => reject(new Error('来源登记未确认；不自动重试')));
      connection.end(payload);
    });
  } finally { selectingStageSource = false; }
});

// ---------------------------------------------------------------- 窗口

function readWindowState() {
  const fallback = { width: 1420, height: 940 };
  let saved = fallback;
  try {
    const value = JSON.parse(fs.readFileSync(STATE_FILE, 'utf8'));
    if (value && Number.isFinite(value.width) && Number.isFinite(value.height)) saved = value;
  } catch {}
  const display = Number.isFinite(saved.x) && Number.isFinite(saved.y) ? screen.getDisplayMatching(saved) : screen.getPrimaryDisplay();
  return require('./window-bounds')(saved, display.workArea);
}

function saveWindowState() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (mainWindow.isMinimized() || mainWindow.isFullScreen()) return;
  const bounds = mainWindow.getNormalBounds();
  try {
    fs.mkdirSync(path.dirname(STATE_FILE), { recursive: true });
    fs.writeFileSync(STATE_FILE, JSON.stringify(bounds));
  } catch { /* 保存失败不影响使用 */ }
}

function createSplash() {
  splashWindow = new BrowserWindow({
    width: 360,
    height: 268,
    frame: false,
    transparent: true,
    resizable: false,
    movable: true,
    show: false,
    center: true,
    icon: ICON_ICO,
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  splashWindow.once('ready-to-show', () => {
    if (splashWindow && !splashWindow.isDestroyed()) splashWindow.show();
  });
  splashWindow.loadFile(SPLASH_HTML).catch((err) => trace('启动画面载入失败: ' + err.message));
}

function closeSplash() {
  if (splashWindow && !splashWindow.isDestroyed()) splashWindow.destroy();
  splashWindow = null;
}

function hardenWindow(contents, base) {
  contents.on('will-prevent-unload',event=>{
    if(shuttingDown){event.preventDefault();return;}
    const owner=BrowserWindow.fromWebContents(contents);
    if(!owner||owner.isDestroyed())return;
    const response=dialog.showMessageBoxSync(owner,{type:'question',title:APP_TITLE,message:'放弃未保存的修改？',buttons:['继续编辑','放弃修改'],cancelId:0,defaultId:0});
    if(response===1)event.preventDefault();
  });
  contents.setWindowOpenHandler(({ url }) => {
    try {
      const offered = new URL(url);
      if (offered.origin === base && offered.pathname === '/ui/' && /^#\/work\?session=[1-9][0-9]*$/.test(offered.hash)) {
        return { action: 'allow', overrideBrowserWindowOptions: {
          ...require('./window-bounds')({width:1360,height:900},screen.getPrimaryDisplay().workArea), title: APP_TITLE, frame: false,
          icon: ICON_ICO, autoHideMenuBar: true,
          webPreferences: { preload: path.join(APP_DIR, 'preload.js'), contextIsolation: true,
            nodeIntegration: false, sandbox: true, spellcheck: false, navigateOnDragDrop: false, partition: 'persist:lingxi' },
        }};
      }
      if (/^https?:$/.test(offered.protocol) && offered.origin !== base) shell.openExternal(url);
    } catch { /* Invalid window targets never navigate. */ }
    return { action: 'deny' };
  });
  contents.on('did-create-window', child => {
    windowChrome.attach(child);
    child.removeMenu();
    hardenWindow(child.webContents, base);
    child.on('page-title-updated', event => event.preventDefault());
  });
  contents.on('will-navigate', (event, url) => {
    try { if (new URL(url).origin === base) return; } catch { /* Invalid URLs never navigate. */ }
    event.preventDefault();
    if (/^https?:/i.test(url)) shell.openExternal(url);
  });
  contents.on('will-attach-webview', (event) => event.preventDefault());
}

function createMainWindow(port) {
  const base = `http://127.0.0.1:${port}`;
  const state = readWindowState();

  mainWindow = new BrowserWindow({
    ...state,
    show: false,
    title: APP_TITLE,
    frame: false,
    backgroundColor: '#eef2f7',
    icon: ICON_ICO,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(APP_DIR, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: false,
      navigateOnDragDrop: false,
      partition: 'persist:lingxi',
    },
  });
  trace('主窗口已创建');

  hardenWindow(mainWindow.webContents, base);
  windowChrome.attach(mainWindow);
  mainWindow.removeMenu();
  // 页面自己的 <title> 不覆盖窗口标题，任务栏里始终是「零汐」。
  mainWindow.on('page-title-updated', (event) => event.preventDefault());

  mainWindow.webContents.on('before-input-event', (event, input) => {
    if (input.type !== 'keyDown' || !(input.control || input.meta)) return;
    const key = input.key.toLowerCase();
    if (input.shift && key === 'i') {
      mainWindow.webContents.toggleDevTools();
      event.preventDefault();
      return;
    }
    if (key === '=' || key === '+') {
      mainWindow.webContents.setZoomFactor(Math.min(2, mainWindow.webContents.getZoomFactor() + 0.1));
      event.preventDefault();
    } else if (key === '-') {
      mainWindow.webContents.setZoomFactor(Math.max(0.6, mainWindow.webContents.getZoomFactor() - 0.1));
      event.preventDefault();
    } else if (key === '0') {
      mainWindow.webContents.setZoomFactor(1);
      event.preventDefault();
    }
  });

  mainWindow.once('ready-to-show', () => {
    trace('主窗口可显示');
    closeSplash();
    mainWindow.show();
    if (SMOKE) runSmoke();
  });

  mainWindow.webContents.on('did-finish-load', () => trace('页面载入完成'));
  mainWindow.on('close', event=>{saveWindowState();closeBehavior.onClose(event)});
  mainWindow.on('closed', () => { mainWindow = null; });
  mainWindow.webContents.on('render-process-gone', (_event, details) => {
    log('[渲染进程] ' + JSON.stringify(details));
    if(!quitting&&!SMOKE)void dialog.showMessageBox(mainWindow,{type:'error',title:APP_TITLE,message:'界面暂时无法显示',detail:'可以重新加载界面。已保存内容保留，后台任务不会重复启动。',buttons:['重新加载','退出'],cancelId:1}).then(choice=>{if(choice.response===0)mainWindow.webContents.reloadIgnoringCache();else{quitting=true;app.quit()}});
  });
  mainWindow.webContents.on('did-fail-load', (_event, code, description) => {
    log(`[载入失败] ${code} ${description}`);
    if (SMOKE) { app.exit(4); return; }
    showError('页面没有加载成功', `错误 ${code}：${description}\n日志：${LOG_FILE}`);
    app.quit();
  });

  mainWindow.loadURL(`${base}/ui/#lingxi-bootstrap=${UI_BOOTSTRAP}`).catch(() => trace('loadURL 失败（地址与启动凭据不写日志）'));
}

/** 打包自检：截一张窗口图后退出，用来确认窗口真的渲染出了界面。 */
async function runSmoke() {
  try {
    await sleep(3500);
    const image = await mainWindow.webContents.capturePage();
    fs.mkdirSync(path.dirname(SMOKE_OUT), { recursive: true });
    fs.writeFileSync(SMOKE_OUT, image.toPNG());
    trace('[自检] 已保存 ' + SMOKE_OUT);
  } catch (err) {
    trace('[自检] 失败 ' + err.message);
  } finally {
    quitting = true;
    stopBackend();
    app.exit(0);
  }
}

// ---------------------------------------------------------------- 生命周期

function bootstrap() {
  openLog();
  log(`外壳启动 / Electron ${process.versions.electron}`);

  app.on('web-contents-created', (_event, contents) => {
    contents.on('will-attach-webview', (event) => event.preventDefault());
  });

  app.whenReady().then(async () => {
    trace('app ready');
    Menu.setApplicationMenu(null);
    session.defaultSession.setPermissionRequestHandler((_wc, permission, callback) => {
      const allowed = new Set([
        'media',
        'clipboard-read',
        'clipboard-sanitized-write',
        'notifications',
        'fullscreen',
        'pointerLock',
      ]);
      callback(allowed.has(permission));
    });

    if (!SMOKE) createSplash();
    try {
      const port = await startBackend();
      createMainWindow(port);
    } catch (err) {
      closeSplash();
      log('[启动失败] ' + (err.stack || err.message));
      if (SMOKE) { app.exit(5); return; }
      showError('零汐没能启动', `${err.message}\n\n日志：${LOG_FILE}`);
      quitting = true;
      app.quit();
    }
  }).catch((err) => trace('whenReady 失败: ' + (err.stack || err.message)));

  app.on('second-instance', () => {
    closeBehavior.restore();
  });

  app.on('window-all-closed', () => {
    quitting = true;
    app.quit();
  });

  app.on('before-quit', event => {
    if(shutdownFinished)return;
    event.preventDefault();
    if(shuttingDown)return;
    if(recoveryStore.hasUnsaved()&&mainWindow&&!mainWindow.isDestroyed()){const choice=dialog.showMessageBoxSync(mainWindow,{type:'question',title:APP_TITLE,message:'放弃未保存的修改并退出？',buttons:['继续编辑','放弃并退出'],cancelId:0,defaultId:0});if(choice!==1){quitting=false;return}}
    shuttingDown=true;quitting=true;closeSplash();closeBehavior.destroy();
    void shutdownBackend().catch(error=>log('正常退出未完成：'+error.message)).finally(()=>{shutdownFinished=true;app.quit()});
  });

  app.on('will-quit', () => {
    stopBackend();
    trace('退出');
    if (logStream) { try { logStream.end(); } catch { /* 忽略 */ } }
  });
}

// 单实例：已有窗口就把它叫到前面，本进程退出。
let gotLock = true;
try {
  gotLock = SMOKE ? true : app.requestSingleInstanceLock();
  trace('单实例锁=' + gotLock);
} catch (err) {
  trace('单实例锁异常: ' + err.message);
}

if (!gotLock) {
  trace('已有实例在运行，本进程退出');
  app.quit();
} else {
  bootstrap();
}
