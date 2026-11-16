import {SettingsSearch} from '@/components/settings/SettingsSearch'
import {ConversationSettingsPanel,SessionManager,MemoryRules,TaskDefaults} from '@/components/settings/CustomerSettings'
import {confirmDiscard} from '@/components/shared/UnsavedChanges'
import { useEffect, useRef, useState } from 'react'
import { Archive, Brain, Check, Download, FileText, MessageSquare, Settings2, Monitor, Moon, MoreHorizontal, Palette, RotateCcw, ShieldCheck, SlidersHorizontal, Sun, Upload, UsersRound } from 'lucide-react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'
import { useTheme } from '@/context/ThemeContext'
import { CloseBehaviorSettings } from '@/components/settings/CloseBehaviorSettings'
import { DiagnosticsPanel } from '@/components/settings/EvolutionSettings'
import { WorkPresetSettings } from '@/components/shared/WorkPresets'
import { DataManagementPanel } from '@/components/settings/DataManagementPanel'
import { ProgramManagerPanel } from '@/components/settings/ProgramManagerPanel'
import { ManagementSurface } from '@/components/shared/ManagementSurface'
import { DataModeNotice } from '@/components/shared/DataModeNotice'
import { SettingRow } from '@/components/shared/SettingRow'
import { Button } from '@/components/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Switch } from '@/components/ui/switch'
import { Slider } from '@/components/ui/slider'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { CharactersPage } from '@/pages/CharactersPage'
import { CharacterLifecycle } from '@/components/settings/ManagementExtras'
import { useApp } from '@/context/AppContext'
import { ReasoningSliderSettings } from '@/components/shared/ReasoningSettings'
import { ProviderFailoverSettings } from '@/components/shared/ProviderFailoverSettings'
const accents = ['#191919', '#4c88d2', '#5a9b93', '#9a7bca', '#c68465', '#586779']
const categories = [{ id: 'appearance', name: '外观', icon: Palette }, {id:'model-behavior',name:'模型行为',icon:SlidersHorizontal},{id:'conversations',name:'上下文与记忆',icon:MessageSquare},{id:'sessions',name:'会话管理',icon:Archive},{id:'memory',name:'记忆管理',icon:Brain},{id:'tasks',name:'工作与任务',icon:Settings2},{ id: 'work', name: '工作预设', icon: FileText }, { id: 'characters', name: '人物卡', icon: UsersRound }, { id: 'characters-manage', name: '人物管理', icon: UsersRound }, { id: 'data', name: '权限与数据', icon: ShieldCheck }]
const dataSections = [{ id: 'permissions', name: '权限与审批' }, { id: 'storage', name: '存储空间' }, { id: 'backup', name: '备份与导入' }, { id: 'cleanup', name: '安全清理' }, { id: 'diagnostics', name: '运行诊断' }, { id: 'program', name: '程序管理' }]
export function SettingsPage() {
  const [params, setParams] = useSearchParams()
  const navigate=useNavigate(); useEffect(()=>{if(params.get('tab')==='models')navigate('/connections?tab=assignments',{replace:true});else if(params.get('tab')==='automation')navigate('/?schedule=reminder',{replace:true});else if(params.get('tab')==='data'&&params.get('section')==='features')navigate('/skills?tab=status',{replace:true})},[params,navigate])
  const tab = categories.some(value => value.id === params.get('tab')) ? params.get('tab')! : 'appearance'
  const dataSection = dataSections.some(value => value.id === params.get('section')) ? params.get('section')! : 'permissions'
  const { theme, mode, glass, reduceMotion, setMode, setGlass, setReduceMotion, setAccent, setRadius, setDensity, importTheme, exportTheme, resetTheme, setFontScale } = useTheme()
  const { connection } = useApp()
  const fileInput = useRef<HTMLInputElement>(null)
  const tabsRef = useRef<HTMLDivElement>(null)
  const [vertical, setVertical] = useState(true)
  useEffect(() => {
    const host = tabsRef.current?.closest('.app-main')
    if (!host) return
    const update = () => setVertical(host.clientWidth > 760)
    const observer = new ResizeObserver(update)
    update(); observer.observe(host)
    return () => observer.disconnect()
  }, [])
  return <Tabs ref={tabsRef} orientation={vertical ? 'vertical' : 'horizontal'} value={tab} onValueChange={value => {if(value!==tab)void confirmDiscard().then(ok=>{if(ok)setParams(value === 'appearance' ? {} : { tab: value })})}} className="settings-management-root"><ManagementSurface title="设置" className="settings-page" navigation={<TabsList className="settings-tabs" aria-label="设置分类">{categories.map(({ id, name, icon: Icon }) => <TabsTrigger key={id} value={id}><Icon />{name}</TabsTrigger>)}</TabsList>}>
    <SettingsSearch/><DataModeNotice />
    <TabsContent value="appearance" className="management-view appearance-settings">
      <section className="settings-section preference-group"><header className="management-section-title"><h2>外观</h2><DropdownMenu><DropdownMenuTrigger asChild><Button variant="ghost" size="icon" aria-label="更多外观操作"><MoreHorizontal /></Button></DropdownMenuTrigger><DropdownMenuContent align="end"><DropdownMenuItem onSelect={resetTheme}><RotateCcw />恢复默认外观</DropdownMenuItem></DropdownMenuContent></DropdownMenu></header>
        <div className="appearance-mode-grid" aria-label="明暗模式">{([{ id: 'light', label: '浅色', icon: Sun }, { id: 'dark', label: '深色', icon: Moon }, { id: 'system', label: '系统', icon: Monitor }] as const).map(({ id, label, icon: Icon }) => <button key={id} type="button" className={`mode-button appearance-choice${mode === id ? ' is-active' : ''}`} aria-pressed={mode === id} onClick={() => setMode(id)}><span className={`appearance-miniature is-${id}`} aria-hidden="true"><i /><span><b /><b /><b /></span></span><span className="appearance-choice-label"><Icon /><span>{label}</span>{mode === id && <Check className="appearance-choice-check" />}</span></button>)}</div>
        <div className="preference-rows"><SettingRow title="圆角"><div className="preference-range"><output>{theme.appearance.radius}px</output><Slider aria-label="界面圆角" min={6} max={24} value={[theme.appearance.radius]} onValueChange={([v]) => setRadius(v)} /></div></SettingRow>
          <SettingRow title="玻璃效果"><div className="preference-range"><output>{glass}%</output><Slider aria-label="玻璃效果" min={35} max={100} value={[glass]} onValueChange={([v]) => setGlass(v)} /></div></SettingRow>
          <SettingRow title="字号" description="不影响消息字号"><div className="preference-range"><output>{theme.appearance.fontScale}%</output><Slider aria-label="界面字体比例" min={90} max={125} step={5} value={[theme.appearance.fontScale]} onValueChange={([v]) => setFontScale(v)} /></div></SettingRow>
          <SettingRow title="界面密度"><div className="preference-segment"><Button variant={theme.appearance.density === 'comfortable' ? 'secondary' : 'ghost'} size="sm" onClick={() => setDensity('comfortable')}>舒适</Button><Button variant={theme.appearance.density === 'compact' ? 'secondary' : 'ghost'} size="sm" onClick={() => setDensity('compact')}>紧凑</Button></div></SettingRow>
          <SettingRow title="减弱动态" description="减少界面动画"><Switch aria-label="减弱动态" checked={reduceMotion} onCheckedChange={setReduceMotion} /></SettingRow>
        </div>
        <SettingRow title="强调色"><div className="preference-color-row">{accents.map(color => <button key={color} type="button" aria-label={`使用主题色 ${color}`} aria-pressed={theme.appearance.accent === color} style={{ background: color }} onClick={() => setAccent(color)}>{theme.appearance.accent === color && <Check />}</button>)}</div></SettingRow>
      </section>
      <section className="preference-group theme-package"><header className="management-section-title"><h2>主题包</h2></header><div className="theme-package-row"><div><strong title={theme.name}>{theme.name}</strong><p>JSON · 最大 100 KB · 不含聊天与 Key</p></div><div><Button variant="outline" size="sm" onClick={() => fileInput.current?.click()}><Upload />导入主题</Button><Button variant="ghost" size="sm" onClick={exportTheme}><Download />导出</Button></div></div><input ref={fileInput} type="file" accept=".json" className="hidden" aria-label="主题文件" onChange={async event => { const file = event.target.files?.[0]; if (file) { try { await importTheme(file) } catch (e) { toast.error('主题导入失败', { description: (e as Error).message }) } } event.target.value = '' }} /></section>
      <CloseBehaviorSettings />
    </TabsContent>

    <TabsContent value="model-behavior" className="management-view settings-model-behavior"><ReasoningSliderSettings/><ProviderFailoverSettings ready={connection==='connected'}/></TabsContent>
<TabsContent value="conversations" className="management-view"><ConversationSettingsPanel/></TabsContent><TabsContent value="sessions" className="management-view"><SessionManager/></TabsContent><TabsContent value="memory" className="management-view"><MemoryRules/></TabsContent><TabsContent value="tasks" className="management-view"><TaskDefaults/></TabsContent>
    <TabsContent value="work" className="management-view"><WorkPresetSettings /></TabsContent>
    <TabsContent value="characters" className="management-view settings-characters"><CharactersPage embedded /></TabsContent>
    <TabsContent value="characters-manage" className="management-view settings-characters-manage"><CharacterLifecycle /></TabsContent>
    <TabsContent value="data" className="management-view management-data"><header className="management-data-heading"><h2>权限与数据</h2></header><nav className="management-data-sections" aria-label="权限与数据分类">{dataSections.map(item => <button key={item.id} type="button" aria-current={dataSection === item.id ? 'page' : undefined} onClick={() => setParams({ tab: 'data', section: item.id })}>{item.name}</button>)}</nav>{dataSection === 'diagnostics' ? <DiagnosticsPanel /> : <><DataManagementPanel section={dataSection} />{dataSection === 'program' && <ProgramManagerPanel />}</>}</TabsContent>
  </ManagementSurface></Tabs>
}
