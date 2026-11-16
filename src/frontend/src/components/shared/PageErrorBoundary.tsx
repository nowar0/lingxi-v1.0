import {Component,type ReactNode} from 'react'
import {Button} from '@/components/ui/button'
export class PageErrorBoundary extends Component<{children:ReactNode;resetKey?:string},{failed:boolean}>{
 state={failed:false}
 static getDerivedStateFromError(){return {failed:true}}
 componentDidUpdate(previous:Readonly<{children:ReactNode;resetKey?:string}>){if(previous.resetKey!==this.props.resetKey&&this.state.failed)this.setState({failed:false})}
 render(){return this.state.failed?<div role="alert" className="page-error-state"><h2>此页面暂时无法显示</h2><p>已保存内容保留，可以重新打开此页面。</p><Button variant="outline" onClick={()=>this.setState({failed:false})}>重新打开</Button><Button variant="ghost" onClick={()=>window.location.reload()}>重新加载界面</Button></div>:this.props.children}
}
