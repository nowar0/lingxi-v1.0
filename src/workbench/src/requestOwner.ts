/** A replaced read can never publish into the next task, tab or refresh. */
export function createRequestOwner() {
  let current: AbortController | undefined;
  return {
    begin() {
      current?.abort();
      const owner = current = new AbortController();
      return {signal: owner.signal, isCurrent: () => current === owner && !owner.signal.aborted};
    },
    invalidate() { current?.abort(); current = undefined; },
  };
}
