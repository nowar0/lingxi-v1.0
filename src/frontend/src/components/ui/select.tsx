import { Children, isValidElement, useRef, useState, type ComponentProps, type ReactNode,useEffect } from 'react'
import * as Menu from '@radix-ui/react-dropdown-menu'
import { Check, ChevronDown } from 'lucide-react'
import { useSurface } from '@/context/SurfaceContext'
import { cn } from '@/lib/utils'
import './select.css'

type Option = { value:string; label:ReactNode; disabled?:boolean }
function options(children:ReactNode,disabled=false):Option[]{return Children.toArray(children).flatMap(node=>{if(!isValidElement<{value?:string|number;disabled?:boolean;children?:ReactNode}>(node))return[];if(node.type==='option')return[{value:String(node.props.value??Children.toArray(node.props.children).join('')),label:node.props.children,disabled:disabled||node.props.disabled}];return options(node.props.children,disabled||node.props.disabled)})}

/** Keeps native form/change semantics while presenting the shared neutral menu. */
export function Select({children,className,onChange,value,defaultValue,disabled,...props}:ComponentProps<'select'>){
  const native=useRef<HTMLSelectElement>(null),trigger=useRef<HTMLButtonElement>(null),surface=useSurface(),items=options(children)
  const [fallback,setFallback]=useState(String(defaultValue??items[0]?.value??'')),selected=String(value??fallback),current=items.find(item=>item.value===selected)
  useEffect(()=>{const node=trigger.current;if(!node)return;const labels=native.current?.labels;for(const label of labels||[])if(!label.contains(node)){const click=()=>node.focus();label.addEventListener('click',click);return()=>label.removeEventListener('click',click)}},[props.id])
  const choose=(next:string)=>{if(disabled)return;const node=native.current;if(!node)return;Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value')!.set!.call(node,next);node.dispatchEvent(new Event('change',{bubbles:true}))}
  return <><select {...props} ref={native} value={value} defaultValue={defaultValue} disabled={disabled} aria-label={undefined} aria-labelledby={undefined} aria-hidden tabIndex={-1} className="lingxi-select-native" onInvalid={event=>{event.preventDefault();trigger.current?.focus();trigger.current?.setAttribute('aria-invalid','true')}} onChange={event=>{setFallback(event.target.value);onChange?.(event)}}>{children}</select><Menu.Root><Menu.Trigger asChild><button ref={trigger} type="button" id={props.id?props.id+'-trigger':undefined} aria-required={props.required} aria-invalid={props['aria-invalid']} aria-describedby={props['aria-describedby']} disabled={disabled} aria-label={props['aria-label']} aria-labelledby={props['aria-labelledby']} className={cn('lingxi-select',className)}><span>{current?.label||selected||'请选择'}</span><ChevronDown/></button></Menu.Trigger><Menu.Portal><Menu.Content data-ui-surface={surface['data-ui-surface']} style={surface.style} className="lingxi-select-menu" sideOffset={5} collisionPadding={10}><Menu.RadioGroup value={selected} onValueChange={choose}>{items.map((item,index)=><Menu.RadioItem key={item.value+index} value={item.value} disabled={disabled||item.disabled} className="lingxi-select-option"><span>{item.label}</span><Menu.ItemIndicator><Check/></Menu.ItemIndicator></Menu.RadioItem>)}</Menu.RadioGroup></Menu.Content></Menu.Portal></Menu.Root></>
}
