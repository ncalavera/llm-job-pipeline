const labels = {cv:'CV',cover_letter:'Cover letter',answers:'Answers',test:'Test response',evidence:'Career evidence',notes:'Notes & correspondence',sent:'Sent',draft:'Draft',unknown:'Unconfirmed'};
const el = (tag, text, cls) => {const node=document.createElement(tag);if(text)node.textContent=text;if(cls)node.className=cls;return node;};
let rows=[];
const vacancyId=new URLSearchParams(location.search).get('vacancy');
function render(){
 const query=document.getElementById('search').value.toLowerCase().split(/\s+/).filter(Boolean);
 const status=document.getElementById('status').value, kind=document.getElementById('kind').value;
 const selected=rows.filter(r=>(!vacancyId||r.vacancy_id===vacancyId)&&(!status||(!['notes','evidence'].includes(r.kind)&&r.status===status))&&(!kind||r.kind===kind)&&query.every(w=>[r.organisation,r.filename,r.text].join(' ').toLowerCase().includes(w)));
 document.getElementById('count').textContent=`${selected.length} material versions shown`;
 const host=document.getElementById('list');host.replaceChildren();
 const grouped=selected.reduce((m,r)=>m.set(r.organisation,[...(m.get(r.organisation)||[]),r]),new Map());
 for(const [org,items] of [...grouped].sort(([a],[b])=>a.localeCompare(b))){
  const section=el('details',null,'group');section.open=Boolean(query.length||vacancyId||status||kind);section.append(el('summary',org.replaceAll('-', ' ')+' · '+items.length+' versions'));
  if(items[0].vacancy_id){const link=el('a','Open application');link.href='/?vacancy='+encodeURIComponent(items[0].vacancy_id);section.append(link);}
  for(const row of items){
   const card=el('details',null,'card'), summary=el('summary',row.filename);
   summary.append(el('span',row.kind==='evidence'?'Source record':row.kind==='notes'?'Note':labels[row.status]||'Unconfirmed','badge'));if(row.date)summary.append(el('span',row.date,'badge'));
   summary.append(el('span',row.id.slice(0,6),'badge'));card.append(summary);
   card.append(el('div',`${labels[row.kind]||row.kind}${row.date?' · Source date: '+row.date:''}`,'meta'));
   if(row.evidence_note)card.append(el('p',row.evidence_note));
   if(row.evidence)card.append(el('p','Submission evidence: '+row.evidence));
   card.append(el('p','Source: '+row.source,'source'));
   if(row.text)card.append(el('pre',row.text));
   const download=el('a','Download original');download.href='/api/materials?id='+encodeURIComponent(row.id);card.append(download);
   if(row.text){const copy=el('button','Copy text');copy.style.marginLeft='16px';copy.onclick=async()=>{try{await navigator.clipboard.writeText(row.text);copy.textContent='Copied';}catch{copy.textContent='Select and copy the text above';}};card.append(copy);}
   section.append(card);
  }host.append(section);
 }
}
for(const id of ['search','status','kind'])document.getElementById(id).addEventListener('input',render);
try{const response=await fetch('/api/materials');if(!response.ok)throw new Error();rows=await response.json();render();}catch{document.getElementById('count').textContent='Materials could not be loaded. Reload to try again.';}
