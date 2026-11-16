export function PageLoading() {
  return <div className="page-loading" role="status" aria-label="正在加载页面">
    <img src="./assets/lingxi-brand.svg" alt="" className="page-loading-logo" draggable={false} />
    <span className="sr-only">正在加载页面…</span>
  </div>
}
