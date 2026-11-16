import { test } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'

test('Agent package requests remain human-preview then exact one-use execution', () => {
  const source = fs.readFileSync(new URL('../src/components/skills/PackageCallRequest.tsx', import.meta.url), 'utf8')
  assert.match(source, /awaiting_user_preview/)
  assert.match(source, /value\.authorized !== false/)
  assert.match(source, /live\.revision !== intent\.revision \|\| live\.digest !== intent\.digest/)
  assert.match(source, /calls\/preview/)
  assert.match(source, /client\.execute\(exact\)/)
  assert.match(source, /我批准本次执行/)
  assert.match(source, /setPreview\(undefined\); const value = await client\.execute/)
  assert.match(source, /结果未自动发送给模型/)
})
test('chat activity integrates request only for the specific intent tool', () => {
  const source = fs.readFileSync(new URL('../src/components/shared/ToolActivities.tsx', import.meta.url), 'utf8')
  assert.match(source, /activity\.tool==='request_package_call'/)
  assert.match(source, /<PackageCallRequest result=\{activity\.result\}/)
})
