import { test } from 'node:test';
import assert from 'node:assert/strict';
import { acceptsSnapshot } from '../src/lib/snapshotOrder.ts';
test('same timeline late HTTP and SSE snapshots cannot rewind completed content',()=>{
 assert.equal(acceptsSnapshot(0,15,0,14),false);
 assert.equal(acceptsSnapshot(0,15,0,16),true);
 assert.equal(acceptsSnapshot(0,15,0,15),true);
});
test('timeline rewinds stay monotonic and legacy snapshots only precede revisioned data',()=>{
 assert.equal(acceptsSnapshot(2,20,1,21),false);
 assert.equal(acceptsSnapshot(1,20,2,21),true);
 assert.equal(acceptsSnapshot(-1,-1,0,undefined),true);
 assert.equal(acceptsSnapshot(0,5,0,undefined),false);
});
