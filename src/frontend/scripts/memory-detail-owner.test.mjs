import {test} from 'node:test'
import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import ts from 'typescript'

const source=readFileSync(new URL('../src/components/memory/MemoryV2Panel.tsx',import.meta.url),'utf8')
const ast=ts.createSourceFile('MemoryV2Panel.tsx',source,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX)
const functions={}
function visit(node){if(ts.isVariableDeclaration(node)&&ts.isIdentifier(node.name)&&['selectItem','closeDetail','changeAdvancedDetail'].includes(node.name.text))functions[node.name.text]=node.initializer.getText(ast);ts.forEachChild(node,visit)}
visit(ast)
const code=ts.transpileModule(Object.entries(functions).map(([name,body])=>`export const ${name}=${body}`).join('\n'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText
function fixture(){
 const state={selected:undefined,starOpen:false,advancedOpen:true},detailRequest={current:0},historyOwner={current:0},detailView={current:'list'}
 let complete
 const setters=Object.fromEntries([...source.matchAll(/\b(set[A-Z]\w*)\(/g)].map(match=>[match[1],()=>{}]))
 Object.assign(setters,{setSelected:value=>state.selected=value,setStarDetailOpen:value=>state.starOpen=value,setAdvancedDetailOpen:value=>state.advancedOpen=value})
 const injected={...setters,detailRequest,historyOwner,detailView,view:'list',alive:{current:true},memoryV2:{get:()=>new Promise(resolve=>complete=resolve)},message:error=>String(error)}
 const module={exports:{}};new Function('module','exports',...Object.keys(injected),code)(module,module.exports,...Object.values(injected))
 return {...module.exports,state,detailView,complete:()=>complete({id:'memory-a',key:'late response'})}
}
for(const close of ['closeDetail','changeAdvancedDetail'])test(close+' rejects a detail result that arrives after closing',async()=>{
 const current=fixture(),pending=current.selectItem('memory-a');current[close](false);current.complete()
 assert.equal(await pending,false);assert.equal(current.state.selected,undefined)
})
test('switching memory view rejects a result captured in the previous view',async()=>{
 const current=fixture(),pending=current.selectItem('memory-a');current.detailView.current='star';current.complete()
 assert.equal(await pending,false);assert.equal(current.state.selected,undefined)
})
