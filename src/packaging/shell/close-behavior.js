'use strict';
const fs=require('node:fs'),path=require('node:path');
module.exports=function createCloseBehavior({app,BrowserWindow,Tray,Menu,ipcMain,dialog,getWindow,dataRoot,appDir,icon,isQuitting,onQuit,getOrigin}){
 const preferenceFile=path.join(dataRoot,'shell','close-behavior.json');
 let chooser=null,tray=null,pending=null;
 function read(){try{const p=JSON.parse(fs.readFileSync(preferenceFile,'utf8'));return ['ask','tray','exit'].includes(p.action)?p.action:'ask'}catch{return 'ask'}}
 function write(action){if(!['ask','tray','exit'].includes(action))throw Error('关闭选项无效');fs.mkdirSync(path.dirname(preferenceFile),{recursive:true});const tmp=preferenceFile+'.tmp';fs.writeFileSync(tmp,JSON.stringify({action}));fs.renameSync(tmp,preferenceFile);return action}
 function restore(){const win=getWindow();if(win&&!win.isDestroyed()){if(win.isMinimized())win.restore();win.show();win.focus()}}
 function exit(){if(chooser&&!chooser.isDestroyed())chooser.destroy();chooser=null;onQuit()}
 function ensureTray(){
  if(tray)return
  tray=new Tray(icon);tray.setToolTip('零汐 · 正在后台运行');tray.on('double-click',restore);tray.on('click',restore)
  tray.setContextMenu(Menu.buildFromTemplate([{label:'打开零汐',click:restore},{type:'separator'},{label:'关闭窗口时每次询问',click:()=>{write('ask');restore()}},{label:'关闭窗口时最小化到托盘',click:()=>write('tray')},{label:'关闭窗口时退出程序',click:()=>write('exit')},{type:'separator'},{label:'退出程序',click:exit}]))
 }
 function minimize(){ensureTray();const win=getWindow();if(win&&!win.isDestroyed())win.hide()}
 function trusted(event){try{return new URL(event.senderFrame.url).origin===getOrigin()&&new URL(event.senderFrame.url).pathname==='/ui/'&&event.sender.session===getWindow()?.webContents.session}catch{return false}}
 ipcMain.handle('lingxi:close:preference',(event,action)=>{if(!trusted(event))throw Error('关闭设置来源无效');return {action:action===undefined?read():write(action)}})
 ipcMain.handle('lingxi:close:choose',async(event,body)=>{
  if(!chooser||event.sender!==chooser.webContents||!body||!['tray','exit','cancel'].includes(body.action)||typeof body.remember!=='boolean')throw Error('关闭请求无效')
  if(body.action==='cancel'){chooser.close();return {cancelled:true}}
  try{if(body.action==='tray')ensureTray();write(body.remember?body.action:'ask')}catch(error){await dialog.showMessageBox(chooser,{type:'error',message:'未能保存关闭选项',detail:'请重试。窗口保持打开。'});return {error:true}}
  const action=body.action;chooser.destroy();chooser=null;pending?.();pending=null
  if(action==='tray')minimize();else exit()
  return {action}
 })
 function showChoice(){
  if(chooser&&!chooser.isDestroyed()){chooser.show();chooser.focus();return}
  const parent=getWindow();if(!parent||parent.isDestroyed())return
  chooser=new BrowserWindow({width:540,height:236,useContentSize:true,resizable:false,minimizable:false,maximizable:false,frame:false,modal:true,parent,show:false,title:'关闭零汐',backgroundColor:'#ffffff',icon,webPreferences:{preload:path.join(appDir,'preload.js'),contextIsolation:true,nodeIntegration:false,sandbox:true}})
  chooser.removeMenu();chooser.webContents.setWindowOpenHandler(()=>({action:'deny'}));chooser.webContents.on('will-navigate',event=>event.preventDefault());chooser.webContents.on('will-attach-webview',event=>event.preventDefault())
  chooser.once('ready-to-show',()=>chooser?.show());chooser.once('closed',()=>{chooser=null;pending?.();pending=null})
  chooser.loadFile(path.join(appDir,'close-choice.html')).catch(()=>{chooser?.destroy();chooser=null})
 }
 function onClose(event){if(isQuitting())return;event.preventDefault();if(read()==='exit'){exit();return}if(read()==='tray'){try{minimize();return}catch{write('ask')}}showChoice()}
 function destroy(){chooser?.destroy();chooser=null;tray?.destroy();tray=null}
 return {onClose,restore,destroy,read,write,minimize,exit,hasTray:()=>!!tray};
};
