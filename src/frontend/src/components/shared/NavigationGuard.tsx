import {useEffect,useState} from 'react'
import {useBlocker} from 'react-router-dom'
import {confirmDiscard,hasUnsavedChanges} from './UnsavedChanges'
export function NavigationGuard(){
 const [allow,setAllow]=useState(false)
 const blocker=useBlocker(({currentLocation,nextLocation})=>!allow&&hasUnsavedChanges()&&(currentLocation.pathname+currentLocation.search!==nextLocation.pathname+nextLocation.search))
 useEffect(()=>{if(blocker.state!=='blocked')return;let live=true;void confirmDiscard().then(ok=>{if(live){if(ok){setAllow(true);blocker.proceed()}else blocker.reset()}});return()=>{live=false}},[blocker])
 useEffect(()=>{if(blocker.state==='unblocked')setAllow(false)},[blocker.state])
 return null
}
