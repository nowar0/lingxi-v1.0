import { cameraMatrix, clamp, defaultCamera, idKey, nebulaAtmosphere, nebulaThemeLabel, LAYER_NAMES, LAYER_COLORS, hexRgb, placeNebulaLabels, POINT_STRIDE, projectPoint, MAX_SCENE_DPR } from '../../lib/nebula'
import type { NebulaCamera, NebulaId, NebulaItem, NebulaLabel, NebulaScene, Vec3 } from '../../lib/nebula'

export type NebulaRendererStatus = 'ready' | 'unavailable' | 'lost' | 'error'
export interface NebulaRendererCallbacks {
  onStatus: (status: NebulaRendererStatus) => void
  onPick: (item: NebulaItem) => void
  onClearSelection?:()=>void
  onLabels: (labels: NebulaLabel[]) => void
  onView?:(zoom:number)=>void
  onMetrics?: (metrics: { renderMs: number; dpr: number; points: number }) => void
}
interface Program { value: WebGLProgram; attributes: Record<string, number>; uniforms: Record<string, WebGLUniformLocation | null> }
interface Resources { points: Program; lines: Program; fog: Program; pointBuffer: WebGLBuffer; solidBuffer: WebGLBuffer; candidateBuffer: WebGLBuffer; dustBuffer: WebGLBuffer; pinBuffer:WebGLBuffer; highlightBuffer:WebGLBuffer; quadBuffer: WebGLBuffer }
type GL = WebGLRenderingContext | WebGL2RenderingContext
const pointVertex = `
attribute vec3 a_position;
attribute vec3 a_color;
attribute float a_size;
attribute float a_light;
attribute float a_index;
uniform mat4 u_matrix;
uniform float u_dpr;
uniform float u_maxPoint;
uniform float u_selected;
uniform float u_pinPass;
varying mediump vec3 v_color;
varying mediump float v_light;
varying mediump float v_selected;
varying mediump float v_haze;
varying mediump float v_pinPass;
void main() {
  vec4 projected = u_matrix * vec4(a_position, 1.0);
  gl_Position = projected;
  v_selected = abs(a_index - u_selected) < 0.25 ? 1.0 : 0.0;
  gl_PointSize = min(u_maxPoint, clamp(a_size * 4.0 / max(0.4, projected.w), a_index < 0.0 ? 1.2 : 12.0, 240.0) * u_dpr + v_selected * 8.0 * u_dpr + u_pinPass * 12.0 * u_dpr);
  v_pinPass=u_pinPass; v_color = a_color; v_light = a_light; v_haze = a_index < -1.0 && a_size > 40.0 ? 1.0 : 0.0;
}`
const pointFragment = `
precision mediump float;
uniform float u_dark;
varying mediump vec3 v_color;
varying mediump float v_light;
varying mediump float v_selected;
varying mediump float v_haze;
varying mediump float v_pinPass;
void main() {
  float radius = length(gl_PointCoord - vec2(0.5)) * 2.0;
  if (radius > 1.0) discard;
  if(v_pinPass>.5){float ring=(1.0-smoothstep(.018,.045,abs(radius-.72)))*.72;gl_FragColor=vec4(v_color,ring);return;}
  float core = exp(-radius * radius * 14.0);
  float halo = pow(max(0.0, 1.0 - radius), 2.6) * 0.2;
  float ring = v_selected * (1.0 - smoothstep(0.025, 0.075, abs(radius - 0.74))) * 0.8;
  float alpha = mix((core + halo) * v_light + ring, exp(-radius * radius * 3.5) * v_light, v_haze);
  float tone = dot(v_color, vec3(0.2126, 0.7152, 0.0722));
  gl_FragColor = vec4(mix(v_color,vec3(1.0),(core*.32+v_selected*.12)*(1.0-v_haze)), min(1.0,alpha));
}`
const lineVertex = `
attribute vec3 a_position;
attribute float a_distance;
uniform mat4 u_matrix;
varying mediump float v_distance;
void main() { gl_Position = u_matrix * vec4(a_position, 1.0); v_distance = a_distance; }`
const lineFragment = `
precision mediump float;
uniform vec4 u_color;
uniform float u_candidate;
varying mediump float v_distance;
void main() {
  if (u_candidate > 0.5 && mod(v_distance, 0.095) > 0.048) discard;
  gl_FragColor = u_color;
}`
const fogVertex = `attribute vec2 a_position; varying mediump vec2 v_uv; void main() { v_uv = a_position * 0.5 + 0.5; gl_Position = vec4(a_position, 0.999, 1.0); }`
const fogFragment = `
precision mediump float;
uniform float u_dark;
varying mediump vec2 v_uv;
void main() {
 vec2 uv=v_uv-vec2(.5);
 float cloud=exp(-dot(uv*vec2(1.1,1.65),uv*vec2(1.1,1.65))*6.0);
 float grain=fract(sin(dot(floor(v_uv*720.0),vec2(12.9898,78.233)))*43758.5453);
 float light=.018+cloud*.023+grain*.003;
 gl_FragColor=vec4(vec3(.022,.027,.045)+vec3(.7,.4,1.0)*(light-.018),1.0);
}`

/** Native GPU 3D: all geometry is transformed by a perspective mat4 in vertex shaders.
 * CPU projection is used only for hit-testing and accessible DOM label placement. */
export class NebulaRenderer {
  private gl: GL | null = null
  private resources: Resources | null = null
  private scene: NebulaScene | null = null
  private camera: NebulaCamera = defaultCamera()
  private sceneCenter: Vec3 = [0, 0, 0]
  private matrix = new Float32Array(16)
  private projected = new Float32Array(0)
  private dustCount = 0
  private pinCount=0
  private highlightSolid = 0
  private highlightCandidate = 0
  private raf: number | null = null
  private disposed = false
  private lost = false
  private intersecting = true
  private autoRotate = false
  private reducedMotion = true
  private showEdges = true
  private showAtmosphere = true
  private width = 1
  private height = 1
  private dpr = 1
  private selectedId: NebulaId | null = null
  private pendingSelectionFocus = false
  private hovered = -1
  private keyboardItem = -1
  private pointer: { id: number; x: number; y: number; startX: number; startY: number; moved: boolean } | null = null
  private lastFrame = 0
  private lastLabels = -Infinity
  private lastMetrics = -Infinity
  private maxPoint = 128
  private enabledAttributes = new Set<number>()
  private removeListeners: Array<() => void> = []
  private resizeObserver: ResizeObserver | null = null
  private intersectionObserver: IntersectionObserver | null = null
  private media: MediaQueryList | null = null
  private themeObserver: MutationObserver | null = null

  constructor(private canvas: HTMLCanvasElement, private callbacks: NebulaRendererCallbacks) {
    this.listen(canvas, 'webglcontextlost', this.onContextLost)
    this.listen(canvas, 'webglcontextrestored', this.onContextRestored)
    this.listen(document, 'visibilitychange', this.onVisibility)
    this.listen(window, 'resize', this.resize)
    this.listen(canvas, 'pointerdown', this.onPointerDown)
    this.listen(canvas, 'pointermove', this.onPointerMove)
    this.listen(canvas, 'pointerup', this.onPointerUp)
    this.listen(canvas, 'pointercancel', this.cancelPointer)
    this.listen(canvas, 'lostpointercapture', this.cancelPointer)
    this.listen(canvas, 'pointerleave', this.onPointerLeave)
    this.listen(canvas, 'wheel', this.onWheel, { passive: false })
    this.listen(canvas, 'keydown', this.onKeyDown)
    this.listen(canvas, 'blur', this.onBlur)
    this.media = window.matchMedia?.('(prefers-reduced-motion: reduce)') || null
    this.reducedMotion = this.media?.matches ?? true
    if (this.media) this.listen(this.media, 'change', this.onReducedMotion)
    if (typeof ResizeObserver !== 'undefined') { this.resizeObserver = new ResizeObserver(this.resize); this.resizeObserver.observe(canvas) }
    if (typeof IntersectionObserver !== 'undefined') {
      this.intersectionObserver = new IntersectionObserver(entries => {
        this.intersecting = entries.some(entry => entry.isIntersecting)
        this.syncPause()
      })
      this.intersectionObserver.observe(canvas)
    }
    if(typeof MutationObserver!=='undefined'){this.themeObserver=new MutationObserver(()=>this.invalidate());this.themeObserver.observe(document.documentElement,{attributes:true,attributeFilter:['class']})}
    this.initialize()
  }
  private listen(target: EventTarget, event: string, callback: EventListenerOrEventListenerObject, options?: AddEventListenerOptions): void {
    target.addEventListener(event, callback, options)
    this.removeListeners.push(() => target.removeEventListener(event, callback, options))
  }
  private compileProgram(vertex: string, fragment: string, attributes: string[], uniforms: string[], webgl2: boolean, ownedPrograms: WebGLProgram[]): Program {
    const gl = this.gl!
    const shaders: WebGLShader[] = []
    let program: WebGLProgram | null = null
    try {
      for (const [source, type] of [[vertex, gl.VERTEX_SHADER], [fragment, gl.FRAGMENT_SHADER]] as const) {
        const shader = gl.createShader(type)
        if (!shader) throw new Error('WebGL shader allocation failed')
        shaders.push(shader)
        let code = source
        if (webgl2) {
          code = code.replace(/attribute\b/g, 'in').replace(/varying\b/g, type === gl.VERTEX_SHADER ? 'out' : 'in')
          if (type === gl.FRAGMENT_SHADER) code = code.replace('precision mediump float;', 'precision mediump float;\nout vec4 outColor;').replace(/gl_FragColor\b/g, 'outColor')
          code = `#version 300 es\n${code}`
        }
        gl.shaderSource(shader, code); gl.compileShader(shader)
        if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw new Error('WebGL shader compilation failed')
      }
      program = gl.createProgram()
      if (!program) throw new Error('WebGL program allocation failed')
      shaders.forEach(shader => gl.attachShader(program!, shader)); gl.linkProgram(program)
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error('WebGL shader link failed')
      const result = { value: program, attributes: Object.fromEntries(attributes.map(name => [name, gl.getAttribLocation(program!, name)])), uniforms: Object.fromEntries(uniforms.map(name => [name, gl.getUniformLocation(program!, name)])) }
      ownedPrograms.push(program)
      return result
    } catch (error) { if (program) gl.deleteProgram(program); throw error }
    finally { shaders.forEach(shader => gl.deleteShader(shader)) }
  }
  private initialize(): void {
    if (this.disposed) return
    const attributes = { alpha: false, antialias: true, depth: true, powerPreference: 'low-power' as const, preserveDrawingBuffer: false }
    let webgl2 = false
    try {
      const gl2 = this.canvas.getContext('webgl2', attributes) as WebGL2RenderingContext | null
      this.gl = gl2 || (this.canvas.getContext('webgl', attributes) as WebGLRenderingContext | null)
      webgl2 = Boolean(gl2)
    } catch { this.gl = null }
    const gl = this.gl
    if (!gl) { this.callbacks.onStatus('unavailable'); return }
    const ownedPrograms: WebGLProgram[] = [], ownedBuffers: WebGLBuffer[] = []
    try {
      const makeBuffer = (): WebGLBuffer => { const buffer = gl.createBuffer(); if (!buffer) throw new Error('WebGL buffer allocation failed'); ownedBuffers.push(buffer); return buffer }
      const points = this.compileProgram(pointVertex, pointFragment, ['a_position', 'a_color', 'a_size', 'a_light', 'a_index'], ['u_matrix', 'u_dpr', 'u_maxPoint', 'u_selected','u_dark','u_pinPass'], webgl2, ownedPrograms)
      const lines = this.compileProgram(lineVertex, lineFragment, ['a_position', 'a_distance'], ['u_matrix', 'u_color', 'u_candidate'], webgl2, ownedPrograms)
      const fog = this.compileProgram(fogVertex, fogFragment, ['a_position'], ['u_dark'], webgl2, ownedPrograms)
      this.resources = { points, lines, fog, pointBuffer: makeBuffer(), solidBuffer: makeBuffer(), candidateBuffer: makeBuffer(), dustBuffer: makeBuffer(), pinBuffer:makeBuffer(), highlightBuffer:makeBuffer(), quadBuffer: makeBuffer() }
      this.maxPoint = gl.getParameter(gl.ALIASED_POINT_SIZE_RANGE)?.[1] || 128
      gl.bindBuffer(gl.ARRAY_BUFFER, this.resources.dustBuffer); gl.bufferData(gl.ARRAY_BUFFER, nebulaAtmosphere(this.scene), gl.STATIC_DRAW)
      gl.bindBuffer(gl.ARRAY_BUFFER, this.resources.quadBuffer); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW)
      gl.clearColor(.018, .018, .018, 1)
      this.uploadScene()
      this.callbacks.onStatus('ready'); this.resize()
    } catch {
      ownedBuffers.forEach(buffer => gl.deleteBuffer(buffer)); ownedPrograms.forEach(program => gl.deleteProgram(program))
      this.resources = null; this.callbacks.onStatus('error'); this.stop()
    }
  }
  setScene(scene: NebulaScene): void {
    const firstContent = !this.scene?.items.length && scene.items.length > 0
    if (scene.items.length) {
      const min=[Infinity,Infinity,Infinity],max=[-Infinity,-Infinity,-Infinity]
      for(const item of scene.items)for(let axis=0;axis<3;axis++){min[axis]=Math.min(min[axis],item.position[axis]);max[axis]=Math.max(max[axis],item.position[axis])}
      this.sceneCenter=[(min[0]+max[0])/2,(min[1]+max[1])/2,(min[2]+max[2])/2]
      if(firstContent)this.camera.target=this.sceneCenter
    }
    this.scene = scene
    this.projected = new Float32Array(scene.items.length * 4)
    this.hovered = -1; this.keyboardItem = -1; this.lastLabels = -Infinity
    this.uploadScene(); this.callbacks.onView?.(this.camera.zoom/defaultCamera().zoom);this.invalidate()
  }
  private uploadScene(): void {
    const gl = this.gl, resources = this.resources, scene = this.scene
    if (!gl || !resources || !scene || this.lost) return
    const dust=nebulaAtmosphere(scene);this.dustCount=dust.length/POINT_STRIDE
    gl.bindBuffer(gl.ARRAY_BUFFER, resources.dustBuffer); gl.bufferData(gl.ARRAY_BUFFER, dust, gl.STATIC_DRAW)
    const pins:number[]=[]
    for(let i=0;i<scene.items.length;i++)if(scene.items[i].node?.pinned)pins.push(...scene.points.slice(i*POINT_STRIDE,(i+1)*POINT_STRIDE))
    this.pinCount=pins.length/POINT_STRIDE;gl.bindBuffer(gl.ARRAY_BUFFER,resources.pinBuffer);gl.bufferData(gl.ARRAY_BUFFER,new Float32Array(pins),gl.STATIC_DRAW)
    for (const [buffer, data] of [[resources.pointBuffer, scene.points], [resources.solidBuffer, scene.solidLines], [resources.candidateBuffer, scene.candidateLines]] as const) {
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer); gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW)
    }
  }
  private uploadHighlight():void {
    const gl=this.gl,resources=this.resources,scene=this.scene
    if(!gl||!resources||!scene||this.lost)return
    const index=this.selectedId==null?undefined:scene.itemIndex.get(idKey(this.selectedId)),position=index===undefined?undefined:scene.items[index].position
    const pick=(lines:Float32Array)=>{const values:number[]=[];if(position)for(let i=0;i<lines.length;i+=8){if([0,4].some(offset=>Math.hypot(lines[i+offset]-position[0],lines[i+offset+1]-position[1],lines[i+offset+2]-position[2])<.0001)){
      const a=[lines[i],lines[i+1],lines[i+2]],b=[lines[i+4],lines[i+5],lines[i+6]],length=lines[i+7],bend=Math.min(.25,length*.1)
      const at=(t:number)=>a.map((v,axis)=>v+(b[axis]-v)*t+(axis===1?Math.sin(t*Math.PI)*bend:0))
      for(let step=0;step<18;step++)values.push(...at(step/18),length*step/18,...at((step+1)/18),length*(step+1)/18)
    }}return values}
    const solid=pick(scene.solidLines),candidate=pick(scene.candidateLines);this.highlightSolid=solid.length/4;this.highlightCandidate=candidate.length/4
    gl.bindBuffer(gl.ARRAY_BUFFER,resources.highlightBuffer);gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([...solid,...candidate]),gl.STATIC_DRAW)
  }
  setSelected(id: NebulaId | null | undefined): void {
    const next = id ?? null, changed = next !== this.selectedId
    this.selectedId = next
    if (changed) this.pendingSelectionFocus = false
    if (this.pendingSelectionFocus && next !== null) {
      const index = this.scene?.itemIndex.get(idKey(next))
      if (index !== undefined && this.scene) { this.camera.target = this.scene.items[index].position; this.pendingSelectionFocus = false }
    }
    this.uploadHighlight();this.lastLabels = -Infinity; this.lastMetrics = -Infinity; this.invalidate()
  }
  focusAt(position: Vec3): void { this.camera.target=position; this.camera.zoom=Math.max(this.camera.zoom,1.65);this.callbacks.onView?.(this.camera.zoom/defaultCamera().zoom); this.invalidate() }
  focusSelected(): void {
    this.pendingSelectionFocus = this.selectedId !== null
    this.setSelected(this.selectedId)
  }
  setAutoRotate(value: boolean): void { this.autoRotate = value && !this.reducedMotion; this.invalidate() }
  setAtmosphere(value:boolean):void{this.showAtmosphere=value;this.invalidate()}
  setShowEdges(value: boolean): void { this.showEdges = value; this.invalidate() }
  zoomBy(factor: number): void { this.camera.zoom = clamp(this.camera.zoom * factor, .45, 3.8);this.callbacks.onView?.(this.camera.zoom/defaultCamera().zoom); this.invalidate() }
  reset(): void { this.camera = {...defaultCamera(),target:this.sceneCenter}; this.keyboardItem = -1;this.callbacks.onView?.(1); this.invalidate() }
  private resize = (): void => {
    if (this.disposed) return
    const rect = this.canvas.getBoundingClientRect()
    this.width = Math.max(1, rect.width); this.height = Math.max(1, rect.height)
    // A DPR cap and a 2.5M-pixel budget prevent pathological backing-store sizes.
    this.dpr = Math.max(.25, Math.min(MAX_SCENE_DPR, window.devicePixelRatio || 1, Math.sqrt(2500000 / (this.width * this.height))))
    const width = Math.max(1, Math.round(this.width * this.dpr)), height = Math.max(1, Math.round(this.height * this.dpr))
    if (this.canvas.width !== width || this.canvas.height !== height) { this.canvas.width = width; this.canvas.height = height }
    this.lastLabels = -Infinity; this.lastMetrics = -Infinity; this.invalidate()
  }
  private canRender(): boolean { return !this.disposed && !this.lost && Boolean(this.resources) && !document.hidden && this.intersecting }
  private invalidate(): void { if (this.canRender() && this.raf === null) this.raf = window.requestAnimationFrame(this.draw) }
  private stop(): void { if (this.raf !== null) window.cancelAnimationFrame(this.raf); this.raf = null; this.lastFrame = 0 }
  private syncPause(): void { this.cancelPointer(); if (!this.canRender()) this.stop(); else this.invalidate() }
  private onVisibility = (): void => this.syncPause()
  private onReducedMotion = (): void => { this.reducedMotion = this.media?.matches ?? true; if (this.reducedMotion) { this.autoRotate = false; this.stop() }; this.invalidate() }
  private onContextLost = (event: Event): void => {
    event.preventDefault(); this.lost = true; this.stop(); this.cancelPointer()
    // A lost context invalidates its own objects. They must not be reused on restore.
    this.resources = null; this.enabledAttributes.clear(); this.callbacks.onLabels([]); this.callbacks.onStatus('lost')
  }
  private onContextRestored = (): void => { this.lost = false; this.initialize() }
  private useProgram(program: Program): void {
    const gl = this.gl!
    // Disable stale point attributes before a smaller fog/line buffer is bound on the next frame.
    for (const location of this.enabledAttributes) gl.disableVertexAttribArray(location)
    this.enabledAttributes.clear(); gl.useProgram(program.value)
  }
  private attribute(program: Program, name: string, size: number, stride: number, offset: number): void {
    const gl = this.gl!, location = program.attributes[name]
    if (location < 0) return
    gl.enableVertexAttribArray(location); this.enabledAttributes.add(location)
    gl.vertexAttribPointer(location, size, gl.FLOAT, false, stride, offset)
  }
  private drawLines(buffer: WebGLBuffer, vertices: number, color: readonly [number, number, number, number], candidate: boolean, start=0): void {
    if (!vertices) return
    const gl = this.gl!, program = this.resources!.lines
    this.useProgram(program); gl.bindBuffer(gl.ARRAY_BUFFER, buffer)
    this.attribute(program, 'a_position', 3, 16, 0); this.attribute(program, 'a_distance', 1, 16, 12)
    gl.uniformMatrix4fv(program.uniforms.u_matrix, false, this.matrix)
    gl.uniform4f(program.uniforms.u_color, ...color); gl.uniform1f(program.uniforms.u_candidate, candidate ? 1 : 0)
    gl.drawArrays(gl.LINES, start, vertices)
  }
  private draw = (time: number): void => {
    this.raf = null
    if (!this.canRender()) return
    const started = performance.now(), gl = this.gl!, resources = this.resources!
    const dark=true
    const elapsed = this.lastFrame ? Math.min(50, time - this.lastFrame) : 0
    this.lastFrame = time
    if (this.autoRotate && !this.reducedMotion && !this.pointer) this.camera.yaw += elapsed * .000025
    cameraMatrix(this.camera, this.width / this.height, this.matrix)
    gl.viewport(0, 0, this.canvas.width, this.canvas.height); gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT)
    gl.disable(gl.DEPTH_TEST); gl.disable(gl.BLEND)
    this.useProgram(resources.fog); gl.bindBuffer(gl.ARRAY_BUFFER, resources.quadBuffer); this.attribute(resources.fog, 'a_position', 2, 8, 0)
    gl.uniform1f(resources.fog.uniforms.u_dark,dark?1:0); gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4)
    gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA)

    const scene = this.scene
    if (scene) {

      if(this.showEdges&&this.selectedId!=null){const selected=scene.items[scene.itemIndex.get(idKey(this.selectedId))??-1];const color=hexRgb(selected?.color||'#b298e6');this.drawLines(resources.highlightBuffer,this.highlightSolid,[...color,.7],false);this.drawLines(resources.highlightBuffer,this.highlightCandidate,[...color,.5],true,this.highlightSolid)}
      // Additive, non-depth-writing point sprites do not let transparent halos occlude real nodes.
      gl.disable(gl.DEPTH_TEST); gl.blendFunc(gl.SRC_ALPHA, gl.ONE)
      this.useProgram(resources.points); gl.bindBuffer(gl.ARRAY_BUFFER, resources.dustBuffer)
      const stride = POINT_STRIDE * 4
      this.attribute(resources.points, 'a_position', 3, stride, 0); this.attribute(resources.points, 'a_color', 3, stride, 12)
      this.attribute(resources.points, 'a_size', 1, stride, 24); this.attribute(resources.points, 'a_light', 1, stride, 28); this.attribute(resources.points, 'a_index', 1, stride, 32)
      gl.uniformMatrix4fv(resources.points.uniforms.u_matrix, false, this.matrix); gl.uniform1f(resources.points.uniforms.u_dpr, this.dpr)
      gl.uniform1f(resources.points.uniforms.u_dark,dark?1:0)
      gl.uniform1f(resources.points.uniforms.u_maxPoint, this.maxPoint)
      const selectedIndex = this.selectedId === null ? undefined : scene.itemIndex.get(idKey(this.selectedId))
      gl.uniform1f(resources.points.uniforms.u_selected, this.keyboardItem >= 0 ? this.keyboardItem : selectedIndex ?? -1)
      gl.uniform1f(resources.points.uniforms.u_pinPass,0)
      gl.uniform1f(resources.points.uniforms.u_selected,-1)
      if(this.showAtmosphere)gl.drawArrays(gl.POINTS,0,this.dustCount)
      gl.bindBuffer(gl.ARRAY_BUFFER, resources.pointBuffer)
      this.attribute(resources.points, 'a_position', 3, stride, 0); this.attribute(resources.points, 'a_color', 3, stride, 12)
      this.attribute(resources.points, 'a_size', 1, stride, 24); this.attribute(resources.points, 'a_light', 1, stride, 28); this.attribute(resources.points, 'a_index', 1, stride, 32)
      gl.uniform1f(resources.points.uniforms.u_selected,this.keyboardItem >= 0 ? this.keyboardItem : selectedIndex ?? -1)
      gl.drawArrays(gl.POINTS, 0, scene.items.length)
      if(this.pinCount){
        gl.bindBuffer(gl.ARRAY_BUFFER,resources.pinBuffer)
        this.attribute(resources.points,'a_position',3,stride,0);this.attribute(resources.points,'a_color',3,stride,12);this.attribute(resources.points,'a_size',1,stride,24);this.attribute(resources.points,'a_light',1,stride,28);this.attribute(resources.points,'a_index',1,stride,32)
        gl.uniform1f(resources.points.uniforms.u_pinPass,1);gl.drawArrays(gl.POINTS,0,this.pinCount)
      }
      for (let index = 0; index < scene.items.length; index++) projectPoint(scene.items[index].position, this.matrix, this.width, this.height, this.projected, index * 4, scene.points[index * POINT_STRIDE + 6])
      if (time - this.lastLabels >= 100) { this.emitLabels(selectedIndex); this.lastLabels = time }
    }
    if (time - this.lastMetrics > 1000) { this.callbacks.onMetrics?.({ renderMs: performance.now() - started, dpr: this.dpr, points: scene?.items.length || 0 }); this.lastMetrics = time }
    if (this.autoRotate && !this.reducedMotion) this.invalidate()
  }
  private emitLabels(selectedIndex?: number): void {
    if (!this.scene) return
    const candidates: NebulaLabel[] = [], seen = new Set<number>()
    const add = (index: number): void => {
      if (seen.has(index) || index < 0 || index >= this.scene!.items.length) return
      seen.add(index)
      const offset = index * 4, item = this.scene!.items[index]
      if (this.projected[offset + 2] <= 0) return
      const x = this.projected[offset], y = this.projected[offset + 1]
      if (x < 0 || x > this.width || y < 0 || y > this.height) return
      candidates.push({ key: item.key, title: item.kind === 'group' ? `${nebulaThemeLabel(item.title)} · ${item.count}` : item.title, group: item.kind === 'group', count: item.count, color:LAYER_COLORS[item.layer], pinned:item.node?.pinned, x, y })
    }
    if (selectedIndex !== undefined) add(selectedIndex)
    add(this.hovered); add(this.keyboardItem)
    for (let index = 0; index < this.scene.items.length && candidates.length < 16; index++) if (this.scene.items[index].kind === 'group') add(index)
    for(const root of this.scene.roots){
      const p=new Float32Array(4);projectPoint(root.position,this.matrix,this.width,this.height,p)
      if(p[2]>0)candidates.push({key:`cluster-layer:${root.layer}`,title:`${LAYER_NAMES[root.layer]} · ${root.count}`,group:true,count:root.count,color:LAYER_COLORS[root.layer],x:p[0],y:p[1]})
    }
    this.callbacks.onLabels(placeNebulaLabels(candidates, this.width, this.height))
  }
  private pickIndex(x: number, y: number): number {
    let chosen = -1, best = Infinity
    for (let index = 0; index < (this.scene?.items.length || 0); index++) {
      const offset = index * 4
      if (this.projected[offset + 2] <= 0) continue
      const distance = Math.hypot(x - this.projected[offset], y - this.projected[offset + 1])
      const radius = Math.max(9, this.projected[offset + 3] * .75)
      const score = distance / radius + this.projected[offset + 2] * .0001
      if (distance <= radius && score < best) { chosen = index; best = score }
    }
    return chosen
  }
  private onPointerDown = (event: Event): void => {
    const e = event as PointerEvent
    if (e.button !== 0 || this.pointer || !this.canRender()) return
    this.canvas.focus({ preventScroll: true }); this.canvas.setPointerCapture?.(e.pointerId)
    this.pointer = { id: e.pointerId, x: e.clientX, y: e.clientY, startX: e.clientX, startY: e.clientY, moved: false }
  }
  private onPointerMove = (event: Event): void => {
    const e = event as PointerEvent, pointer = this.pointer
    if (pointer) {
      if (e.pointerId !== pointer.id) return
      const dx = e.clientX - pointer.x, dy = e.clientY - pointer.y
      if (Math.hypot(e.clientX - pointer.startX, e.clientY - pointer.startY) > 4) pointer.moved = true
      if (pointer.moved) { this.camera.yaw += dx * .006; this.camera.pitch = clamp(this.camera.pitch + dy * .006, -1.4, 1.4) }
      pointer.x = e.clientX; pointer.y = e.clientY; this.invalidate()
    } else {
      const rect = this.canvas.getBoundingClientRect(), next = this.pickIndex(e.clientX - rect.left, e.clientY - rect.top)
      if (this.hovered !== next) { this.hovered = next; this.lastLabels = -Infinity; this.lastMetrics = -Infinity; this.invalidate() }
    }
  }
  private onPointerUp = (event: Event): void => {
    const e = event as PointerEvent
    if (!this.pointer || this.pointer.id !== e.pointerId) return
    const moved = this.pointer.moved; this.cancelPointer()
    if (!moved && this.scene) {
      const rect = this.canvas.getBoundingClientRect(), index = this.pickIndex(e.clientX - rect.left, e.clientY - rect.top)
      if (index >= 0) this.callbacks.onPick(this.scene.items[index]);else this.callbacks.onClearSelection?.()
    }
  }
  private cancelPointer = (): void => {
    const pointer = this.pointer; this.pointer = null
    if (pointer && this.canvas.hasPointerCapture?.(pointer.id)) this.canvas.releasePointerCapture?.(pointer.id)
  }
  private onPointerLeave = (): void => { if (!this.pointer) { this.hovered = -1; this.lastLabels = -Infinity; this.lastMetrics = -Infinity; this.invalidate() } }
  private onWheel = (event: Event): void => { const e = event as WheelEvent; if (e.ctrlKey || e.metaKey) return; e.preventDefault(); this.zoomBy(Math.exp(-clamp(e.deltaY, -200, 200) * .0015)) }
  private onBlur = (): void => { this.keyboardItem = -1; this.cancelPointer(); this.lastLabels = -Infinity; this.lastMetrics = -Infinity; this.invalidate() }
  private onKeyDown = (event: Event): void => {
    const e = event as KeyboardEvent
    if (e.altKey || e.ctrlKey || e.metaKey) return
    // Tab intentionally remains native, so the canvas can never trap keyboard focus.
    if (e.key === 'Tab') return
    if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', '+', '=', '-', '_', 'Enter', 'Home', 'Escape'].includes(e.key)) return
    e.preventDefault()
    if (e.key === '+' || e.key === '=') this.zoomBy(1.18)
    else if (e.key === '-' || e.key === '_') this.zoomBy(1 / 1.18)
    else if (e.key === 'Home') this.reset()
    else if (e.key === 'Escape') { this.keyboardItem = -1; this.hovered = -1;this.callbacks.onClearSelection?.() }
    else if (e.key === 'Enter') {
      const selectedIndex = this.selectedId == null ? undefined : this.scene?.itemIndex.get(idKey(this.selectedId))
      const index = this.keyboardItem >= 0 ? this.keyboardItem : selectedIndex ?? (this.scene?.items.length ? 0 : -1)
      if (index >= 0 && this.scene) this.callbacks.onPick(this.scene.items[index])
    } else if (e.shiftKey && this.scene?.items.length) {
      const delta = e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 1
      this.keyboardItem = (this.keyboardItem + delta + this.scene.items.length) % this.scene.items.length
    } else {
      if (e.key === 'ArrowLeft') this.camera.yaw -= .1
      if (e.key === 'ArrowRight') this.camera.yaw += .1
      if (e.key === 'ArrowUp') this.camera.pitch = clamp(this.camera.pitch - .1, -1.4, 1.4)
      if (e.key === 'ArrowDown') this.camera.pitch = clamp(this.camera.pitch + .1, -1.4, 1.4)
    }
    this.lastLabels = -Infinity; this.lastMetrics = -Infinity; this.invalidate()
  }
  dispose(): void {
    this.themeObserver?.disconnect()
    if (this.disposed) return
    this.disposed = true; this.stop(); this.cancelPointer()
    this.removeListeners.splice(0).forEach(remove => remove())
    this.resizeObserver?.disconnect(); this.intersectionObserver?.disconnect()
    this.resizeObserver = null; this.intersectionObserver = null
    const gl = this.gl, resources = this.resources
    if (gl && resources && !this.lost) {
      // StrictMode may mount a new renderer onto this same canvas/context after cleanup.
      for (const location of this.enabledAttributes) gl.disableVertexAttribArray(location)
      gl.bindBuffer(gl.ARRAY_BUFFER, null); gl.useProgram(null)
      for (const buffer of [resources.pointBuffer, resources.solidBuffer, resources.candidateBuffer, resources.dustBuffer, resources.pinBuffer, resources.highlightBuffer, resources.quadBuffer]) gl.deleteBuffer(buffer)
      for (const program of [resources.points, resources.lines, resources.fog]) gl.deleteProgram(program.value)
    }
    this.enabledAttributes.clear()
    this.resources = null; this.gl = null; this.scene = null; this.projected = new Float32Array(0)
  }
}
