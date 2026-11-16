import { test } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import ts from 'typescript'
const source=fs.readFileSync(new URL('../src/lib/modelCatalog.ts',import.meta.url),'utf8')
const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022}}).outputText
const {matchesCatalogModel}=await import('data:text/javascript;base64,'+Buffer.from(compiled).toString('base64'))
test('search finds a Chinese custom display name as well as the technical ID',()=>{
 const model={id:'grok-imagine-image',display_name:'零汐图片生成'}
 assert.equal(matchesCatalogModel(model,'零汐图片'),true)
 assert.equal(matchesCatalogModel(model,'GROK-IMAGINE'),true)
 assert.equal(matchesCatalogModel(model,'  图片生成  '),true)
 assert.equal(matchesCatalogModel(model,'文字模型'),false)
})
test('unnamed models and a blank query remain usable',()=>{
 assert.equal(matchesCatalogModel({id:'model-id'},'model'),true)
 assert.equal(matchesCatalogModel({id:'model-id'},'   '),true)
 assert.equal(matchesCatalogModel({id:'model-id'},'no-match'),false)
})
