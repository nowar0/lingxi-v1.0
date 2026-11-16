import {test} from 'node:test'
import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'
const exports={};const source=readFileSync(new URL('../src/lib/artifacts.ts',import.meta.url),'utf8')
vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,{exports,Set})
test('modern Office has deterministic MIME even when browser reports octet-stream',()=>{for(const [name,mime]of Object.entries({docx:'application/vnd.openxmlformats-officedocument.wordprocessingml.document',xlsx:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',pptx:'application/vnd.openxmlformats-officedocument.presentationml.presentation'}))assert.equal(exports.artifactMime({type:'application/octet-stream'},'fixture.'+name),mime)})
test('MP4 is video and WebM preserves explicitly reported video without breaking audio recorder',()=>{assert.equal(exports.artifactMime({type:''},'movie.mp4'),'video/mp4');assert.equal(exports.artifactMime({type:'video/webm;codecs=vp9'},'movie.webm'),'video/webm');assert.equal(exports.artifactMime({type:'audio/webm'},'recording.webm'),'audio/webm');assert.ok(exports.artifactAccept.includes('video/mp4'));assert.ok(exports.artifactAccept.includes('video/webm'))})
test('legacy Office does not masquerade as newly supported OOXML and references stay plain',()=>{assert.equal(exports.artifactMime({type:'application/octet-stream'},'legacy.xls'),'application/octet-stream');assert.equal(exports.artifactReference('a\nb','/v6/media/test'),'[a b](/v6/media/test)')})
