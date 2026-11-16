import React from 'react';
import {SshPanel} from '../vendor/dsh-ssh/src/client/panel/SshPanel';
import {SshApi} from '../vendor/dsh-ssh/src/client/api';
import {PanelController} from '../vendor/dsh-ssh/src/client/panel/controller';
import {confirmAction} from './confirm';
import {api} from './api';
class NativeSshApi extends SshApi{
 async ensureTrust(alias:string){
  const p=await api('/api/dsh-ssh/fingerprint','POST',{alias});
  if(!p.trusted){if(!await confirmAction(`请独立核对 SSH 主机指纹：\n${p.host}:${p.port}\n${p.fingerprint}\n\n核对无误后保存信任？`))throw Error('未确认指纹');await api('/api/dsh-ssh/trust','POST',{token:p.token,fingerprint:p.fingerprint})}
 }
 async testHost(alias:string){await this.ensureTrust(alias);return super.testHost(alias)}
 async ls(alias:string,path:string){await this.ensureTrust(alias);return super.ls(alias,path)}
 async startTunnel(options:any){await this.ensureTrust(options.alias);if(!await confirmAction(`建立本机 127.0.0.1:${options.localPort||'自动端口'} 到 ${options.alias} 的 ${options.remoteHost||'127.0.0.1'}:${options.remotePort} 隧道？`))throw Error('已取消');return super.startTunnel(options)}
 async cluster(options:any){
  let preview=await api('/api/dsh-ssh/cluster/preview','POST',options);
  if(preview.untrusted_aliases.length){for(const alias of preview.untrusted_aliases)await this.ensureTrust(alias);preview=await api('/api/dsh-ssh/cluster/preview','POST',options)}
  const targets=preview.targets.map((h:any)=>`${h.alias} · ${h.user}@${h.host} · 端口 ${h.port}`).join('\n');
  if(!await confirmAction(`将向以下 ${preview.targets.length} 台主机执行：\n${targets}\n\n具体命令：\n${preview.command}\n\n确认有效期120秒。确认执行？`))throw Error('已取消');
  return (await api('/api/dsh-ssh/cluster','POST',{token:preview.token})).results;
 }
}
const sshApi=new NativeSshApi();
const sshController=new PanelController();
sshController.open();

export default function SshTool(){return <SshPanel controller={sshController} api={sshApi}/>}
