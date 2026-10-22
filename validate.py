#!/usr/bin/env python3
"""validate.py lesson FILE [--acs caacs.txt]   |   validate.py area FILE
Checks structure (same rules as tests/test_ground_lessons.py), citations, style, ACS element coverage.
Prints problems; last line is 'OK' or 'N PROBLEMS'."""
import json,os,re,sys
HERE=os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0,HERE)
import faa
REPO=os.environ.get('OPSHUB','/home/claude/opshub')
CITE=re.compile(r"\[\[(\d+):(\d+)\|([^\]]+)\]\]")
TYPES=("match","sort","order","tf","flash","label","calc","decide")
probs=[]
def bad(m): probs.append(m)
_pc={}
def pg(r,p):
    if r not in _pc: _pc[r]=faa.pages(r)
    return _pc[r][p-1] if 1<=p<=len(_pc[r]) else None
def strings(o):
    if isinstance(o,str): yield o
    elif isinstance(o,dict):
        for v in o.values(): yield from strings(v)
    elif isinstance(o,list):
        for v in o: yield from strings(v)
def cites(L):
    seen=set()
    for s in strings(L):
        for r,p,l in CITE.findall(s):
            if (r,p,l) in seen: continue
            seen.add((r,p,l))
            if r not in faa.MAP: bad('cite to unmapped resource %s: [[%s:%s|%s]]'%(r,r,p,l));continue
            t=pg(r,int(p))
            if t is None: bad('cite page out of range [[%s:%s|%s]]'%(r,p,l));continue
            toks=re.findall(r'\d+-\d+-\d+|\d+[-.]\d+',l)
            t2=t.replace('−','-')
            for back in (range(1,9) if r in ('33','34') else (1,2)):
                pp=pg(r,int(p)-back)
                if pp: t2+=pp.replace('\u2212','-')
            if toks and not any(re.sub(r'\(.*','',k) in t2 for k in toks):
                bad('citation label not on that page (check it): [[%s:%s|%s]]'%(r,p,l))
def style(L):
    for s in strings(L):
        if '—' in s or '–' in s: bad('em/en dash in text: '+s[:60]); break
    for s in strings(L):
        if re.search(r'TL;DR',s,re.I): bad('TL;DR used');break
        if re.search(r'(?i)seaplane|multiengine|\bAMEL\b|\bASES\b|\bAMES\b',s) and not re.search(r'ASEL',s):
            print('WARN: seaplane/multiengine mention (fine only as a passing remark): '+s[:70]);break
def lesson(path):
    L=json.load(open(path)); f=os.path.basename(path)
    if L.get('task_code')+'.json'!=f: bad('task_code does not match file name')
    for k in ('task_title','area','minutes','objective','why','prereading','warmup','key_terms','chunks','figures','scenario','skills','quiz','final_quiz','takeaways','cfi_notes','gaps','activities'):
        if k not in L: bad('missing '+k)
    if not L.get('chunks'): bad('no chunks');return
    n=len(L['chunks'])
    for i,c in enumerate(L['chunks'],1):
        for k in ('title','kind','codes','minutes','body'):
            if k not in c: bad('chunk %d missing %s'%(i,k))
        if c.get('kind') not in ('knowledge','risk','skills'): bad('chunk %d bad kind'%i)
        for b in c.get('body',[]):
            if 'figure' in b and b['figure'] not in L.get('figures',{}): bad('chunk %d unknown figure %s'%(i,b['figure']))
    for name in ('quiz','final_quiz'):
        for q in L.get(name,[]):
            if not (0<=q.get('answer',-1)<len(q.get('options',[]))): bad(name+' answer out of range: '+q.get('q','')[:50])
            if len(set(q.get('options',[])))!=len(q.get('options',[])): bad(name+' duplicate options: '+q.get('q','')[:50])
            if not q.get('why'): bad(name+' missing why')
    keys=set()
    for a in L.get('activities',[]):
        t=a.get('type')
        if t not in TYPES: bad('bad activity type %s'%t);continue
        k=a.get('key')
        if k in keys: bad('duplicate activity key %s'%k)
        keys.add(k)
        if a.get('after') is not None and not (1<=a['after']<=n): bad('activity %s after out of range'%k)
        if t=='match' and not all(p['l'] and p['r'] for p in a['pairs']): bad('match empty pair')
        if t=='sort' and not all(0<=i['b']<len(a['buckets']) for i in a['items']): bad('sort bucket index')
        if t=='label':
            if a.get('figure') not in L.get('figures',{}): bad('label figure missing')
            if not all(0<=p['x']<=100 and 0<=p['y']<=100 for p in a['pins']): bad('label pin out of range')
        if t=='calc':
            for it in a['items']:
                if not all(x in it for x in ('q','answer','tol','why')): bad('calc item missing field: '+it.get('q','')[:40])
        if t=='decide':
            for s in a['steps']:
                if 'options' not in s: bad('decide step without options')
    for k,fg in L.get('figures',{}).items():
        if not os.path.exists(os.path.join(REPO,'static','lessons',fg['file'])): bad('figure file missing: '+fg['file'])
        for x in ('res','page','caption','alt','source'):
            if x not in fg: bad('figure %s missing %s'%(k,x))
    # ACS element coverage
    code=L['task_code']
    acs=open(os.path.join(HERE,'caacs.txt'),errors='ignore').read() if code.startswith('CA.') else ''
    if code.startswith('RF.'):
        c=json.load(open(os.path.join(REPO,'ground_lessons','courses','RF.json')))
        a,t=code.split('.')[1:]
        task=[x for ar in c['areas'] if ar['code']==a for x in ar['tasks'] if x['code']==t]
        if task:
            els=set(e['code'] for k in ('knowledge','risk_management','skills') for e in task[0].get(k,[]))
            used=set(c2 for ch in L['chunks'] for c2 in ch.get('codes',[]))|set(s.get('code') for s in L.get('skills',[]))|set(L.get('scenario',{}).get('codes',[]))
            for e in sorted(els-used): bad('outline element not covered: '+e)
    if acs:
        els=set(re.findall(r'\b(%s\.[KRS]\d+[a-z]?)\b'%re.escape(code),acs))
        used=set(c for ch in L['chunks'] for c in ch.get('codes',[]))|set(s.get('code') for s in L.get('skills',[]))|set(L.get('scenario',{}).get('codes',[]))
        for e in sorted(els-used): bad('ACS element not covered: '+e)
    cites(L); style(L)
def area(path):
    A=json.load(open(path))
    if not A.get('questions'): bad('no questions')
    for q in A.get('questions',[]):
        if not (0<=q.get('answer',-1)<len(q.get('options',[]))): bad('answer out of range: '+q.get('q','')[:50])
    cites(A); style(A)
if __name__=='__main__':
    kind,path=sys.argv[1],sys.argv[2]
    (lesson if kind=='lesson' else area)(path)
    for p in probs: print('PROBLEM:',p)
    print('OK' if not probs else '%d PROBLEMS'%len(probs))
