import {useEffect,useState} from 'react'
import {useNavigate,useSearchParams} from 'react-router-dom'
import {Search} from 'lucide-react'
import {Input} from '@/components/ui/input'
import {confirmDiscard} from '@/components/shared/UnsavedChanges'
import './settings-search.css'
const groups:[string,string,string,string][]=[
 ['conversations','近期原文最多保留（轮）','记不住 记忆 原文 轮数 上下文 历史','全部会话 · 原文保留'],
 ['conversations','全局后备容量','上下文 容量 模型 令牌 tokens','全部会话 · 容量未确认时'],
 ['conversations','模型上下文容量','上下文 模型 容量 自动 识别','同服务与模型'],
 ['conversations','摘要后仍保留原文（轮）','摘要 压缩 保留 原文 轮数','消息 / 工作分别默认'],
 ['conversations','自动压缩','自动 压缩 上下文 满了','消息 / 工作分别默认'],
 ['conversations','提前压缩','压缩 等待 后台 摘要','消息 / 工作分别默认'],
 ['conversations','默认摘要模型','摘要 模型 压缩','消息 / 工作分别默认'],
 ['conversations','自动朗读回复','自动说话 朗读 发声 语音','消息 / 工作分别默认'],
 ['conversations','历史聊天 RAG','找以前 聊天 历史 检索 搜索','全部会话 · 工作仅检索当前会话'],
 ['conversations','自动提取记忆','记不住 自动 记住 提取','全部会话'],
 ['conversations','记忆语义检索','记忆 搜索 语义','全部会话'],
 ['memory','记忆保留','忘记 遗忘 半衰期 保留 衰减','全部记忆'],
 ['memory','检索','记忆 搜索 检索 找不到','当前选择的记忆范围'],
 ['memory','同步','记忆 更新 冲突 同步','当前选择的记忆范围'],
 ['sessions','会话管理','删除 对话 归档 聊天 恢复','选中的会话'],
 ['tasks','每日 Token 上限','费用 钱 消耗 预算 token 限额','全部任务 · 0 为不限制'],
 ['tasks','独立验收模型','验收 完成 复核 评分','全部任务'],
 ['appearance','关闭窗口','关窗口 托盘 退出 关闭 后台','此电脑'],
 ['appearance','字号','清晰 模糊 字体 大小 缩放','此电脑 · 界面字号'],
 ['appearance','减弱动态','动画 动效 流畅','此电脑'],
 ['appearance','主题包','主题 颜色 导入 外观','此电脑'],
 ['data&section=backup','备份与导入','换电脑 备份 恢复 数据 迁移','选中的备份和新目录'],
 ['data&section=storage','存储空间','目录 数据 路径 空间 迁移','此电脑 · 实际服务路径'],
 ['data&section=permissions','权限与审批','权限 终端 命令 审批 安全','工作权限'],
 ['data&section=cleanup','安全清理','日志 缓存 清理 删除','按预览选择的范围'],
 ['data&section=diagnostics','运行诊断','错误 故障 连接 bug','当前程序'],
 ['work','工作预设','默认 角色 工作 模板 预设','保存的预设 · 绑定时快照'],
 ['@/connections?tab=assignments','模型分配','换模型 key 密钥 服务 账号','各模态的默认模型'],
 ['@/work','项目资料与附件','读全文 附件 文件 提取 发送 预览','当前工作会话 · 附件操作在工作区'],
]
export function SettingsSearch(){
 const [query,setQuery]=useState(''),[params]=useSearchParams(),navigate=useNavigate()
 const q=query.trim().toLocaleLowerCase(),hits=q?groups.filter(([,title,aliases])=>(title+' '+aliases).toLocaleLowerCase().includes(q)).slice(0,12):[]
 useEffect(()=>{const target=params.get('focus');if(!target)return;let tries=0;const timer=setInterval(()=>{const candidates=Array.from(document.querySelectorAll<HTMLElement>('.management-view [aria-label],.management-view label,.management-view h3,.management-view h2,.management-view summary,.management-view .setting-row'));
 const node=candidates.find(n=>n.getAttribute('aria-label')===target||n.textContent?.trim()===target)||candidates.find(n=>n.textContent?.includes(target));if(node){let parent=node.parentElement;while(parent){if(parent instanceof HTMLDetailsElement)parent.open=true;parent=parent.parentElement}node.scrollIntoView({block:'center',behavior:'smooth'});node.classList.add('settings-search-target');setTimeout(()=>node.classList.remove('settings-search-target'),2400);clearInterval(timer)}else if(++tries>30)clearInterval(timer)},120);return()=>clearInterval(timer)},[params])
 const open=async(tab:string,title:string)=>{if(!await confirmDiscard())return;setQuery('');navigate(tab.startsWith('@')?tab.slice(1):`/settings?tab=${tab}&focus=${encodeURIComponent(title)}`)}
 return <div className="settings-search"><label><Search aria-hidden="true"/><Input aria-label="搜索设置" placeholder="搜索设置，例如：记不住、自动说话、换电脑" value={query} onChange={e=>setQuery(e.target.value)} onKeyDown={e=>{if(e.key==='Escape')setQuery('');if(e.key==='Enter'&&hits.length===1)void open(hits[0][0],hits[0][1])}}/></label>{q&&<div className="settings-search-results" role="region" aria-label="设置搜索结果">{hits.map(([tab,title,,scope])=><button type="button" key={tab+title} onClick={()=>void open(tab,title)}><strong>{title}</strong><small>{scope} · 打开查看当前值</small></button>)}{!hits.length&&<p>没有匹配的设置，请换一个关键词。</p>}</div>}</div>
}
