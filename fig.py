#!/usr/bin/env python3
"""fig.py PAGEJPG X0 Y0 X1 Y1 OUTNAME   crop a rendered page (fractions 0-1 of width/height) to
/home/claude/opshub/static/lessons/OUTNAME (1030 px wide JPEG), e.g.
  fig.py /mnt/user-data/uploads/Claude/Projects/flight-academy-lessons/pages/24-194.jpg 0.1 0.62 0.92 0.85 CA.V.A-1.jpg
fig.py PAGEJPG show   -> prints size only."""
import sys
from PIL import Image
a=sys.argv[1:]
im=Image.open(a[0]).convert('RGB')
if a[1]=='show': print(im.size); sys.exit()
x0,y0,x1,y1=[float(v) for v in a[1:5]]
W,H=im.size
c=im.crop((int(x0*W),int(y0*H),int(x1*W),int(y1*H)))
nw=1030; c=c.resize((nw,max(1,round(c.height*nw/c.width))),Image.LANCZOS)
out=a[5] if a[5].startswith('/') else '/home/claude/opshub/static/lessons/'+a[5]
c.save(out,quality=85,optimize=True); print(out,c.size)
