import {build} from 'esbuild';
import {existsSync,readFileSync,writeFileSync,readdirSync} from 'node:fs';
import {createHash} from 'node:crypto';
import path from 'node:path';
const output=existsSync('../project/app')?'../project/app/static/ui/assets/workbench':'../app/static/ui/assets/workbench';
const result=await build({metafile:true,entryPoints:['src/main.tsx'],bundle:true,format:'esm',splitting:true,chunkNames:'chunks/[name]-[hash]',target:'chrome130',
  outdir:output,entryNames:'workbench',minify:true,sourcemap:false,
  jsx:'automatic',alias:{'@deepseek-ai/dsh-client-ui-primitives':'./src/primitives.tsx'},
  define:{'process.env.NODE_ENV':'"production"'},logLevel:'info'});
writeFileSync(output+'/../../workbench-tools.html',readFileSync('workbench-tools.html'));
const sha256=file=>createHash('sha256').update(readFileSync(file)).digest('hex');
function inputs(folder){return readdirSync(folder,{withFileTypes:true}).flatMap(item=>{const name=folder+'/'+item.name;return item.isDirectory()?inputs(name):[name]})}
const files=[...inputs('src'),...inputs('vendor'),'build.mjs','workbench-tools.html','package.json','package-lock.json'].sort();
const outputFiles=Object.keys(result.metafile.outputs).map(file=>path.relative(path.resolve(output,'../..'),path.resolve(file)).replaceAll('\\','/'));
const manifest={schema:'lingxi.workbench-build.v1',inputs:Object.fromEntries(files.map(file=>[file,sha256(file)])),outputs:Object.fromEntries(['workbench-tools.html',...outputFiles].map(file=>[file,sha256(path.join(output,'../..',file))]))};
writeFileSync(output+'/workbench-build.json',JSON.stringify(manifest,null,2)+'\n');
