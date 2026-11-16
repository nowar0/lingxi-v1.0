import {test} from 'node:test';import assert from 'node:assert/strict';import {readFileSync,readdirSync} from 'node:fs';import path from 'node:path';import {fileURLToPath} from 'node:url';import ts from 'typescript';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../src');
function files(folder){return readdirSync(folder,{withFileTypes:true}).flatMap(entry=>entry.isDirectory()?files(path.join(folder,entry.name)):entry.name.endsWith('.tsx')?[path.join(folder,entry.name)]:[])}
test('slotted UI triggers and Tooltip call sites supply exactly one element child',()=>{
 const invalid=[];
 for(const file of files(root)){
  const source=ts.createSourceFile(file,readFileSync(file,'utf8'),ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
  function visit(node){if(ts.isJsxElement(node)){
   const open=node.openingElement;const tooltip=open.tagName.getText(source)==='Tooltip';const slotted=open.attributes.properties.some(attr=>ts.isJsxAttribute(attr)&&attr.name.getText(source)==='asChild'&&(!attr.initializer||attr.initializer.getText(source)==='{true}'));
   if(tooltip||slotted){const children=node.children.filter(child=>!ts.isJsxText(child)||!!child.text.trim()).filter(child=>!ts.isJsxExpression(child)||!!child.expression);if(children.length!==1||ts.isJsxFragment(children[0]))invalid.push(path.relative(root,file)+':'+(source.getLineAndCharacterOfPosition(node.pos).line+1))}
  }ts.forEachChild(node,visit)}visit(source)
 }
 assert.deepEqual(invalid,[],'Each trigger needs its own single element, never a button group or fragment')
});
