/** Timeline edits and streaming changes are separate monotonic dimensions. */
export function acceptsSnapshot(currentVersion: number, currentEvent: number, incomingVersion: number, incomingEvent?: number) {
  if (incomingVersion < currentVersion) return false
  if (currentEvent >= 0 && (incomingEvent === undefined || incomingEvent < currentEvent)) return false
  return true
}
