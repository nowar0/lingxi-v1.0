'use strict';
const trustedUi = require('./trusted-ui');
module.exports = ({ ipcMain, BrowserWindow, getOrigin, getBootstrap }) => {
  ipcMain.handle('lingxi:auth:bootstrap', event => {
    trustedUi(event, { BrowserWindow, getOrigin });
    return { token: getBootstrap() };
  });
};
