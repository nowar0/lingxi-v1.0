import { useCallback, useEffect, useState } from 'react'
import { api, json } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Switch } from '@/components/ui/switch'
import type { SpeechEngine as Engine } from './speech-package-controller'
export { SpeechPackagesPanel } from './SpeechPackagesPanel'

type Diagnostics={requests:Array<{id:string;role:string;model:string;status:string;duration_ms:number;reason?:string;at:string;usage:Record<string,number|null>}>;failures:Array<{reason:string;count:number}>;tools:{completed:number;failed:number;scope:string};speech:Engine[];budget:unknown;switches:unknown[]}
export function DiagnosticsPanel(){
 const [data,setData]=useState<Diagnostics>(),[settings,setSettings]=useState<{domain_tools:boolean;revision:number}>(),[error,setError]=useState(''),[busy,setBusy]=useState(false)
 const refresh=useCallback(async()=>{const [value,options]=await Promise.all([api<Diagnostics>('/v6/diagnostics'),api<{domain_tools:boolean;revision:number}>('/v6/evolution/settings')]);setData(value);setSettings(options)},[])
 useEffect(()=>{void refresh().catch(e=>setError((e as Error).message))},[refresh])
 return <Card className="settings-section evo-settings"><div className="settings-section-heading"><div><h2>运行诊断</h2><p>真实请求耗时、工具结果、语音状态和预算；不记录提示词、隐藏推理或 Key。</p></div><Button size="sm" variant="outline" disabled={busy} onClick={()=>{setBusy(true);void refresh().then(()=>setError('')).catch(e=>setError((e as Error).message)).finally(()=>setBusy(false))}}>刷新诊断</Button></div><div className="evo-settings-body">{error&&<p className="evo-error" role="alert">{error}</p>}{data&&<><p>工具成功 {data.tools.completed} · 未完成 {data.tools.failed}。{data.tools.scope}</p><div className="evo-table-scroll"><table className="evo-table"><caption>最近模型请求</caption><thead><tr><th>时间 / 角色</th><th>模型</th><th>结果</th><th>耗时</th></tr></thead><tbody>{data.requests.map(item=><tr key={item.id}><td>{item.at}<small>{item.role}</small></td><td>{item.model}</td><td>{item.status}<small>{item.reason}</small></td><td>{item.duration_ms.toLocaleString()} ms</td></tr>)}{!data.requests.length&&<tr><td colSpan={4}>尚无模型请求记录。</td></tr>}</tbody></table></div><details><summary>失败聚合</summary>{data.failures.map(item=><p key={item.reason}>{item.reason||'未提供原因'} · {item.count} 次</p>)}{!data.failures.length&&<p>暂无失败记录。</p>}</details><details><summary>工作预算与故障切换记录</summary><pre>{JSON.stringify({budget:data.budget,switches:data.switches},null,2)}</pre></details><details><summary>语音引擎状态</summary>{data.speech.map(item=><p key={item.engine}>{item.name} · {item.ready?'已加载':item.installed?'待加载':'未安装'} · {item.last_error||item.issues.join('；')}</p>)}</details></>}</div></Card>
}
