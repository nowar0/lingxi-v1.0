// Copy only the renderer's required local resources; never load from a CDN.
import {createRequire} from 'node:module'
import path from 'node:path'
import fs from 'node:fs'
import {fileURLToPath} from 'node:url'
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..')
const require=createRequire(import.meta.url)
const source=path.dirname(require.resolve('pdfjs-dist/package.json'))
const target=path.join(root,'public','pdfjs')
let count=0,size=0
for(const folder of ['cmaps','standard_fonts','wasm','iccs']) {
 const destination=path.join(target,folder);fs.mkdirSync(destination,{recursive:true})
 for(const file of fs.readdirSync(path.join(source,folder),{withFileTypes:true})) {
  if(!file.isFile())continue
  const from=path.join(source,folder,file.name),to=path.join(destination,file.name)
  fs.copyFileSync(from,to);count++;size+=fs.statSync(from).size
 }
}
fs.copyFileSync(path.join(source,'LICENSE'),path.join(target,'LICENSE'))
console.log(JSON.stringify({pdfjs:'6.4.299',files:count+1,bytes:size,external_resources:false}))
