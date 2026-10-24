#!/usr/bin/env python3
"""dupes.py [PREFIX ...]  Find near-duplicate questions across lesson and area files.
Compares every question-like string (quiz, final_quiz, chunk checks, scenario q, game items, area questions)
across DIFFERENT files using word 3-gram Jaccard similarity. Prints pairs >= threshold (default 0.5)."""
import json,glob,os,re,sys,itertools
D='/home/claude/opshub/ground_lessons'
pre=[a for a in sys.argv[1:] if not a.startswith('-')] or ['CA.','RF.']
thr=float(next((a[2:] for a in sys.argv[1:] if a.startswith('-t')),0.5))
def items(path):
    o=json.load(open(path)); name=os.path.basename(path)[:-5]
    out=[]
    def add(where,t):
        if isinstance(t,str) and len(t)>30: out.append((name,where,t))
    for k in ('quiz','final_quiz'):
        for i,q in enumerate(o.get(k,[])): add(f'{k}{i+1}',q.get('q'))
    for i,c in enumerate(o.get('chunks',[])): add(f'check{i+1}',(c.get('check') or {}).get('q'))
    for i,q in enumerate((o.get('scenario') or {}).get('questions',[])): add(f'scen{i+1}',q.get('q'))
    for a in o.get('activities',[]):
        k=a.get('key')
        for i,c in enumerate(a.get('cards',[])): add(f'{k}.tf{i+1}',c.get('s'))
        for i,c in enumerate(a.get('items',[])): add(f'{k}.it{i+1}',c.get('q') or c.get('t'))
        for i,c in enumerate(a.get('steps',[])):
            if isinstance(c,dict): add(f'{k}.st{i+1}',c.get('s'))
    for i,q in enumerate(o.get('questions',[])): add(f'area_q{i+1}',q.get('q'))
    return out
def grams(t):
    w=re.findall(r'[a-z0-9]+',t.lower()); return set(zip(w,w[1:],w[2:]))
files=[f for p in pre for f in glob.glob(f'{D}/{p}*.json')+glob.glob(f'{D}/areas/{p}*.json')]
allq=[(n,w,t,grams(t)) for f in sorted(set(files)) for (n,w,t) in items(f)]
hits=[]
for a,b in itertools.combinations(allq,2):
    if a[0]==b[0] or not a[3] or not b[3]: continue
    j=len(a[3]&b[3])/len(a[3]|b[3])
    if j>=thr: hits.append((round(j,2),a,b))
for j,a,b in sorted(hits,reverse=True):
    print(f'{j}  {a[0]}:{a[1]}  <->  {b[0]}:{b[1]}\n   A: {a[2][:140]}\n   B: {b[2][:140]}')
print(len(hits),'pairs >=',thr,'among',len(allq),'items')
