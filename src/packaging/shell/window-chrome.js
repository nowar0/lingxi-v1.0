'use strict';

// The calling top-level UI can control only its own window.
module.exports = ({ BrowserWindow, ipcMain, getOrigin }) => {
  const watched = new WeakSet();
  const state = win => ({ maximized: win.isMaximized(), fullscreen: win.isFullScreen() });
  const owner = event => {
    const sender = event.sender;
    if (!sender || sender.isDestroyed() || !event.senderFrame || event.senderFrame !== sender.mainFrame) throw new Error('窗口请求来源无效');
    const url = new URL(event.senderFrame.url);
    if (url.origin !== getOrigin() || !['/', '/ui/', '/ui/index.html'].includes(url.pathname)) throw new Error('窗口请求来源无效');
    const win = BrowserWindow.fromWebContents(sender);
    if (!win || win.isDestroyed()) throw new Error('窗口已关闭');
    return win;
  };
  ipcMain.handle('lingxi:window:state', event => state(owner(event)));
  ipcMain.handle('lingxi:window:action', (event, action) => {
    const win = owner(event);
    if (action === 'minimize') win.minimize();
    else if (action === 'toggle-maximize') {
      if (win.isFullScreen()) win.setFullScreen(false);
      else if (win.isMaximized()) win.unmaximize();
      else win.maximize();
    } else if (action === 'close') win.close();
    else throw new Error('不支持的窗口操作');
    return win.isDestroyed() ? null : state(win);
  });
  return { attach(win) {
    if (watched.has(win)) return;
    watched.add(win);
    const publish = () => { if (!win.isDestroyed() && !win.webContents.isDestroyed()) win.webContents.send('lingxi:window:changed', state(win)); };
    const events = ['maximize', 'unmaximize', 'enter-full-screen', 'leave-full-screen'];
    events.forEach(name => win.on(name, publish));
    win.once('closed', () => events.forEach(name => win.removeListener(name, publish)));
  } };
};
