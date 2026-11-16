import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { build } from 'esbuild';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const result=await build({stdin:{contents:"export {MessageContent,safeMessageUrl} from './src/components/shared/MessageContent'",resolveDir:root,loader:'tsx'},tsconfig:path.join(root,'tsconfig.json'),bundle:true,loader:{'.css':'empty'},external:['pdfjs-dist','pdfjs-dist/*','react','react-dom','react/jsx-runtime','react/jsx-dev-runtime'],platform:'node',format:'cjs',write:false,logLevel:'silent'});
const module={exports:{}};new Function('require','module','exports',result.outputFiles[0].text)(createRequire(import.meta.url),module,module.exports);
const {MessageContent,safeMessageUrl}=module.exports;
const render=text=>renderToStaticMarkup(React.createElement(MessageContent,{text}));
test('Markdown covers headings, nested lists, quotes, tables, language and unfinished fences',()=>{
 const html=render('# 标题\n\n- 第一项\n  - 嵌套\n\n> 引用\n\n| 项目 | 结果 |\n| --- | --- |\n| A | 通过 |\n\n```python\nprint("未闭合也显示")');
 for(const value of ['<h1>','<ul>','<blockquote>','<table>','python','复制代码','未闭合也显示'])assert.ok(html.includes(value),value);
});
test('raw HTML and dangerous URL forms never become executable DOM',()=>{
 const html=render('<script>window.bad=1</script>\n\n<iframe src="https://bad.invalid"></iframe>\n\n[x](javascript:alert%281%29)\n\n[y](data:text/html,bad)\n\n[safe](https://example.com/path)');
 assert.ok(!html.includes('<script')&&!html.includes('<iframe')&&!html.includes('href="javascript:')&&!html.includes('href="data:'));
 assert.ok(html.includes('href="https://example.com/path"'));
 for(const url of ['javascript:alert(1)','file:///C:/private','//tracker.invalid/a','data:image/svg+xml,bad','https://u:p@host.invalid/','java\nscript:bad'])assert.equal(safeMessageUrl(url),undefined,url);
});
test('local media links remain usable and external images do not auto-load',()=>{
 const url='/v6/media/'+ 'a'.repeat(32)+'?session_id=8';
 assert.ok(render('[生成图片]('+url+')').includes('<img src="'+url+'"'));
 const external=render('![远程图](https://example.com/a.png)');
 assert.ok(!external.includes('<img'));assert.ok(external.includes('href="https://example.com/a.png"'));
 const long=render('```txt\n'+Array.from({length:30},(_,i)=>'line '+i).join('\n')+'\n```');
 assert.ok(long.includes('展开代码'));assert.ok(long.includes('is-collapsed'));assert.ok(long.includes('line 29'));
});
