'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

module.exports = function selectBackend(original, log = () => {}) {
  const folder = path.dirname(original);
  const pending = path.join(folder, 'LingxiServer.update.exe');
  const manifest = path.join(folder, 'message-actions-update.json');
  if (!fs.existsSync(pending) || !fs.existsSync(manifest)) return original;
  const expected = JSON.parse(fs.readFileSync(manifest, 'utf8')).sha256;
  const actual = crypto.createHash('sha256').update(fs.readFileSync(pending)).digest('hex');
  if (!/^[a-f0-9]{64}$/.test(expected) || actual !== expected) throw Error('待应用的消息功能更新校验失败，请重新安装更新');
  try {
    fs.renameSync(pending, original);
    fs.unlinkSync(manifest);
    log('消息功能后端更新已应用');
    return original;
  } catch (error) {
    if (!['EPERM', 'EACCES', 'EBUSY'].includes(error.code)) throw error;
    // A previous process or virus scan may still hold the old executable. Both
    // executables use the same verified dependency directory and data lock.
    log('原后端仍被占用，使用已校验的消息功能后端');
    return pending;
  }
};
