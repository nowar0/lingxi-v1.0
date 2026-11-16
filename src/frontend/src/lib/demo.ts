import type { Character, MemoryItem, Message, ModelModule, ModelProfile, ScheduleItem, Session, TaskItem } from '@/types'

export const demoCharacter: Character = {
  id: 'lingxi', name: '零汐', description: '安静、可靠，也有一点好奇心的本地数字伙伴。',
  persona: '温柔而清晰，尊重边界，乐于帮助。', greeting: '欢迎回来。今天想先聊点什么，还是继续刚才的事情？',
  tags: ['本地伙伴', '雾蓝', '温柔可靠'], avatar: './assets/lingxi-avatar.png', art: './assets/lingxi-stage.png', revision: 1,
}

export const demoSessions: Session[] = [
  { id: 101, title: '今天也要慢慢来', mode: 'companion', updated_at: new Date().toISOString(), unread: 0, pinned: true },
  { id: 102, title: '周末散步计划', mode: 'companion', updated_at: new Date(Date.now() - 86400000).toISOString() },
  { id: 103, title: '关于最近的小情绪', mode: 'companion', updated_at: new Date(Date.now() - 172800000).toISOString() },
]

export const demoWorkSessions: Session[] = [
  { id: 201, title: '重构零汐前端工程', mode: 'work', updated_at: new Date().toISOString(), pinned: true, unread: 2 },
  { id: 202, title: '整理本地知识库', mode: 'work', updated_at: new Date(Date.now() - 3600000).toISOString() },
  { id: 203, title: '检查数据备份流程', mode: 'work', updated_at: new Date(Date.now() - 86400000).toISOString() },
]

export const demoMessages: Message[] = [
  { id: 1, role: 'assistant', content: '欢迎回来。我把今天的待办和正在进行的任务都整理好了，你可以从任何一件小事开始。', at: new Date(Date.now() - 1800000).toISOString(), speaker: { id: 'lingxi', revision: 1, name: '零汐', avatar: './assets/lingxi-avatar.png' } },
  { id: 2, role: 'user', content: '我想先把新 UI 的陪伴与工作流程梳理清楚。', at: new Date(Date.now() - 1500000).toISOString() },
  { id: 3, role: 'assistant', content: '好，我们可以把陪伴留给自然的交流，把工作区留给清晰的目标与执行。两种模式共享你允许公开的画像，但工作不会读取私人陪伴记忆。\n\n我已经准备了一个简单的检查清单，要现在看看吗？', at: new Date(Date.now() - 1200000).toISOString(), speaker: { id: 'lingxi', revision: 1, name: '零汐', avatar: './assets/lingxi-avatar.png' } },
]

export const demoWorkMessages: Message[] = [
  { id: 11, role: 'user', content: '检查新前端的信息架构，并给出第一轮实现计划。', at: new Date(Date.now() - 2400000).toISOString() },
  { id: 12, role: 'assistant', content: '我会先核对页面与真实接口边界，再拆分布局、共享组件和页面模块。工作区只读取当前项目的材料与允许共享的画像。', at: new Date(Date.now() - 2340000).toISOString() },
  { id: 13, role: 'assistant', content: '已完成接口盘点。下一步将建立 React 工程，并优先打通会话、主题导入和人物卡包流程。', at: new Date(Date.now() - 2280000).toISOString() },
]

export const demoSchedule: ScheduleItem[] = [
  { id: 1, title: '整理本周的学习资料', date: new Date().toISOString().slice(0, 10), done: false, revision: 1 },
  { id: 2, title: '给阳台的植物浇水', date: new Date().toISOString().slice(0, 10), done: true, revision: 1 },
  { id: 3, title: '备份一次本地数据', date: new Date(Date.now() + 86400000).toISOString().slice(0, 10), done: false, revision: 1 },
]

export const demoMemories: MemoryItem[] = [
  { id: 1, key: '偏好安静的开始', value: '早上更喜欢先看到简洁的计划，不希望被连续提醒。', scope: 'companion', category: 'preference', pinned: true, revision: 2, source: '陪伴对话', updated_at: new Date().toISOString() },
  { id: 2, key: '界面表达偏好', value: '喜欢低饱和、清晰留白和不过度装饰的界面。', scope: 'shared', category: 'communication_preference', pinned: true, revision: 1, source: '手动添加', updated_at: new Date().toISOString() },
  { id: 3, key: '当前项目约束', value: '新 UI 不改写业务数据，真实状态与演示状态必须明确区分。', scope: 'work', category: 'fact', revision: 1, source: '工作会话', updated_at: new Date().toISOString() },
  { id: 4, key: '周末安排', value: '周末下午通常适合散步和整理房间。', scope: 'companion', category: 'event', revision: 1, source: '陪伴对话', updated_at: new Date().toISOString() },
]

export const demoTasks: TaskItem[] = [
  {
    id: 'job-lingxi-ui', session_id: 201, title: '零汐 UI 完全重构', status: 'running', revision: 3,
    document: { goal: '生成完整 React 工程与高保真可交互原型，并接入已核实的真实接口。', acceptance: '路由完整、构建通过、真实与演示状态清晰。' },
    nodes: [
      { id: 'n1', status: 'completed', document: { title: '资料与接口盘点', task: '读取交接资料、原 UI 和 FastAPI 路由。' } },
      { id: 'n2', status: 'completed', document: { title: '设计系统与工程骨架', task: '建立雾蓝主题、组件和路由。' } },
      { id: 'n3', status: 'running', document: { title: '核心页面与交互', task: '实现陪伴、工作、主题和卡包流程。' } },
      { id: 'n4', status: 'pending', document: { title: '构建验证', task: '执行类型检查和生产构建。' } },
    ],
  },
  { id: 'job-backup', session_id: 203, title: '核对离线备份流程', status: 'completed', revision: 4, document: { goal: '验证备份包内容和恢复边界。' }, nodes: [] },
  { id: 'job-memory', session_id: 202, title: '整理共享画像分类', status: 'waiting', revision: 1, document: { goal: '只保留允许工作读取的共享信息。' }, nodes: [] },
]

export const demoModules: ModelModule[] = [
  { id: 'companion', revision: 1, enabled: true, model: 'lingxi-chat', required_capability: 'text' },
  { id: 'work', revision: 1, enabled: true, model: 'lingxi-work', required_capability: 'text' },
  { id: 'vision', revision: 1, enabled: false, model: '', required_capability: 'vision' },
  { id: 'tts', revision: 1, enabled: false, model: '', required_capability: 'tts' },
]

export const demoProfiles: ModelProfile[] = [
  { id: 'demo-local', name: '演示模型配置', model: 'lingxi-chat', base_url: 'http://127.0.0.1:8000', revision: 1, local: true, capabilities: ['text'] },
]
