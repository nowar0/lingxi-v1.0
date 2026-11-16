'use strict';
const { ipcMain, clipboard, nativeImage, dialog, shell, app } = require('electron');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');

module.exports = function installMessageActions({ getWindow, getPort, dataRoot, BrowserWindow }) {
  const saved = new Map();
  const types = { 'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp' };
  function validate(event, request) {
    const origin = `http://127.0.0.1:${getPort()}`;
    const win = require('./trusted-ui')(event, { BrowserWindow, getWindow, getOrigin: () => origin });
    if (!request || !Number.isSafeInteger(request.sessionId) || request.sessionId <= 0 || !/^[a-f0-9]{32}$/.test(request.mediaId)) throw Error('无效的图片编号');
    return win;
  }
  function readImage(request) {
    return new Promise((resolve, reject) => {
      const req = http.get({hostname:'127.0.0.1',port:getPort(),path:`/v6/media/${request.mediaId}?session_id=${request.sessionId}`,headers:{Origin:`http://127.0.0.1:${getPort()}`}}, response => {
        const mime = (response.headers['content-type'] || '').split(';')[0];
        if (response.statusCode !== 200 || !types[mime]) { response.resume(); reject(Error('此会话没有可用的图片')); return; }
        let size = 0; const chunks = [];
        response.on('data', chunk => { size += chunk.length; if (size > 15 * 1024 * 1024) { response.destroy(); reject(Error('图片超过 15 MiB')); } else chunks.push(chunk); });
        response.on('error', reject);
        response.on('end', () => resolve({buffer:Buffer.concat(chunks),mime,extension:types[mime]}));
      });
      req.setTimeout(15000, () => req.destroy(Error('图片读取超时')));
      req.on('error', reject);
    });
  }
  for (const action of ['copy', 'save', 'reveal']) ipcMain.handle(`lingxi:image:${action}`, async (event, request) => {
    try {
      const win = validate(event, request);
      const image = await readImage(request);
      const key = `${request.sessionId}:${request.mediaId}`;
      if (action === 'copy') {
        const value = nativeImage.createFromBuffer(image.buffer);
        if (value.isEmpty()) throw Error('图片无法复制');
        clipboard.writeImage(value);
        return {ok:true};
      }
      if (action === 'save') {
        const result = await dialog.showSaveDialog(win, {title:'图片另存为',defaultPath:path.join(app.getPath('pictures'), `零汐图片-${request.mediaId.slice(0,8)}${image.extension}`),filters:[{name:'图片',extensions:[image.extension.slice(1)]}]});
        if (result.canceled || !result.filePath) return {ok:true,cancelled:true};
        await fs.writeFile(result.filePath, image.buffer);
        saved.set(key, result.filePath);
        return {ok:true};
      }
      let file = saved.get(key);
      if (file) { try { await fs.access(file); } catch { file = undefined; } }
      if (!file) {
        const directory = await fs.realpath(path.join(dataRoot, 'attachments'));
        file = await fs.realpath(path.join(directory, request.mediaId + image.extension));
        if (!file.startsWith(directory + path.sep)) throw Error('图片路径不匹配');
      }
      shell.showItemInFolder(file);
      return {ok:true};
    } catch (error) { return {ok:false,error:error.message || '图片操作未完成'}; }
  });
};
