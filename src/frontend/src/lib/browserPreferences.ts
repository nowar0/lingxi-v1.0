const names=['lingxi-next-theme-v1','lingxi-message-theme-v1','lingxi-effort-slider-v1','lingxi-pinned-companion','lingxi-pinned-work','lingxi-sidebar-layout-v1','lingxi-work-rail-open','lingxi-work-rail-width','lingxi-work-tool-width','lingxi-work-projects-collapsed','lingxi-companion-rail:v1']
export let preferenceRevision:string|null=null
export function collectBrowserPreferences(){const entries:Record<string,string>={};for(const key of names){const value=localStorage.getItem(key);if(value!==null&&value.length<30000)entries[key]=value}return {format:'lingxi-browser-preferences-v1',entries}}
export async function hydrateBrowserPreferences(){
 try{const response=await fetch('/v6/data/preferences',{signal:AbortSignal.timeout(5000)});if(!response.ok)return;const value=await response.json();preferenceRevision=value.revision
 const snapshot=value.preferences?.browser;if(snapshot?.format==='lingxi-browser-preferences-v1'&&snapshot.entries){for(const key of names){const entry=snapshot.entries[key];if(typeof entry==='string'&&entry.length<30000){JSON.parse(entry);localStorage.setItem(key,entry)}}}
 else if(value.preferences?.appearance?.format==='lingxi-theme'&&!localStorage.getItem('lingxi-next-theme-v1'))localStorage.setItem('lingxi-next-theme-v1',JSON.stringify({theme:value.preferences.appearance,mode:value.preferences.appearance.appearance.theme,glass:72,reduceMotion:false}))
 }catch{/* Existing local settings remain usable if the backend is unavailable. */}
}
export function setPreferenceRevision(value:string){preferenceRevision=value}
