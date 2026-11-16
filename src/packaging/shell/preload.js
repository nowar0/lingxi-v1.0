// 桌面窗口与本地服务的桥接：文件夹选择仅提供只读目录名称。
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  selectPluginStageSource: (request) => ipcRenderer.invoke('lingxi:plugin-stage:select', request),
});

contextBridge.exposeInMainWorld('LingxiShell', {
  isDesktop: true,
  hostSession: () => ipcRenderer.invoke('lingxi:auth:bootstrap'),
  browseFolders: path => ipcRenderer.invoke('lingxi:folder:browse', path),
  setUnsaved:dirty=>ipcRenderer.send('lingxi:unsaved',dirty),
  recovery:{read:key=>ipcRenderer.sendSync('lingxi:recovery',{action:'read',key}),set:(key,value)=>ipcRenderer.sendSync('lingxi:recovery',{action:'set',key,value})},
  dataLocation: {
    state: () => ipcRenderer.invoke('lingxi:data:state'),
    switch: request => ipcRenderer.invoke('lingxi:data:switch',request),
  },
  windowChrome: {
    state: () => ipcRenderer.invoke('lingxi:window:state'),
    action: action => ipcRenderer.invoke('lingxi:window:action', action),
    onChange: listener => {
      const handle = (_event, value) => listener(value);
      ipcRenderer.on('lingxi:window:changed', handle);
      return () => ipcRenderer.removeListener('lingxi:window:changed', handle);
    },
  },
  closeChoice: request=>ipcRenderer.invoke('lingxi:close:choose',request),
  closePreference: action=>ipcRenderer.invoke('lingxi:close:preference',action),
  selectPluginStageSource: (request) => ipcRenderer.invoke('lingxi:plugin-stage:select', request),
  messageImages: {
    copy: (request) => ipcRenderer.invoke('lingxi:image:copy', request),
    save: (request) => ipcRenderer.invoke('lingxi:image:save', request),
    reveal: (request) => ipcRenderer.invoke('lingxi:image:reveal', request),
  },
  platform: process.platform,
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
  },
});
