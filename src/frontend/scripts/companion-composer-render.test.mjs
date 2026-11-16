import {test} from 'node:test';import assert from 'node:assert/strict';import {readFileSync} from 'node:fs';
import path from 'node:path';import {fileURLToPath} from 'node:url';import {createRequire} from 'node:module';
import {build} from 'esbuild';import React from 'react';import {renderToStaticMarkup} from 'react-dom/server';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const hidden=new Set(['ComposerLimit','ConversationQuestionCard','ComposerMenu','ComposerAttachments','ImageGenerationDialog','QuotePreview','DraftRecovery']);
const result=await build({stdin:{contents:"export {CompanionComposer} from './src/components/shared/CompanionComposer';export {TooltipProvider} from './src/components/ui/tooltip'",loader:'tsx',resolveDir:root},tsconfig:path.join(root,'tsconfig.json'),bundle:true,platform:'node',format:'cjs',write:false,loader:{'.css':'empty'},external:['react','react-dom','react/jsx-runtime','react/jsx-dev-runtime'],logLevel:'silent',plugins:[{name:'isolated-composer-state',setup(build){
 if(process.env.LINGXI_COMPOSER_RENDER_BASELINE)build.onLoad({filter:/[\\/]CompanionComposer\.tsx$/},()=>({contents:readFileSync(process.env.LINGXI_COMPOSER_RENDER_BASELINE,'utf8'),loader:'tsx'}));
 build.onResolve({filter:/(AppContext|MessageActionsContext|useComposerMedia|ComposerLimit|ConversationQuestionCard|ComposerMenu|ComposerAttachments|ImageGenerationDialog|QuotePreview|DraftRecovery)$/},args=>({path:args.path,namespace:'state-fixture'}));
 build.onLoad({filter:/.*/,namespace:'state-fixture'},args=>{
  const name=args.path.split('/').at(-1);let contents;
  if(name==='AppContext')contents='export const useApp=()=>globalThis.__lingxiComposerFixture.app';
  else if(name==='useComposerMedia')contents='export const useComposerMedia=()=>globalThis.__lingxiComposerFixture.media';
  else if(name==='MessageActionsContext')contents='export const useMessageActions=()=>({currentQuote:()=>undefined,clearQuote:()=>{}})';
  else if(hidden.has(name))contents='export const ComposerLimit=()=>null;export const ConversationQuestions=()=>null;export const ComposerMenu=()=>null;export const composerSuggestionKey=()=>false;export const ComposerAttachments=()=>null;export const ImageGenerationDialog=()=>null;export const QuotePreview=()=>null;export const DraftRecovery=()=>null;';
  else throw Error('Unexpected fixture '+name);return {contents,loader:'js'};
 });
}}]});
const module={exports:{}};new Function('require','module','exports',result.outputFiles[0].text)(createRequire(import.meta.url),module,module.exports);
function render({draft='',generating=false,attachments=0,connected=true,recording=false}={}){
 globalThis.__lingxiComposerFixture={app:{sendMessage:async()=>{},stopGenerating:async()=>{},generating:{companion:generating},sending:{companion:false},connection:connected?'connected':'offline',activeCharacter:{name:'零汐'},drafts:{companion:draft},setDraft:()=>{},messages:[]},media:{items:Array.from({length:attachments},()=>({status:'ready'})),busy:false,recording}};
 try{return renderToStaticMarkup(React.createElement(module.exports.TooltipProvider,null,React.createElement(module.exports.CompanionComposer)))}finally{delete globalThis.__lingxiComposerFixture}
}
test('message composer mounts with its real Radix tooltip triggers when idle',()=>{const markup=render();assert.ok(markup.includes('aria-label="消息输入"'));assert.ok(markup.includes('aria-label="发送消息"'));assert.ok(!markup.includes('aria-label="停止当前回复"'))});
test('generating without a draft offers one stop control',()=>{const markup=render({generating:true});assert.ok(markup.includes('aria-label="停止生成"'));assert.ok(!markup.includes('aria-label="停止当前回复"'))});
test('generating with a draft keeps separate stop and send buttons',()=>{const markup=render({generating:true,draft:'下一条'});assert.ok(markup.includes('aria-label="停止当前回复"'));assert.ok(markup.includes('aria-label="发送消息"'))});
test('generating with attachments keeps separate stop and send buttons',()=>{const markup=render({generating:true,attachments:1});assert.ok(markup.includes('aria-label="停止当前回复"'));assert.ok(markup.includes('aria-label="发送消息"'))});
test('offline composer and recording state render without losing their controls',()=>{assert.ok(render({connected:false}).includes('服务未连接'));assert.ok(render({recording:true}).includes('aria-label="停止录音"'))});
