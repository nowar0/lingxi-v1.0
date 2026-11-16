import {PageErrorBoundary} from '@/components/shared/PageErrorBoundary'
import { lazy } from 'react'
import {BrowserPreferencesSync} from '@/components/shared/BrowserPreferencesSync'
import { MessageActionsProvider } from '@/context/MessageActionsContext'
import { InputContextMenu } from '@/components/shared/InputContextMenu'
import { createHashRouter,createRoutesFromElements,RouterProvider,Navigate,Route,Outlet } from 'react-router-dom'
import { AppToaster } from '@/components/shared/AppToaster'
import { AppProvider } from '@/context/AppContext'
import { ThemeProvider } from '@/context/ThemeContext'
import { TooltipProvider } from '@/components/ui/tooltip'
import { AppShell } from '@/components/layout/AppShell'

const HomePage = lazy(() => import('@/pages/HomePage').then((module) => ({ default: module.HomePage })))
const CompanionPage = lazy(() => import('@/pages/CompanionPage').then((module) => ({ default: module.CompanionPage })))
const WorkPage = lazy(() => import('@/pages/WorkPage').then((module) => ({ default: module.WorkPage })))
const TasksPage = lazy(() => import('@/pages/TasksPage').then((module) => ({ default: module.TasksPage })))
const MemoryPage = lazy(() => import('@/pages/MemoryPage').then((module) => ({ default: module.MemoryPage })))
const SkillsPage = lazy(() => import('@/pages/SkillsPage').then((module) => ({ default: module.SkillsPage })))
const ConnectionsPage = lazy(() => import('@/pages/ConnectionsPage').then((module) => ({ default: module.ConnectionsPage })))
const ServicesPage = lazy(() => import('@/pages/ServicesPage').then((module) => ({ default: module.ServicesPage })))
const SettingsPage = lazy(() => import('@/pages/SettingsPage').then((module) => ({ default: module.SettingsPage })))

function RoutedRoot(){return <MessageActionsProvider><InputContextMenu/><BrowserPreferencesSync/><Outlet/><AppToaster/></MessageActionsProvider>}
const router=createHashRouter(createRoutesFromElements(<Route element={<RoutedRoot/>}><Route element={<AppShell />}><Route index element={<HomePage />} /><Route path="companion" element={<CompanionPage />} /><Route path="work" element={<WorkPage />} /><Route path="tasks" element={<TasksPage />} /><Route path="memory" element={<MemoryPage />} /><Route path="skills" element={<SkillsPage />} /><Route path="connections" element={<ConnectionsPage />} /><Route path="characters" element={<Navigate to="/settings?tab=characters" replace />} /><Route path="services" element={<ServicesPage />} /><Route path="settings" element={<SettingsPage />} /><Route path="*" element={<Navigate to="/" replace />} /></Route></Route>))
export default function App(){return <PageErrorBoundary><ThemeProvider><AppProvider><TooltipProvider><RouterProvider router={router}/></TooltipProvider></AppProvider></ThemeProvider></PageErrorBoundary>}
