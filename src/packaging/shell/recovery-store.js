"use strict";
const fs = require('node:fs');
const path = require('node:path');
const trustedUi = require('./trusted-ui');
module.exports = ({ ipcMain, getWindow, getOrigin, dataRoot, BrowserWindow }) => {
  const file = path.join(dataRoot, 'shell', 'composer-recovery.json');
  const valid = key => typeof key === 'string' && /^lingxi-(outbox:|draft:|pending-attachments-v1:)/.test(key) && key.length < 160;
  let entries = {};
  try { const value = JSON.parse(fs.readFileSync(file, 'utf8')); if (value && typeof value === 'object' && !Array.isArray(value)) entries = value; } catch {}
  let timer;
  const dirtyWindows = new Set();
  const watched = new WeakSet();
  const owner = event => trustedUi(event, { BrowserWindow, getWindow, getOrigin });
  ipcMain.on('lingxi:unsaved', (event, value) => {
    try {
      const win = owner(event);
      if (!watched.has(win)) { watched.add(win); win.once('closed', () => dirtyWindows.delete(win)); }
      if (value === true) dirtyWindows.add(win); else dirtyWindows.delete(win);
    } catch {}
  });
  const flush = () => {
    clearTimeout(timer); timer = undefined;
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const temporary = file + '.tmp';
    const fd = fs.openSync(temporary, 'w');
    try { fs.writeFileSync(fd, JSON.stringify(entries)); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
    fs.renameSync(temporary, file);
  };
  ipcMain.on('lingxi:recovery', (event, request) => {
    try {
      owner(event);
      if (request?.action === 'read') {
        if (request.key !== undefined && !valid(request.key)) throw Error('恢复内容无效');
        event.returnValue = request.key === undefined ? { ...entries } : { [request.key]: entries[request.key] ?? null };
        return;
      }
      if (request?.action !== 'set' || !valid(request.key) || (request.value !== null && (typeof request.value !== 'string' || request.value.length > 150000))) throw Error('恢复内容无效');
      const next = { ...entries };
      if (request.value === null) delete next[request.key]; else next[request.key] = request.value;
      if (Buffer.byteLength(JSON.stringify(next)) > 8 * 1024 * 1024) throw Error('恢复区已满');
      entries = next;
      clearTimeout(timer); timer = setTimeout(() => { try { flush(); } catch {} }, 200);
      event.returnValue = { ok: true };
    } catch (error) { event.returnValue = { error: error.message }; }
  });
  return { flush, hasUnsaved: () => dirtyWindows.size > 0 };
};
