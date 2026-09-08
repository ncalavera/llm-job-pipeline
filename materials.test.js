import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, mkdir, writeFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {handleMaterials} from './server.js';
test('private catalogue downloads only known immutable objects and disables caching',async()=>{
 const dir=await mkdtemp(join(tmpdir(),'materials-'));const previous=process.env.JOBSEARCH_PRIVATE_DIR;
 process.env.JOBSEARCH_PRIVATE_DIR=dir;
 const response=()=>({headers:{},setHeader(k,v){this.headers[k]=v;},writeHead(status,h){this.status=status;Object.assign(this.headers,h);},end(body){this.body=body;}});
 try{
  await mkdir(join(dir,'materials','objects'),{recursive:true});
  const hash='a'.repeat(64);
  await writeFile(join(dir,'materials','index.json'),JSON.stringify([{id:'one',sha256:hash,filename:'cv.txt'}]));
  await writeFile(join(dir,'materials','objects',hash),'original');
  const res=response();await handleMaterials({method:'GET',url:'/api/materials?id=one'},res);
  assert.equal(res.status,200);assert.equal(res.body.toString(),'original');assert.equal(res.headers['Cache-Control'],'no-store');assert.equal(res.headers['Access-Control-Allow-Origin'],undefined);
  const invalid=response();await handleMaterials({method:'GET',url:'/api/materials?id=../../secret'},invalid);assert.equal(invalid.status,404);
  const post=response();await handleMaterials({method:'POST',url:'/api/materials'},post);assert.equal(post.status,405);
 }finally{if(previous===undefined)delete process.env.JOBSEARCH_PRIVATE_DIR;else process.env.JOBSEARCH_PRIVATE_DIR=previous;await rm(dir,{recursive:true,force:true});}
});
