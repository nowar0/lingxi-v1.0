import React from 'react';
import {createRoot} from 'react-dom/client';

/** Presentation only: callers keep the same preview, token and one-time execution boundaries. */
export function confirmAction(message:string):Promise<boolean>{
 return new Promise(resolve=>{
  const previous=document.activeElement as HTMLElement|null;
  const host=document.createElement('div');host.className='lx-git-confirm-backdrop';document.body.append(host);
  const root=createRoot(host);let settled=false;
  const finish=(allowed:boolean)=>{if(settled)return;settled=true;document.removeEventListener('keydown',key);resolve(allowed);queueMicrotask(()=>{root.unmount();host.remove();if(previous?.isConnected)previous.focus()})};
  const key=(event:KeyboardEvent)=>{if(event.key==='Escape'){event.preventDefault();finish(false)}if(event.key==='Tab'){const controls=[...host.querySelectorAll<HTMLButtonElement>('button')];if(event.shiftKey&&document.activeElement===controls[0]){event.preventDefault();controls.at(-1)?.focus()}else if(!event.shiftKey&&document.activeElement===controls.at(-1)){event.preventDefault();controls[0]?.focus()}}};
  document.addEventListener('keydown',key);
  root.render(<section role="dialog" aria-modal="true" aria-labelledby="lx-operation-confirm-title" className="lx-git-confirm lx-operation-confirm"><header><h3 id="lx-operation-confirm-title">确认本次操作</h3></header><pre className="lx-operation-command">{message}</pre><footer className="lx-actions"><button onClick={()=>finish(false)}>取消</button><button className="lx-confirm-primary" onClick={()=>finish(true)}>确认本次操作</button></footer></section>);
  requestAnimationFrame(()=>host.querySelector<HTMLButtonElement>('button')?.focus());
 });
}
