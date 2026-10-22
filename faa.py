#!/usr/bin/env python3
"""Source lookup for lesson work.
  faa.py page RES PAGE        text of a resource page (PDF page number, 1-based)
  faa.py grep TERM [RES]      pages mentioning TERM (regex, case-insens.) in one/all resources
  faa.py grepCAACS TERM       lines of the Commercial ACS matching TERM
  faa.py ids                  resource id -> document"""
import json,os,re,sys
HERE=os.path.dirname(os.path.abspath(__file__))
MAP=json.load(open(os.path.join(HERE,'resmap.json')))
def pages(rid):
    m=MAP[str(rid)]
    d=m.get('dir',os.path.join(HERE,'faatext'))
    t=open(os.path.join(d,m['file']),errors='ignore').read().replace('−','-').replace('–','-')
    return t.split('\f')
def main():
    a=sys.argv[1:]
    if not a: print(__doc__);return
    if a[0]=='ids':
        for k,v in MAP.items(): print(k,v['name'],v['file'])
    elif a[0]=='page':
        p=pages(a[1]); n=int(a[2]); print(p[n-1] if 1<=n<=len(p) else 'NO SUCH PAGE (%d pages)'%len(p))
    elif a[0]=='grep':
        rx=re.compile(a[1],re.I)
        for rid in ([a[2]] if len(a)>2 else MAP):
            for i,p in enumerate(pages(rid),1):
                for ln in p.splitlines():
                    if rx.search(ln): print('%s:%d: %s'%(rid,i,ln.strip()[:160]));break
    elif a[0]=='grepCAACS':
        rx=re.compile(a[1],re.I)
        for ln in open(os.path.join(HERE,'caacs.txt'),errors='ignore'):
            if rx.search(ln): print(ln.rstrip())
if __name__=="__main__": main()
