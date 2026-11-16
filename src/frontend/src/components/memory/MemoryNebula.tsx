import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import { Minus, Plus, RotateCcw, Orbit, Network, FoldVertical, Pin } from 'lucide-react'
import { buildNebulaScene, NEBULA_LAYERS } from '../../lib/nebula'
import type { MemoryNebulaProps, NebulaItem, NebulaLabel } from '../../lib/nebula'
import { NebulaPreviewRenderer as NebulaRenderer } from './nebula-preview-renderer'
import type { NebulaRendererStatus } from './nebula-renderer'
import './nebula.css'
export type { MemoryNebulaProps, NebulaNode, NebulaEdge, NebulaId, NebulaLayer } from '../../lib/nebula'

/** Read-only graph. The owner supplies real memories and opens their detail drawer. */
export function MemoryNebula({ nodes, edges, selectedId, onSelect, onClearSelection, onOpenList, loading = false, variant='nebula',focusRequest=0 }: MemoryNebulaProps & { onOpenList?: () => void;variant?:'nebula'|'graph';focusRequest?:number }) {
  const helpId = useId()
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const rendererRef = useRef<NebulaRenderer | null>(null)
  const selectRef = useRef(onSelect)
  selectRef.current = onSelect
  const clearRef=useRef(onClearSelection);clearRef.current=onClearSelection
  const [status, setStatus] = useState<NebulaRendererStatus | 'initializing'>('initializing')
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set())
  const [labels, setLabels] = useState<NebulaLabel[]>([])
  const [autoRotate, setAutoRotate] = useState(true)
  const [reducedMotion, setReducedMotion] = useState(true)
  const [showEdges, setShowEdges] = useState(true)
  const [zoom,setZoom]=useState(1)
  const scene = useMemo(() => buildNebulaScene(nodes, edges, { layers: new Set(NEBULA_LAYERS), scope: '', theme: '', query: '', expanded, selectedId }), [nodes, edges, expanded, selectedId])
  const sceneRef = useRef(scene)
  sceneRef.current = scene
  const pick = useCallback((item: NebulaItem) => {
    if (item.kind === 'group') setExpanded(current => new Set([...current, item.key]))
    else if (item.node) selectRef.current(item.node.id)
  }, [])
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const renderer = new NebulaRenderer(canvas, { onStatus: setStatus, onPick: pick, onClearSelection:()=>clearRef.current?.(), onLabels: setLabels,onView:setZoom })
    rendererRef.current = renderer
    return () => { rendererRef.current = null; renderer.dispose() }
  }, [pick])
  useEffect(() => {
    const media = window.matchMedia('(prefers-reduced-motion: reduce)')
    const update = () => {
      const reduce = media.matches || document.documentElement.classList.contains('reduce-motion')
      setReducedMotion(reduce)
      if (reduce) setAutoRotate(false)
    }
    const observer = new MutationObserver(update)
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
    update()
    media.addEventListener('change', update)
    return () => { media.removeEventListener('change', update); observer.disconnect() }
  }, [])
  useEffect(() => { rendererRef.current?.setScene(scene); rendererRef.current?.setSelected(selectedId) }, [scene, selectedId])
  useEffect(() => rendererRef.current?.setAutoRotate(autoRotate && !reducedMotion), [autoRotate, reducedMotion])
  useEffect(() => rendererRef.current?.setShowEdges(showEdges), [showEdges])
  useEffect(()=>rendererRef.current?.setAtmosphere(variant==='nebula'),[variant])
  useEffect(()=>{if(focusRequest)rendererRef.current?.focusSelected()},[focusRequest])
  const fallback = status === 'unavailable' || status === 'lost' || status === 'error'
  return <div className="memory-nebula memory-nebula--simple" aria-label="记忆星空" aria-busy={loading}>
    <section className="memory-nebula__scene" aria-label="三维星图">
      <canvas ref={canvasRef} className="memory-nebula__canvas" tabIndex={fallback ? -1 : 0} aria-hidden={fallback} role="group" aria-label="三维记忆星图" aria-describedby={helpId} onKeyDown={event=>{if(event.key===' '){event.preventDefault();if(!reducedMotion)setAutoRotate(value=>!value)}}} />
      <p id={helpId} className="memory-nebula__sr">拖动旋转，滚轮或加减键缩放。方向键旋转，Shift 加方向键选择记忆，回车查看，Home 复位。</p>
      {!fallback && <div className="memory-nebula__labels">{labels.map(label => <button type="button" key={label.key} className={label.group ? 'is-group' : undefined} style={{ left: label.x, top: label.y, color:label.color,borderColor:label.color?label.color+'44':undefined }} title={label.title} onClick={() => { const item = sceneRef.current.items.find(value => value.key === label.key); if (item) pick(item); else { const cluster=sceneRef.current.roots.find(value=>`cluster-layer:${value.layer}`===label.key);if(cluster)rendererRef.current?.focusAt(cluster.position) } }} aria-label={label.group ? `展开${label.title}` : `查看${label.title}`}><i style={{background:label.color}}/>{label.pinned&&<Pin className="memory-label-pin" aria-label="已钉选"/>}{label.title}</button>)}</div>}
      {fallback && <div className="memory-nebula__fallback" role="status"><h3>星图暂不可用</h3><button type="button" onClick={onOpenList}>返回列表</button></div>}
      {!fallback && !scene.items.length && <div className="memory-nebula__empty" role="status"><h3>{loading ? '正在读取…' : '暂无记忆'}</h3>{!loading && <button type="button" onClick={onOpenList}>返回列表</button>}</div>}
      {!fallback && <div className="memory-nebula__scene-controls" aria-label="星图操作">
        <button type="button" title="缩小" aria-label="缩小星图" onClick={() => rendererRef.current?.zoomBy(1 / 1.2)}><Minus /></button>
        <span className="memory-nebula-zoom" aria-live="polite">{Math.round(zoom*100)}%</span>
        <button type="button" title="放大" aria-label="放大星图" onClick={() => rendererRef.current?.zoomBy(1.2)}><Plus /></button>
        <button type="button" title="复位" aria-label="复位星图" onClick={() => rendererRef.current?.reset()}><RotateCcw /></button>
        <button type="button" aria-label="自动旋转" title="自动旋转" aria-pressed={autoRotate && !reducedMotion} disabled={reducedMotion} onClick={() => setAutoRotate(current => !current)}><Orbit /></button>
        <button type="button" aria-label="关联线" title="关联线" aria-pressed={showEdges} onClick={() => setShowEdges(current => !current)}><Network /></button>
        {!!expanded.size && <button type="button" title="收起星团" aria-label="收起星团" onClick={() => setExpanded(new Set())}><FoldVertical /></button>}
      </div>}
    </section>
  </div>
}
export default MemoryNebula
