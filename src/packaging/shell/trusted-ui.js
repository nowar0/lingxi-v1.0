'use strict';

// Only an app-owned, top-level Lingxi UI may use desktop capabilities.
module.exports = function trustedUi(event, { BrowserWindow, getOrigin, getWindow }) {
  const sender = event.sender;
  if (!sender || sender.isDestroyed?.() || !event.senderFrame || event.senderFrame !== sender.mainFrame) throw Error('请求窗口不匹配');
  const url = new URL(event.senderFrame.url);
  if (url.origin !== getOrigin() || !['/ui/', '/ui/index.html'].includes(url.pathname)) throw Error('页面来源不匹配');
  const win = BrowserWindow ? BrowserWindow.fromWebContents(sender) : getWindow?.();
  if (!win || win.isDestroyed?.() || win.webContents !== sender) throw Error('请求窗口不匹配');
  return win;
};
