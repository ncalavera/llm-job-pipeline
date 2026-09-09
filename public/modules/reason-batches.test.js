import {test} from 'node:test';
import assert from 'node:assert/strict';
import {reasonBatch} from './reason-batches.js';
const role={llm_score:20,screening_state:'ready',posting_fingerprint:'p',screening_fingerprint:'p:f',screening:{
 posting_facts:{requirements:[{kind:'education',strength:'required',quote:'PhD in epidemiology'},{kind:'language',strength:'required',quote:'Fluent Japanese'}]},
 profile_comparison:[{requirement:0,finding:'possible_conflict',note:'Check degree'},{requirement:1,finding:'possible_conflict',note:'Check language'}]}};
test('one reason group per role; only quoted required conflicts with current preparation qualify',()=>{
 assert.equal(reasonBatch(role,'f').key,'eligibility');
 assert.equal(reasonBatch(role,'stale'),null);
 // A score no longer gates a batch: a quoted required condition the profile
 // may not meet groups a role whatever it scores, and the review screen's
 // default band starts at 40.
 assert.equal(reasonBatch({...role,llm_score:null},'f').key,'eligibility');
 assert.equal(reasonBatch({...role,llm_score:80},'f').key,'eligibility');
 const r=structuredClone(role);
 r.screening.profile_comparison[1].finding='unknown';
 assert.equal(reasonBatch(r,'f').key,'expertise');
 r.screening.posting_facts.requirements[0].strength='preferred';
 assert.equal(reasonBatch(r,'f'),null);
 r.screening.posting_facts.requirements[0].strength='required';
 r.screening.posting_facts.requirements[0].quote='';
 assert.equal(reasonBatch(r,'f'),null);
});
