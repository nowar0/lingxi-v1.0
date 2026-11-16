import {settingsPayload,sessionOptionFields} from './settingsPayload'
import { api, json } from '@/api/client'
export type SessionOptions = { revision:number; model:string; effort:string; temperature:number|null; tool_groups:string[]|null; voice_engine:string; auto_read:boolean }
export type OptionsView = {settings:SessionOptions;effective:Omit<SessionOptions,'revision'>&{resolved_model?:string};sources:Record<string,string>;parameter_support:{temperature:boolean;effort_levels:string[]}}
export type Proposal={id:string;revision:number;goal:string;acceptance:string;why:string;status:string;error:string;job_id?:string;work_session_id?:number;cost_hint:string;link?:string}
export const evolution={
 options:(sid:number)=>api<OptionsView>(`/v6/sessions/${sid}/options`),
 saveOptions:(sid:number,value:SessionOptions)=>{return api<OptionsView>(`/v6/sessions/${sid}/options`,json('PUT',settingsPayload(value,sessionOptionFields)))},
 proposals:(sid:number)=>api<{items:Proposal[]}>(`/v6/sessions/${sid}/work-proposals`),
 suggest:(sid:number,goal:string)=>api<Proposal>(`/v6/sessions/${sid}/suggest-work-task`,json('POST',{goal})),
 decide:(sid:number,item:Proposal,accept:boolean)=>api<Proposal>(`/v6/sessions/${sid}/work-proposals/${item.id}/${item.status==='needs_workspace'?'resume':'decision'}`,json('POST',{expected_revision:item.revision,accept,allow_planning_cost:accept,retry:item.status==='failed'})),
 command:(sid:number,text:string,id:string)=>api<{session_id:number;proposal?:Proposal;message?:string;download?:string}>(`/v6/sessions/${sid}/commands`,json('POST',{text,request_id:id})),
}
export {sentenceParts} from './speechSentences'
