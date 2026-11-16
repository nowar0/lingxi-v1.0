import { useEffect, useRef, useState, type FormEvent } from 'react'
import { ArrowLeft, ChevronRight, Folder, HardDrive, Home } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import './workspace-folder-picker.css'

type Folder = { name: string; path: string }
type Listing = { path: string; parent: string; items: Folder[]; truncated: boolean }
type FolderBridge = { browseFolders: (path: string) => Promise<Listing> }

export function WorkspaceFolderPicker({ open, initialPath, onOpenChange, onSelect }: {
  open: boolean
  initialPath: string
  onOpenChange: (open: boolean) => void
  onSelect: (path: string) => void
}) {
  const [listing, setListing] = useState<Listing | null>(null)
  const [entryPath, setEntryPath] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const request = useRef(0)

  const browse = async (path: string) => {
    const id = ++request.current
    setLoading(true)
    setError('')
    try {
      const shell = (window as unknown as { LingxiShell?: FolderBridge }).LingxiShell
      if (!shell?.browseFolders) throw new Error('请在零汐桌面应用中选择文件夹')
      const next = await shell.browseFolders(path)
      if (id !== request.current) return
      setListing(next)
      setEntryPath(next.path)
    } catch (cause) {
      if (id === request.current) setError((cause as Error).message)
    } finally {
      if (id === request.current) setLoading(false)
    }
  }

  useEffect(() => {
    if (!open) { request.current++; return }
    setListing(null)
    setEntryPath(initialPath)
    void browse(initialPath || '')
    return () => { request.current++ }
    // The initial path is read once when the picker opens.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const submitPath = (event: FormEvent) => { event.preventDefault(); void browse(entryPath.trim()) }
  const select = () => {
    if (!listing?.path || loading) return
    onSelect(listing.path)
    onOpenChange(false)
  }

  return <Dialog open={open} onOpenChange={onOpenChange}>
    <DialogContent className="workspace-folder-picker" aria-describedby="workspace-folder-picker-description">
      <DialogHeader>
        <DialogTitle>选择文件夹</DialogTitle>
        <DialogDescription id="workspace-folder-picker-description">选择一个本地文件夹作为工作区。</DialogDescription>
      </DialogHeader>
      <form className="workspace-folder-picker-body" onSubmit={submitPath}>
        <div className="workspace-folder-picker-location">
          <Button type="button" variant="ghost" size="icon" aria-label="返回上一级" disabled={loading || !listing?.parent} onClick={() => void browse(listing!.parent)}><ArrowLeft /></Button>
          <Button type="button" variant="ghost" size="icon" aria-label="查看磁盘" disabled={loading} onClick={() => void browse('')}><Home /></Button>
          <Input aria-label="文件夹路径" value={entryPath} onChange={event => setEntryPath(event.target.value)} placeholder="输入本地文件夹路径" />
          <Button type="submit" size="sm" variant="outline" disabled={loading}>转到</Button>
        </div>
        <div className="workspace-folder-picker-list" role="group" aria-label={listing?.path ? `${listing.path} 中的文件夹` : '本地磁盘'}>
          {loading && <p className="workspace-folder-picker-status" role="status">正在读取文件夹…</p>}
          {!loading && error && <p className="workspace-folder-picker-status" role="alert">{error}</p>}
          {!loading && !error && listing?.items.map(item => <button type="button" key={item.path} className="workspace-folder-picker-item" onClick={() => void browse(item.path)} title={item.path}>
            {listing.path ? <Folder /> : <HardDrive />}
            <span>{item.name}</span>
            <ChevronRight />
          </button>)}
          {!loading && !error && listing && !listing.items.length && <p className="workspace-folder-picker-status">这个文件夹里没有子文件夹。</p>}
          {!loading && !error && listing?.truncated && <p className="workspace-folder-picker-status">仅显示前 300 个文件夹，可在上方输入完整路径。</p>}
        </div>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>取消</Button>
          <Button type="button" disabled={!listing?.path || loading || !!error || entryPath.trim() !== listing?.path} onClick={select}>选择此文件夹</Button>
        </DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}
