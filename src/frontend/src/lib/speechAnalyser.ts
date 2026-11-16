export async function attachSpeechAnalyser(audio: HTMLAudioElement): Promise<() => void> {
  const context = new AudioContext()
  let frame = 0
  let closed = false
  let source: MediaElementAudioSourceNode | undefined
  let analyser: AnalyserNode | undefined
  const reset = () => dispatchEvent(new CustomEvent('lingxi-voice-level', { detail: 0 }))
  const cleanup = () => {
    if (closed) return
    closed = true; cancelAnimationFrame(frame); source?.disconnect(); analyser?.disconnect(); reset()
    void context.close()
  }
  try {
    await context.resume()
    if (context.state !== 'running' || audio.paused) { cleanup(); return () => undefined }
    analyser = context.createAnalyser(); analyser.fftSize = 256
    source = context.createMediaElementSource(audio); source.connect(analyser); analyser.connect(context.destination)
    const samples = new Uint8Array(analyser.fftSize)
    let level = 0
    const tick = () => {
      if (closed || audio.paused || audio.ended || !analyser) { reset(); return }
      analyser.getByteTimeDomainData(samples)
      const rms = Math.sqrt(samples.reduce((sum, sample) => sum + ((sample - 128) / 128) ** 2, 0) / samples.length)
      level = level * .4 + Math.min(1, Math.max(0, (rms - .008) * 9)) * .6
      dispatchEvent(new CustomEvent('lingxi-voice-level', { detail: level }))
      frame = requestAnimationFrame(tick)
    }
    tick()
    return cleanup
  } catch {
    if (source) { source.disconnect(); source.connect(context.destination) }
    else cleanup()
    return source ? () => { source?.disconnect(); cleanup() } : () => undefined
  }
}
