'use strict';
const fs=require('node:fs'),path=require('node:path'),http=require('node:http');
function safeDirectory(value){
 if(typeof value!=='string'||!value.trim()||!path.isAbsolute(value))throw Error('数据目录必须是完整的绝对路径');
 const resolved=path.resolve(value);let part=resolved;
 while(true){if(fs.lstatSync(part).isSymbolicLink())throw Error('数据目录不允许链接');const parent=path.dirname(part);if(parent===part)break;part=parent}
 if(!fs.statSync(path.join(resolved,'lingxi.db')).isFile())throw Error('没有找到已恢复的数据库');return resolved;
}
function pointer(anchor){return path.join(anchor,'shell','data-location.json')}
function readLocation(anchor){
 const file=pointer(anchor);let info;
 try{info=fs.lstatSync(file)}catch(error){
  if(error.code==='ENOENT')return anchor; // Only an absent pointer means first launch.
  throw Error(`无法读取数据目录设置。\n设置文件：${file}\n请恢复该文件的访问权限后重启。`);
 }
 let value;
 try{
  if(!info.isFile()||info.isSymbolicLink())throw Error('设置文件不是普通文件');
  value=JSON.parse(fs.readFileSync(file,'utf8'));
  if(!value||value.format!=='lingxi-data-location-v1'||typeof value.path!=='string'||!path.isAbsolute(value.path))throw Error('设置文件格式无效');
 }catch{
  throw Error(`数据目录设置已损坏或无法读取。\n设置文件：${file}\n请从备份恢复此设置文件后重启；不要删除数据目录。`);
 }
 try{return safeDirectory(value.path)}catch(error){
  const reason=error.code==='ENOENT'?'目录或数据库文件不存在':(['EACCES','EPERM'].includes(error.code)?'没有读取权限':error.message);
  throw Error(`无法打开已选择的数据目录：${value.path}\n原因：${reason}\n请确认磁盘已连接、目录可访问且 lingxi.db 仍在原位，再重新启动。\n设置文件：${file}`);
 }
}
function register({app,ipcMain,getWindow,getPort,secret,anchor,current,onQuit}){
 const trusted=event=>{const win=getWindow();try{return event.sender===win.webContents&&new URL(event.senderFrame.url).origin===`http://127.0.0.1:${getPort()}`&&new URL(event.senderFrame.url).pathname==='/ui/'}catch{return false}};
 const check=body=>new Promise((resolve,reject)=>{const raw=Buffer.from(JSON.stringify(body));const req=http.request({host:'127.0.0.1',port:getPort(),path:'/v11/desktop-source/restore-ready',method:'POST',headers:{'Content-Type':'application/json','Content-Length':raw.length,'x-lingxi-source-bridge':secret},timeout:20000},res=>{let data='';res.on('data',chunk=>{data+=chunk;if(data.length>16000)req.destroy(Error('结果过长'))});res.on('end',()=>{try{const result=JSON.parse(data);if(res.statusCode!==200)throw Error(typeof result.detail==='string'?result.detail:'恢复目录校核失败');resolve(result)}catch(e){reject(e)}})});req.on('error',reject);req.on('timeout',()=>req.destroy(Error('校核超时，请重试')));req.end(raw)});
 ipcMain.handle('lingxi:data:state',event=>{if(!trusted(event))throw Error('来源无效');let previous='';try{previous=JSON.parse(fs.readFileSync(pointer(anchor),'utf8')).previous||''}catch{}return {current,canRevert:!!previous&&previous!==current}});
 ipcMain.handle('lingxi:data:switch',async(event,body)=>{
  if(!trusted(event)||!body||body.confirmed!==true)throw Error('请确认切换数据目录');
  let selected=body.path;
  if(body.revert){selected=JSON.parse(fs.readFileSync(pointer(anchor),'utf8')).previous;safeDirectory(selected)}
  const result=await check({path:selected,revert:!!body.revert,previous:body.revert?selected:undefined});
  const target=safeDirectory(result.path);if(target===current)throw Error('当前已使用这个目录');
  const destination=pointer(anchor);fs.mkdirSync(path.dirname(destination),{recursive:true});const temporary=destination+'.writing';fs.writeFileSync(temporary,JSON.stringify({format:'lingxi-data-location-v1',path:target,previous:current}));fs.renameSync(temporary,destination);
  // Relaunch through the stable anchor, so later starts and revert use the same pointer.
  process.env.LINGXI_DATA_DIR=anchor;app.relaunch();setTimeout(onQuit,100);return {restarting:true};
 });
}
module.exports={readLocation,register,safeDirectory};
