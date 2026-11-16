// Theme and wallpaper remain owned by the original Lingxi DIY controller.
// The former DSH preference is intentionally left untouched for rollback.
export function applyTheme(){
 document.body.toggleAttribute('data-ds-dark-theme',document.documentElement.dataset.theme==='dark');
}
(window as any).LingxiWorkbenchTheme={apply:applyTheme};
