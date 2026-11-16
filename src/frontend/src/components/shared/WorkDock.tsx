import { createPortal } from 'react-dom'
import { useEffect, useId, useRef, useState, type Dispatch, type ReactNode } from 'react'
import { ArrowLeft, ArrowRight, Check, ChevronRight, FileCode2, Maximize2, Minimize2, MoreHorizontal, MessageSquarePlus, PanelRightClose, PanelRightOpen, Plus, X, type LucideIcon } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Tooltip } from '@/components/ui/tooltip'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSub, DropdownMenuSubContent, DropdownMenuSubTrigger } from '@/components/ui/dropdown-menu'
import { DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { useWorkDockLayout } from './useWorkDockLayout'
import { cn } from '@/lib/utils'
import { workTabTitle, type WorkPane, type WorkTab, type WorkTabAction, type WorkTabState, type WorkToolKind } from '@/lib/workspaceTabs'
import { revealDockPane } from '@/lib/workDockMotion'

export type WorkToolOption = { id: WorkToolKind; title: string; icon: LucideIcon; detail: string }
export function WorkDock({ state, dispatch, open, onOpenChange, options, onOpen, renderTab, renderStart, projectPath, reveal, onCloseTab, onCreateSideChat, toolbarHost, leftToolbarHost, onFocusPane, onTabsOverflow }: {
  state: WorkTabState; dispatch: Dispatch<WorkTabAction>; open: boolean; onOpenChange: (value: boolean) => void
  options: readonly WorkToolOption[]; onOpen: (kind: WorkToolKind, side?: WorkPane) => void
  renderTab: (tab: WorkTab) => ReactNode; renderStart: (side: WorkPane) => ReactNode; projectPath: string
  onTabsOverflow?:(overflow:boolean)=>void
  onFocusPane?:(side:WorkPane)=>void
  leftToolbarHost?:HTMLElement|null
  toolbarHost?:HTMLElement|null
  onCloseTab?: (id: string) => void
  onCreateSideChat?: () => void
  reveal?: { side: WorkPane; version: number }
}) {
  const { rootRef, singlePane, resizing, present, width, maxWidth, handleProps } = useWorkDockLayout(open, onOpenChange)
  const [expanded, setExpanded] = useState(false)
  const [dragging, setDragging] = useState<string>()
  const [dropSide, setDropSide] = useState<WorkPane>()
  const [menuTab, setMenuTab] = useState<string>()
  const focusNext = useRef<WorkPane | 'toggle' | undefined>(undefined)
  const prefix = useId()
  const tabId = (id: string) => `${prefix}-tab-${encodeURIComponent(id)}`
  const panelId = (id: string) => `${prefix}-panel-${encodeURIComponent(id)}`
  useEffect(() => { if (!open) setExpanded(false) }, [open])
  useEffect(() => {
    if (!reveal?.version) return
    const next = revealDockPane(reveal.side, (rootRef.current?.clientWidth || Infinity) <= 720, { open, expanded })
    setExpanded(next.expanded); onOpenChange(next.open)
    // Reveal is an explicit command, including reopening an already active tab.
    // Later manual collapse/expand must not replay that earlier command.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reveal])
  useEffect(() => {
    for (const side of ['left', 'right'] as const) {
      const button = rootRef.current?.closest('.workspace-body')?.querySelector<HTMLElement>(`[data-pane="${side}"] [role="tab"][aria-selected="true"]`)
      button?.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'instant' })
    }
    if (!focusNext.current) return
    const side = focusNext.current; focusNext.current = undefined
    const button = side === 'toggle' || (side === 'right' && !open)
      ? rootRef.current?.closest('.workspace-page')?.querySelector<HTMLElement>('[data-workspace-tab-count]')
      : rootRef.current?.closest('.workspace-body')?.querySelector<HTMLElement>(`[data-pane="${side}"] [role="tab"][aria-selected="true"]`) || rootRef.current?.closest('.workspace-body')?.querySelector<HTMLElement>(`[data-pane="${side}"] .workspace-tab-add`)
    button?.focus()
  }, [state.active.left, state.active.right, state.tabs.length, open, rootRef])
  useEffect(()=>{const list=toolbarHost?.querySelector<HTMLElement>('[role=tablist]');if(!list){onTabsOverflow?.(false);return}const measure=()=>onTabsOverflow?.(list.scrollWidth>list.clientWidth+2);measure();const observer=new ResizeObserver(measure);observer.observe(list);return()=>observer.disconnect()},[toolbarHost,state.tabs,state.active,open,present,onTabsOverflow])
  const select = (tab: WorkTab) => { onFocusPane?.(tab.side); dispatch({ type: 'select', id: tab.id }); if (tab.side === 'right') onOpenChange(true) }
  const close = (tab: WorkTab) => {
    focusNext.current = tab.side
    if (onCloseTab) { onCloseTab(tab.id); return }
    dispatch({ type: 'close', id: tab.id })
    if (tab.side === 'right' && state.tabs.filter(item => item.side === 'right').length === 1) onOpenChange(false)
  }
  const move = (tab: WorkTab, side: WorkPane) => {
    if (tab.kind === 'chat' && side === 'right' && onCreateSideChat) { onCreateSideChat(); setDragging(undefined); setDropSide(undefined); return }
    onFocusPane?.(side); dispatch({ type: 'move', id: tab.id, side }); focusNext.current = side
    setExpanded(false)
    if (side === 'right') onOpenChange(true)
    else if (singlePane) onOpenChange(false)
    setDragging(undefined); setDropSide(undefined)
  }
  const header = (side: WorkPane) => {
    const tabs = state.tabs.filter(tab => tab.side === side && tab.kind !== 'start')
    return <div className={`workspace-tabbar dock-pane-header dock-${side}`} data-pane={side} inert={side === 'right' ? !open : open && (singlePane || expanded)} aria-hidden={side === 'right' ? !open : open && (singlePane || expanded)}>
      <div role="tablist" aria-label={`${side === 'left' ? '左侧' : '右侧'}窗格标签页`} className="workspace-tablist">
        {tabs.map(tab => {
          const Icon = tab.kind==='side-chat'?MessageSquarePlus:options.find(option => option.id === tab.kind)?.icon || FileCode2
          const active = state.active[side] === tab.id
          return <div key={tab.id} className={cn('workspace-tab', active && 'is-active')} draggable onDragOver={event => { if (dragging && state.tabs.find(t => t.id === dragging)?.side === side) { event.preventDefault(); event.stopPropagation(); event.dataTransfer.dropEffect = 'move' } }} onDrop={event => { event.preventDefault(); event.stopPropagation(); if (dragging && event.dataTransfer.getData('application/x-lingxi-work-tab') === dragging) dispatch({ type: 'reorder', id: dragging, beforeId: tab.id }); setDragging(undefined); setDropSide(undefined) }} onDragStart={event => { setDragging(tab.id); event.dataTransfer.effectAllowed = 'move'; event.dataTransfer.setData('application/x-lingxi-work-tab', tab.id) }} onDragEnd={() => { setDragging(undefined); setDropSide(undefined) }} onContextMenu={event => { event.preventDefault(); setMenuTab(tab.id) }}>
            <button draggable role="tab" id={tabId(tab.id)} aria-controls={panelId(tab.id)} aria-selected={active} tabIndex={active ? 0 : -1} className="workspace-tab-trigger" title={tab.path || workTabTitle(tab)} onClick={() => select(tab)} onKeyDown={event => {
              if (event.altKey && ['ArrowLeft', 'ArrowRight'].includes(event.key)) {
                event.preventDefault(); const i = tabs.findIndex(t => t.id === tab.id)
                if (event.key === 'ArrowLeft' && i > 0) dispatch({ type: 'reorder', id: tab.id, beforeId: tabs[i-1].id })
                if (event.key === 'ArrowRight' && i < tabs.length - 1) dispatch({ type: 'reorder', id: tabs[i+1].id, beforeId: tab.id })
                return
              }
              if (event.key === 'Delete') { event.preventDefault(); close(tab); return }
              if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
              event.preventDefault(); const index = tabs.findIndex(item => item.id === tab.id)
              const next = event.key === 'Home' ? tabs[0] : event.key === 'End' ? tabs.at(-1)! : tabs[(index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length]
              focusNext.current = side; select(next)
            }}><Icon /><span>{workTabTitle(tab)}</span></button>
            <DropdownMenu open={menuTab === tab.id} onOpenChange={value => setMenuTab(value ? tab.id : undefined)}><DropdownMenuTrigger asChild><button className="workspace-tab-menu" aria-label={`标签页操作：${workTabTitle(tab)}`}><MoreHorizontal /></button></DropdownMenuTrigger><DropdownMenuContent align="start"><DropdownMenuItem disabled={tabs[0]?.id === tab.id} onSelect={() => { const i = tabs.findIndex(t => t.id === tab.id); if (i > 0) dispatch({ type: 'reorder', id: tab.id, beforeId: tabs[i-1].id }) }}><ArrowLeft />在本栏向前移动</DropdownMenuItem><DropdownMenuItem disabled={tabs.at(-1)?.id === tab.id} onSelect={() => { const i = tabs.findIndex(t => t.id === tab.id); if (i < tabs.length - 1) dispatch({ type: 'reorder', id: tabs[i+1].id, beforeId: tab.id }) }}><ArrowRight />在本栏向后移动</DropdownMenuItem><DropdownMenuItem onSelect={() => move(tab, side === 'left' ? 'right' : 'left')}>{side === 'left' ? <ArrowRight /> : <ArrowLeft />}{tab.kind === 'chat' && side === 'left' && onCreateSideChat ? '创建独立侧聊' : `移至${side === 'left' ? '右侧' : '左侧'}窗格`}</DropdownMenuItem><DropdownMenuItem onSelect={() => close(tab)}><X />关闭标签页</DropdownMenuItem></DropdownMenuContent></DropdownMenu>
            <button className="workspace-tab-close" aria-label={`关闭标签页：${workTabTitle(tab)}`} tabIndex={active ? 0 : -1} onClick={() => close(tab)}><X /></button>
          </div>
        })}
      </div>
      <DropdownMenu><DropdownMenuTrigger asChild><Button variant="ghost" size="icon" className="workspace-tab-add" aria-label={`添加${side === 'left' ? '左侧' : '右侧'}标签页`}><Plus /></Button></DropdownMenuTrigger><DropdownMenuContent align="start">
        {onCreateSideChat && <DropdownMenuItem onSelect={onCreateSideChat}><MessageSquarePlus />侧边聊天</DropdownMenuItem>}
        {options.filter(option => ['files', 'terminal', 'browser'].includes(option.id)).map(option => <DropdownMenuItem key={option.id} onSelect={() => onOpen(option.id, side)}><option.icon />{option.id === 'files' ? '项目文件' : option.id === 'browser' ? '浏览器' : option.title}</DropdownMenuItem>)}
        <DropdownMenuSub><DropdownMenuSubTrigger><MoreHorizontal />更多工具<ChevronRight className="ml-auto" /></DropdownMenuSubTrigger><DropdownMenuSubContent>{options.filter(option => !['start', 'chat', 'files', 'terminal', 'browser'].includes(option.id)).map(option => <DropdownMenuItem key={option.id} onSelect={() => onOpen(option.id, side)}><option.icon />{option.title}</DropdownMenuItem>)}</DropdownMenuSubContent></DropdownMenuSub>
      </DropdownMenuContent></DropdownMenu>
    </div>
  }
  const draggedTab=state.tabs.find(t=>t.id===dragging)
  const DragIcon=draggedTab?.kind==='side-chat'?MessageSquarePlus:options.find(t=>t.id===draggedTab?.kind)?.icon||FileCode2
  const visible = (side: WorkPane) => side === 'right' ? present : !(open && (singlePane || expanded))
  return <div ref={rootRef} data-unified-header={!!toolbarHost} className={cn('workspace-stage workspace-dock', state.tabs.filter(t=>t.side==='left').length===1 && state.tabs.find(t=>t.side==='left')?.kind==='chat' && 'is-main-chat', !present && 'is-right-collapsed', expanded && open && 'is-right-expanded', singlePane && 'is-single-pane', resizing && 'is-resizing')}>
    {toolbarHost&&createPortal(<>{present&&header('right')}{open&&!singlePane&&state.tabs.some(t=>t.side==='right'&&t.kind!=='start')&&<Tooltip content={expanded?'恢复分栏':'扩大窗格'}><Button variant="ghost" size="icon" className="workspace-expand size-8" aria-label={expanded?'恢复分栏':'扩大窗格'} onClick={()=>setExpanded(v=>!v)}>{expanded?<Minimize2/>:<Maximize2/>}</Button></Tooltip>}<Tooltip content={open?'收起工具区':'展开工具区'}><Button variant="ghost" size="icon" className="size-8 workspace-pane-toggle" aria-label={open?'收起工具区':'展开工具区'} aria-expanded={open} onClick={()=>onOpenChange(!open)}>{open?<PanelRightClose/>:<PanelRightOpen/>}</Button></Tooltip></>,toolbarHost)}
    {leftToolbarHost&&state.tabs.some(t=>t.side==='left'&&t.kind!=='chat')&&createPortal(header('left'),leftToolbarHost)}
    {!toolbarHost&&<>{header('left')}{header('right')}</>}
    {state.tabs.map(tab => {
      const shown = state.active[tab.side] === tab.id && visible(tab.side)
      return <section key={`${tab.id}:${['git', 'ssh', 'review', 'file'].includes(tab.kind) ? projectPath : ''}`} id={panelId(tab.id)} role={tab.kind === 'start' ? 'region' : 'tabpanel'} aria-label={tab.kind === 'start' ? '添加工具' : undefined} aria-labelledby={tab.kind === 'start' ? undefined : tabId(tab.id)} hidden={!shown} inert={!shown||(tab.side==='right'&&!open)} aria-hidden={tab.side==='right'&&!open?true:undefined} data-pane-content={tab.side} onPointerDownCapture={()=>onFocusPane?.(tab.side)} onFocusCapture={()=>onFocusPane?.(tab.side)} className={cn('dock-tab-panel', `dock-${tab.side}`, tab.kind === 'start' && 'is-start', ['chat', 'side-chat'].includes(tab.kind) && 'is-chat')}>
        <div className={cn('workspace-tab-content', ['git', 'ssh', 'review'].includes(tab.kind) && 'is-frame', tab.kind === 'start' && 'is-start', ['chat', 'side-chat'].includes(tab.kind) && 'is-chat')}>{renderTab(tab)}</div>
      </section>
    })}
    {(['left', 'right'] as const).map(side => !state.tabs.some(tab => tab.side === side) && <section key={`empty-${side}`} hidden={!visible(side)} inert={!visible(side)} className={`dock-tab-panel dock-${side} is-start`}>{renderStart(side)}</section>)}
    <div role="separator" aria-label="调整右侧窗格，向右拖动可完全收起" aria-orientation="vertical" aria-valuemin={0} aria-valuemax={maxWidth} aria-valuenow={open ? width : 0} tabIndex={open && !singlePane && !expanded ? 0 : -1} className="workspace-tool-resize dock-divider" {...handleProps} />
    {dragging && <div className="dock-drop-overlay" aria-hidden="true">{(['left', 'right'] as const).map(side => <div key={side} className={cn('dock-drop-target', dropSide === side && 'is-target')} onDragEnter={event => { event.preventDefault(); setDropSide(side) }} onDragOver={event => { event.preventDefault(); event.dataTransfer.dropEffect = 'move'; setDropSide(side) }} onDrop={event => { event.preventDefault(); const tab = state.tabs.find(item => item.id === dragging); if (tab && event.dataTransfer.getData('application/x-lingxi-work-tab') === tab.id) move(tab, side); else { setDragging(undefined); setDropSide(undefined) } }}><div className="workspace-drag-card"><header><DragIcon/><span>{workTabTitle(state.tabs.find(t=>t.id===dragging)!)}</span></header><div>{side==='left'?<ArrowLeft/>:<ArrowRight/>}<strong>移至{side==='left'?'左侧':'右侧'}窗格</strong></div></div></div>)}</div>}
  </div>
}
