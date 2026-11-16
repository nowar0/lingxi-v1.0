import { clamp, idKey, LAYER_COLORS, LAYER_NAMES, placeNebulaLabels, POINT_STRIDE, stableHash } from '../../lib/nebula';
import type { NebulaId, NebulaLabel, NebulaScene, Vec3 } from '../../lib/nebula';
import type { NebulaRendererCallbacks } from './nebula-renderer';
type Particle = {
    position: Vec3;
    size: number;
    alpha: number;
    color: string;
};
type SkyStar = {
    x: number;
    y: number;
    size: number;
    alpha: number;
    phase: number;
};
type Projected = {
    x: number;
    y: number;
    depth: number;
    scale: number;
};
type Edge = {
    a: Vec3;
    b: Vec3;
    candidate: boolean;
    color: string;
};
const random = (seed: number) => () => { seed += 0x6D2B79F5; let n = seed; n = Math.imul(n ^ n >>> 15, n | 1); n ^= n + Math.imul(n ^ n >>> 7, n | 61); return ((n ^ n >>> 14) >>> 0) / 4294967296; };
const xyz = (p: Vec3): Vec3 => [p[0] * 120, -p[1] * 120, p[2] * 120];
const rgb = (color: string) => `${parseInt(color.slice(1, 3), 16)},${parseInt(color.slice(3, 5), 16)},${parseInt(color.slice(5, 7), 16)}`;
/** The supplied preview's 3D projection, layered sprites and easing, driven only by the real scene.
 * Decorative dust has no ID, picking target, relationship or effect on memory counts. */
export class NebulaPreviewRenderer {
    private ctx: CanvasRenderingContext2D | null;
    private scene: NebulaScene | null = null;
    private selected: NebulaId | null = null;
    private hovered = -1;
    private keyboard = -1;
    private yaw = .12;
    private pitch = -.12;
    private targetYaw = .12;
    private targetPitch = -.12;
    private zoom = 1;
    private targetZoom = 1;
    private offset = [0, 0, 0];
    private target = [0, 0, 0];
    private width = 1;
    private height = 1;
    private dpr = 1;
    private frame: number | null = null;
    private lastFrame = 0;
    private lastLabels = -Infinity;
    private lastMetrics = -Infinity;
    private lastZoom = -1;
    private disposed = false;
    private intersecting = true;
    private reduced = false;
    private auto = false;
    private atmosphere = true;
    private edgesVisible = true;
    private backdrop: HTMLCanvasElement | null = null;
    private sprites = new Map<string, HTMLCanvasElement>();
    private sky: SkyStar[] = [];
    private dust: Particle[] = [];
    private positions: Vec3[] = [];
    private projections: Projected[] = [];
    private edges: Edge[] = [];
    private removals: Array<() => void> = [];
    private resizeObserver: ResizeObserver | null = null;
    private intersectionObserver: IntersectionObserver | null = null;
    private themeObserver: MutationObserver | null = null;
    private media: MediaQueryList;
    private pointers = new Map<number, {
        x: number;
        y: number;
    }>();
    private drag: {
        id: number;
        x: number;
        y: number;
        startX: number;
        startY: number;
        moved: boolean;
    } | null = null;
    private pinch = 0;
    constructor(private canvas: HTMLCanvasElement, private callbacks: NebulaRendererCallbacks) {
        this.ctx = canvas.getContext('2d', { alpha: false });
        this.media = window.matchMedia('(prefers-reduced-motion: reduce)');
        this.updateMotion();
        const rand = random(717);
        for (let i = 0; i < 640; i++)
            this.sky.push({ x: rand(), y: rand(), size: rand() > .97 ? 1.25 : .25 + rand() * .55, alpha: .1 + rand() * .46, phase: rand() * 7 });
        this.listen(canvas, 'contextlost', this.contextLost);
        this.listen(canvas, 'contextrestored', this.contextRestored);
        this.listen(canvas, 'pointerdown', this.pointerDown);
        this.listen(canvas, 'pointermove', this.pointerMove);
        this.listen(canvas, 'pointerup', this.pointerEnd);
        this.listen(canvas, 'pointercancel', this.pointerCancel);
        this.listen(canvas, 'lostpointercapture', this.pointerCancel);
        this.listen(canvas, 'pointerleave', this.pointerLeave);
        this.listen(canvas, 'wheel', this.wheel, { passive: false });
        this.listen(canvas, 'keydown', this.keyDown);
        this.listen(canvas, 'blur', this.blur);
        this.listen(document, 'visibilitychange', this.syncPause);
        this.listen(window, 'resize', this.resize);
        this.listen(this.media, 'change', this.updateMotion);
        this.themeObserver = new MutationObserver(this.updateMotion);
        this.themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
        this.resizeObserver = new ResizeObserver(this.resize);
        this.resizeObserver.observe(canvas);
        this.intersectionObserver = new IntersectionObserver(entries => { this.intersecting = entries.some(e => e.isIntersecting); this.syncPause(); });
        this.intersectionObserver.observe(canvas);
        if (this.ctx) {
            canvas.dataset.renderer = 'preview-canvas-3d';
            this.callbacks.onStatus('ready');
            this.resize();
        }
        else
            this.callbacks.onStatus('unavailable');
    }
    private contextLost = (event: Event) => { event.preventDefault(); this.stop(); this.ctx = null; this.callbacks.onStatus('lost'); };
    private contextRestored = () => { if (this.disposed)
        return; this.ctx = this.canvas.getContext('2d', { alpha: false }); if (this.ctx) {
        this.callbacks.onStatus('ready');
        this.resize();
    }
    else
        this.callbacks.onStatus('unavailable'); };
    private listen(target: EventTarget, event: string, listener: EventListenerOrEventListenerObject, options?: AddEventListenerOptions) { target.addEventListener(event, listener, options); this.removals.push(() => target.removeEventListener(event, listener, options)); }
    setScene(scene: NebulaScene) {
        this.scene = scene;
        this.positions = scene.items.map(item => xyz(item.position));
        this.hovered = -1;
        this.keyboard = -1;
        this.lastLabels = -Infinity;
        const dust: Particle[] = [], rand = random(717);
        for (const root of scene.roots) {
            const c = xyz(root.position), r = random(stableHash(root.key));
            for (let i = 0; i < 680; i++) {
                const a = r() * Math.PI * 2, radius = Math.pow(r(), .65) * 130, spiral = a + radius * .018;
                dust.push({ position: [c[0] + Math.cos(spiral) * radius, c[1] + Math.sin(spiral) * radius * .67, c[2] + (r() + r() + r() - 1.5) * 78], size: .45 + r() * 1.35, alpha: .08 + r() * .35, color: root.color });
            }
        }
        if (scene.roots.length)
            for (let i = 0; i < 1100; i++) {
                const t = rand() * Math.PI * 4, r = 40 + t * 17;
                dust.push({ position: [Math.cos(t) * r + (rand() - .5) * 45, Math.sin(t) * r * .58 + (rand() - .5) * 45, (rand() - .5) * 110], size: .35 + rand(), alpha: .1 + rand() * .2, color: i % 3 ? '#b298e6' : '#87b3d7' });
            }
        this.dust = dust;
        this.edges = [];
        const colors = new Map(scene.items.map((item, index) => {
            const offset = index * POINT_STRIDE;
            return [Array.from(scene.points.slice(offset, offset + 3)).join(':'), item.color];
        }));
        for (const [data, candidate] of [[scene.solidLines, false], [scene.candidateLines, true]] as const)
            for (let i = 0; i < data.length; i += 8)
                this.edges.push({ a: xyz([data[i], data[i + 1], data[i + 2]]), b: xyz([data[i + 4], data[i + 5], data[i + 6]]), candidate, color: colors.get([data[i], data[i + 1], data[i + 2]].join(':')) || '#b298e6' });
        this.invalidate();
    }
    setSelected(id: NebulaId | null | undefined) { this.selected = id ?? null; this.lastLabels = -Infinity; this.invalidate(); }
    setAutoRotate(value: boolean) { this.auto = value; this.invalidate(); }
    setShowEdges(value: boolean) { this.edgesVisible = value; this.invalidate(); }
    setAtmosphere(value: boolean) { this.atmosphere = value; this.invalidate(); }
    focusSelected() { const i = this.selected == null ? undefined : this.scene?.itemIndex.get(idKey(this.selected)); if (i !== undefined && this.scene)
        this.focusAt(this.scene.items[i].position); }
    focusAt(position: Vec3) { this.target = [...xyz(position)]; this.targetZoom = Math.max(1.25, this.targetZoom); this.snapReduced(); this.invalidate(); }
    zoomBy(factor: number) { if (!Number.isFinite(factor) || factor <= 0)
        return; this.targetZoom = clamp(this.targetZoom * factor, .45, 3); this.snapReduced(); this.invalidate(); }
    reset() { this.targetYaw = .12; this.targetPitch = -.12; this.targetZoom = 1; this.target = [0, 0, 0]; this.snapReduced(); this.invalidate(); }
    private snapReduced() { if (this.reduced) {
        this.yaw = this.targetYaw;
        this.pitch = this.targetPitch;
        this.zoom = this.targetZoom;
        this.offset = [...this.target];
    } }
    private updateMotion = () => { this.reduced = this.media?.matches || document.documentElement.classList.contains('reduce-motion'); this.snapReduced(); this.syncPause(); };
    private active() { return !this.disposed && !!this.ctx && !document.hidden && this.intersecting && this.width > 0 && this.height > 0; }
    private stop() { if (this.frame !== null)
        cancelAnimationFrame(this.frame); this.frame = null; this.lastFrame = 0; }
    private syncPause = () => { if (this.active())
        this.invalidate();
    else
        this.stop(); };
    private invalidate() { if (this.active() && this.frame === null)
        this.frame = requestAnimationFrame(this.draw); }
    private resize = () => {
        if (this.disposed || !this.ctx)
            return;
        const r = this.canvas.getBoundingClientRect();
        this.width = r.width;
        this.height = r.height;
        this.dpr = Math.min(window.devicePixelRatio || 1, 2);
        this.canvas.width = Math.max(1, Math.round(r.width * this.dpr));
        this.canvas.height = Math.max(1, Math.round(r.height * this.dpr));
        this.ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
        this.backdrop = document.createElement('canvas');
        this.backdrop.width = this.canvas.width;
        this.backdrop.height = this.canvas.height;
        const b = this.backdrop.getContext('2d')!;
        b.scale(this.dpr, this.dpr);
        b.fillStyle = '#090c16';
        b.fillRect(0, 0, this.width, this.height);
        const haze = (x: number, y: number, radius: number, color: string) => { if (radius <= 0)
            return; const g = b.createRadialGradient(x, y, 0, x, y, radius); g.addColorStop(0, color); g.addColorStop(1, 'transparent'); b.fillStyle = g; b.fillRect(0, 0, this.width, this.height); };
        haze(this.width * .53, this.height * .58, Math.max(this.width, this.height) * .49, '#4a346218');
        haze(this.width * .29, this.height * .66, this.width * .3, '#21425817');
        this.lastLabels = -Infinity;
        this.invalidate();
    };
    private sprite(color: string) { let sprite = this.sprites.get(color); if (sprite)
        return sprite; sprite = document.createElement('canvas'); sprite.width = sprite.height = 64; const c = sprite.getContext('2d')!, g = c.createRadialGradient(32, 32, 0, 32, 32, 32), rgbColor = rgb(color); g.addColorStop(0, `rgba(${rgbColor},.8)`); g.addColorStop(.13, `rgba(${rgbColor},.32)`); g.addColorStop(.42, `rgba(${rgbColor},.085)`); g.addColorStop(1, `rgba(${rgbColor},0)`); c.fillStyle = g; c.fillRect(0, 0, 64, 64); this.sprites.set(color, sprite); return sprite; }
    private project(p: Vec3): Projected { const x = p[0] - this.offset[0], y = p[1] - this.offset[1], z = p[2] - this.offset[2], cos = Math.cos(this.yaw), sin = Math.sin(this.yaw), cp = Math.cos(this.pitch), sp = Math.sin(this.pitch), xx = x * cos - z * sin, zz = x * sin + z * cos, yy = y * cp - zz * sp, depth = y * sp + zz * cp, perspective = 780 / Math.max(300, 780 + depth), fit = Math.min(this.width / 670, this.height / (this.width < 600 ? 470 : 570)) * this.zoom; return { x: this.width * .51 + xx * fit * perspective, y: this.height * (this.width < 600 ? .62 : .59) + yy * fit * perspective, depth, scale: fit * perspective }; }
    private draw = (time: number) => {
        this.frame = null;
        if (!this.active())
            return;
        if (this.lastFrame && time - this.lastFrame < 30) {
            this.invalidate();
            return;
        }
        const start = performance.now(), dt = Math.min(65, this.lastFrame ? time - this.lastFrame : 32);
        this.lastFrame = time;
        if (!this.reduced && this.auto && !this.drag && !document.querySelector('[role="dialog"]'))
            this.targetYaw += dt * .000025;
        const cameraEase = this.drag || this.reduced ? 1 : 1 - Math.pow(.82, dt / 32), zoomEase = this.reduced ? 1 : 1 - Math.pow(.86, dt / 32), focusEase = this.reduced ? 1 : 1 - Math.pow(.91, dt / 32);
        this.yaw += (this.targetYaw - this.yaw) * cameraEase;
        this.pitch += (this.targetPitch - this.pitch) * cameraEase;
        this.zoom += (this.targetZoom - this.zoom) * zoomEase;
        for (let i = 0; i < 3; i++)
            this.offset[i] += (this.target[i] - this.offset[i]) * focusEase;
        const c = this.ctx!, w = this.width, h = this.height;
        c.globalAlpha = 1;
        c.globalCompositeOperation = 'source-over';
        if (this.backdrop)
            c.drawImage(this.backdrop, 0, 0, w, h);
        for (const star of this.sky) {
            c.globalAlpha = star.alpha * (this.reduced ? 1 : .84 + .16 * Math.sin(time / 2300 + star.phase));
            c.fillStyle = '#9ba6ce';
            c.fillRect(star.x * w, star.y * h, star.size, star.size);
        }
        if (this.atmosphere) {
            c.globalCompositeOperation = 'screen';
            for (const root of this.scene?.roots || []) {
                const p = this.project(xyz(root.position)), r = 240 * p.scale;
                c.globalAlpha = .17;
                c.drawImage(this.sprite(root.color), p.x - r, p.y - r * .7, r * 2, r * 1.4);
            }
            for (const dust of this.dust) {
                const p = this.project(dust.position);
                if (p.x < -5 || p.x > w + 5 || p.y < -5 || p.y > h + 5)
                    continue;
                const size = Math.max(.35, dust.size * p.scale);
                c.globalAlpha = dust.alpha;
                c.fillStyle = dust.color;
                c.fillRect(p.x, p.y, size, size);
            }
            c.globalCompositeOperation = 'source-over';
        }
        this.projections = this.positions.map(p => this.project(p));
        const selectedIndex = this.selected == null ? undefined : this.scene?.itemIndex.get(idKey(this.selected)), hotPositions = [selectedIndex, this.hovered, this.keyboard].filter((i): i is number => i !== undefined && i >= 0).map(i => this.positions[i]).filter(Boolean);
        if (this.edgesVisible)
            for (const edge of this.edges) {
                const a = this.project(edge.a), b = this.project(edge.b), active = hotPositions.some(p => [edge.a, edge.b].some(q => Math.hypot(p[0] - q[0], p[1] - q[1], p[2] - q[2]) < .01)), distance = Math.hypot(a.x - b.x, a.y - b.y);
                c.strokeStyle = edge.color;
                c.globalAlpha = active ? .48 : !this.atmosphere ? .16 : Math.max(.025, .10 - distance / 2200);
                c.lineWidth = active ? .85 : .45;
                c.setLineDash(edge.candidate ? [3, 5] : []);
                c.beginPath();
                c.moveTo(a.x, a.y);
                c.quadraticCurveTo((a.x + b.x) / 2, (a.y + b.y) / 2 - Math.min(20, distance * .07), b.x, b.y);
                c.stroke();
            }
        ;
        c.setLineDash([]);
        const order = this.projections.map((p, i) => ({ p, i })).sort((a, b) => b.p.depth - a.p.depth);
        for (const { p, i } of order) {
            if (p.x < -30 || p.x > w + 30 || p.y < -30 || p.y > h + 30)
                continue;
            const item = this.scene!.items[i], hot = i === selectedIndex || i === this.hovered || i === this.keyboard, size = Math.max(1.6, (.9 + item.importance * 2.6) * p.scale) * (item.kind === 'group' ? 1.6 : 1), halo = (hot ? 22 : 11) * Math.max(.7, p.scale);
            c.globalAlpha = hot ? 1 : .5 + item.activation * .45;
            c.drawImage(this.sprite(item.color), p.x - halo, p.y - halo, halo * 2, halo * 2);
            c.fillStyle = item.color;
            c.beginPath();
            c.arc(p.x, p.y, size, 0, Math.PI * 2);
            c.fill();
            c.fillStyle = '#f4ebff';
            c.globalAlpha = hot ? .98 : .6;
            c.beginPath();
            c.arc(p.x - size * .12, p.y - size * .12, size * .35, 0, Math.PI * 2);
            c.fill();
            if (hot || item.node?.pinned) {
                c.globalAlpha = hot ? .5 : .32;
                c.strokeStyle = item.color;
                c.lineWidth = .65;
                c.beginPath();
                c.arc(p.x, p.y, size + 7 + (this.reduced || !hot ? 0 : Math.sin(time / 700) * 1.4), 0, Math.PI * 2);
                c.stroke();
                if (hot) {
                    c.beginPath();
                    c.moveTo(p.x - size - 12, p.y);
                    c.lineTo(p.x - size - 8, p.y);
                    c.moveTo(p.x + size + 8, p.y);
                    c.lineTo(p.x + size + 12, p.y);
                    c.stroke();
                }
            }
        }
        c.globalAlpha = 1;
        if (time - this.lastLabels >= 100) {
            this.lastLabels = time;
            this.publishLabels(selectedIndex);
        }
        if (Math.abs(this.lastZoom - this.zoom) > .002) {
            this.lastZoom = this.zoom;
            this.callbacks.onView?.(this.zoom);
        }
        if (time - this.lastMetrics >= 500) {
            this.lastMetrics = time;
            this.callbacks.onMetrics?.({ renderMs: performance.now() - start, dpr: this.dpr, points: this.scene?.items.length || 0 });
        }
        if (!this.reduced || this.moving())
            this.invalidate();
    };
    private moving() { return Math.abs(this.targetZoom - this.zoom) > .0001 || Math.abs(this.targetYaw - this.yaw) > .0001 || Math.abs(this.targetPitch - this.pitch) > .0001 || this.target.some((v, i) => Math.abs(v - this.offset[i]) > .01); }
    private publishLabels(selectedIndex: number | undefined) {
        const candidates: NebulaLabel[] = [], scene = this.scene;
        if (!scene)
            return;
        const append = (i: number) => { const item = scene.items[i], p = this.projections[i]; if (item && p)
            candidates.push({ key: item.key, title: item.title, x: p.x, y: p.y, group: item.kind === 'group', count: item.count, color: item.color, pinned: item.node?.pinned }); };
        if (selectedIndex !== undefined)
            append(selectedIndex);
        if (this.hovered >= 0 && this.hovered !== selectedIndex)
            append(this.hovered);
        if (this.keyboard >= 0 && this.keyboard !== selectedIndex && this.keyboard !== this.hovered)
            append(this.keyboard);
        for (const root of scene.roots) {
            const p = this.project(xyz(root.position));
            candidates.push({ key: `cluster-layer:${root.layer}`, title: `${LAYER_NAMES[root.layer]} · ${root.count}`, x: p.x, y: p.y, group: true, count: root.count, color: LAYER_COLORS[root.layer] });
        }
        this.callbacks.onLabels(placeNebulaLabels(candidates, this.width, this.height));
    }
    private hit(x: number, y: number) { let chosen = -1, distance = 15; this.projections.forEach((p, i) => { const d = Math.hypot(p.x - x, p.y - y); if (d < distance) {
        distance = d;
        chosen = i;
    } }); return chosen; }
    private local(e: PointerEvent) { const r = this.canvas.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; }
    private pointerDown = (event: Event) => { const e = event as PointerEvent; if (e.pointerType === 'mouse' && e.button !== 0)
        return; const p = this.local(e); this.pointers.set(e.pointerId, p); this.canvas.focus({ preventScroll: true }); this.canvas.setPointerCapture?.(e.pointerId); if (!this.drag)
        this.drag = { id: e.pointerId, ...p, startX: p.x, startY: p.y, moved: false }; if (this.pointers.size === 2) {
        const [a, b] = [...this.pointers.values()];
        this.pinch = Math.hypot(a.x - b.x, a.y - b.y);
        this.drag!.moved = true;
    } this.canvas.style.cursor = 'grabbing'; };
    private pointerMove = (event: Event) => { const e = event as PointerEvent, p = this.local(e); if (this.pointers.has(e.pointerId))
        this.pointers.set(e.pointerId, p); if (this.pointers.size > 1) {
        const [a, b] = [...this.pointers.values()], distance = Math.hypot(a.x - b.x, a.y - b.y);
        if (this.pinch && distance)
            this.zoomBy(distance / this.pinch);
        this.pinch = distance;
        return;
    } if (this.drag && this.drag.id === e.pointerId) {
        const d = this.drag;
        if (Math.hypot(p.x - d.startX, p.y - d.startY) > 4)
            d.moved = true;
        if (d.moved) {
            this.targetYaw += (p.x - d.x) * .006;
            this.targetPitch = clamp(this.targetPitch + (p.y - d.y) * .005, -1.2, 1.2);
        }
        d.x = p.x;
        d.y = p.y;
        this.invalidate();
    }
    else {
        const next = this.hit(p.x, p.y);
        if (next !== this.hovered) {
            this.hovered = next;
            this.lastLabels = -Infinity;
            this.invalidate();
        }
        this.canvas.style.cursor = next >= 0 ? 'pointer' : 'grab';
    } };
    private pointerEnd = (event: Event) => { const e = event as PointerEvent, p = this.local(e), click = this.drag?.id === e.pointerId && !this.drag.moved && this.pointers.size === 1; this.pointers.delete(e.pointerId); if (click && this.scene) {
        const i = this.hit(p.x, p.y);
        if (i >= 0)
            this.callbacks.onPick(this.scene.items[i]);
        else
            this.callbacks.onClearSelection?.();
    } this.release(e.pointerId); if (!this.pointers.size) {
        this.drag = null;
        this.pinch = 0;
        this.canvas.style.cursor = 'grab';
    }
    else {
        const [id, p] = [...this.pointers.entries()][0];
        this.drag = { id, ...p, startX: p.x, startY: p.y, moved: true };
    } this.invalidate(); };
    private release(id: number) { if (this.canvas.hasPointerCapture?.(id))
        this.canvas.releasePointerCapture?.(id); }
    private pointerCancel = (event: Event) => { const id = (event as PointerEvent).pointerId; this.pointers.delete(id); if (this.drag?.id === id) {
        this.drag = null;
        this.pinch = 0;
    } this.canvas.style.cursor = 'grab'; this.invalidate(); };
    private pointerLeave = () => { if (!this.drag) {
        this.hovered = -1;
        this.lastLabels = -Infinity;
        this.canvas.style.cursor = 'grab';
        this.invalidate();
    } };
    private wheel = (event: Event) => { const e = event as WheelEvent; e.preventDefault(); this.zoomBy(Math.exp(-clamp(e.deltaY, -200, 200) * .0012)); };
    private blur = () => { this.keyboard = -1; this.hovered = -1; this.drag = null; for (const id of this.pointers.keys())
        this.release(id); this.pointers.clear(); this.lastLabels = -Infinity; this.invalidate(); };
    private keyDown = (event: Event) => { const e = event as KeyboardEvent; if (e.altKey || e.ctrlKey || e.metaKey || e.key === 'Tab')
        return; if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', '+', '=', '-', '_', 'Enter', 'Home', 'Escape'].includes(e.key))
        return; e.preventDefault(); if (e.key === '+' || e.key === '=')
        this.zoomBy(1.15);
    else if (e.key === '-' || e.key === '_')
        this.zoomBy(1 / 1.15);
    else if (e.key === 'Home')
        this.reset();
    else if (e.key === 'Escape') {
        this.keyboard = -1;
        this.callbacks.onClearSelection?.();
    }
    else if (e.key === 'Enter') {
        const i = this.keyboard >= 0 ? this.keyboard : this.selected == null ? 0 : this.scene?.itemIndex.get(idKey(this.selected));
        if (i !== undefined && this.scene?.items[i])
            this.callbacks.onPick(this.scene.items[i]);
    }
    else if (e.shiftKey && this.scene?.items.length) {
        this.keyboard = (this.keyboard + (e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 1) + this.scene.items.length) % this.scene.items.length;
    }
    else {
        if (e.key === 'ArrowLeft')
            this.targetYaw -= .1;
        if (e.key === 'ArrowRight')
            this.targetYaw += .1;
        if (e.key === 'ArrowUp')
            this.targetPitch = clamp(this.targetPitch - .1, -1.2, 1.2);
        if (e.key === 'ArrowDown')
            this.targetPitch = clamp(this.targetPitch + .1, -1.2, 1.2);
    } this.snapReduced(); this.lastLabels = -Infinity; this.invalidate(); };
    dispose() { if (this.disposed)
        return; this.disposed = true; this.stop(); this.blur(); this.removals.splice(0).forEach(remove => remove()); this.resizeObserver?.disconnect(); this.intersectionObserver?.disconnect(); this.themeObserver?.disconnect(); this.sprites.clear(); this.ctx = null; this.scene = null; this.dust = []; this.projections = []; this.backdrop = null; }
}
