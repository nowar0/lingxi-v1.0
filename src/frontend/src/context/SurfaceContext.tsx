import { createContext, useContext, type ComponentType, type CSSProperties } from 'react'
import { useOptionalTheme } from './ThemeContext'
const ProtectedSurface = createContext(false)
export function protectMessageUI<P extends object>(Component:ComponentType<P>) {
 return function MessageScope(props:P){return <ProtectedSurface.Provider value><Component {...props}/></ProtectedSurface.Provider>}
}
export function useSurface(){const protectedUI=useContext(ProtectedSurface);const messageStyle=useOptionalTheme()?.messageStyle;return {protectedUI,style:protectedUI?messageStyle:undefined,'data-ui-surface':protectedUI?'message':'chrome'} as {protectedUI:boolean;style:CSSProperties|undefined;'data-ui-surface':string}}
export function useMessageTokens(){return useOptionalTheme()?.messageStyle}
