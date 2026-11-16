import {Button} from '@/components/ui/button'
import {Input} from '@/components/ui/input'
export function MappingEditor({value,onChange,label}:{value:string;onChange:(value:string)=>void;label:string}){
 let entries:Array<[string,unknown]>=[];try{entries=Object.entries(JSON.parse(value))}catch{}
 const save=(rows:Array<[string,unknown]>)=>onChange(JSON.stringify(Object.fromEntries(rows),null,2))
 return <div className="customer-settings"><div className="customer-list">{entries.map(([key,target],index)=><div key={index} className="customer-fields"><Input aria-label={`${label}标签 ${index+1}`} placeholder="标签，如 happy" value={key} onChange={e=>save(entries.map((row,i)=>i===index?[e.target.value,row[1]]:row))}/><div className="customer-actions"><Input aria-label={`${label}资源 ${index+1}`} placeholder="资源名称或路径" value={typeof target==='string'?target:JSON.stringify(target)} onChange={e=>save(entries.map((row,i)=>i===index?[row[0],e.target.value]:row))}/><Button size="sm" variant="ghost" aria-label={`移除${label} ${index+1}`} onClick={()=>save(entries.filter((_,i)=>i!==index))}>移除</Button></div></div>)}</div><Button size="sm" variant="outline" onClick={()=>save([...entries,[`标签_${entries.length+1}`,'']])}>添加{label}</Button></div>
}
