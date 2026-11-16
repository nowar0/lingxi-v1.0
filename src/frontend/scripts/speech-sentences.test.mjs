import test from 'node:test'
import assert from 'node:assert/strict'
import {sentenceParts} from '../src/lib/speechSentences.ts'
test('Chinese and English sentences become separate audio requests',()=>{assert.deepEqual(sentenceParts('你好。今天好吗？Hello. Fine!'),['你好。','今天好吗？','Hello.','Fine!'])})
test('Markdown code and image URLs are not read aloud',()=>{const text=sentenceParts('看 [文档](https://example.invalid)。\n```sh\nrm secret\n```\n![图片](file:///secret.png)').join('');assert(!text.includes('https:'));assert(!text.includes('file:'));assert(!text.includes('rm secret'));assert(text.includes('文档'));assert(text.includes('代码内容已略过'))})
test('Long text is bounded without splitting surrogate pairs',()=>{const text='甲😀乙'.repeat(200),parts=sentenceParts(text);assert(parts.length>1);assert.equal(parts.join(''),text);assert(parts.every(part=>part.length<=161&&!/[\uD800-\uDBFF]$/.test(part)))})
test('Blank messages produce no synthesis request and decimal numbers stay intact',()=>{assert.deepEqual(sentenceParts(' \n '),[]);assert.deepEqual(sentenceParts('Version 1.2 is ready.'),['Version 1.2 is ready.'])})
