import {test} from 'node:test';
import assert from 'node:assert/strict';
import {compactSnapshot} from './payload.js';

test('compact payload preserves every row and filter fact; defers only long text', () => {
  const role={id:'v',status:'unseen',llm_summary:'Summary',full_description:'Long posting',llm_reasoning:'Reasoning',screening:{posting_facts:{location:'London'}}};
  const original={groups:[role],companies:[{company_id:'c',name:'Company',description:'Long profile'}],archived_groups:[{...role,id:'old'}]};
  const lite=compactSnapshot(original);
  assert.deepEqual(lite.groups.map(r=>r.id),['v']);
  assert.deepEqual(lite.groups[0].screening,role.screening);
  assert.equal(lite.groups[0].llm_summary,'Summary');
  assert.equal(lite.groups[0].full_description,undefined);
  assert.equal(role.full_description,'Long posting');
  assert.equal(lite.companies[0].name,'Company');
  assert.equal(lite.archived_groups[0].id,'old');
});

test('detail requests coalesce and never overwrite live status', async () => {
 globalThis.window={VACANCY_DATA:{config:{},groups:[],companies:[],vacancy_ids:[],stats:{},triage_reviews:[]}};
 globalThis.location={protocol:'https:',origin:'https://test.invalid'};
 const {hydrateDetail}=await import('./api.js');
 const prior=globalThis.fetch; let calls=0;
 globalThis.fetch=async()=>{calls++;return {ok:true,json:async()=>({full_description:'Full text',llm_reasoning:'Why',status:'passed'})}};
 try {
  const record={id:'v',status:'applied',_detailKind:'vacancy'};
  await Promise.all([hydrateDetail(record),hydrateDetail(record)]);
  assert.equal(calls,1); assert.equal(record.status,'applied');
  assert.equal(record.full_description,'Full text'); assert.equal(record._detailKind,undefined);
  await hydrateDetail(record); assert.equal(calls,1);
 } finally {globalThis.fetch=prior;}
});
