from pathlib import Path
r=Path(__file__).parent;w=r/'web'
s=(w/'index.template.html').read_text(encoding='utf-8')
for marker,path in [('GALLERY_CSS',w/'vendor/photoswipe.css'),('GALLERY',w/'vendor/photoswipe.umd.min.js'),('CSS',w/'style.css'),('SEED',r/'seed.json'),('CORE',w/'core.js'),('APP',w/'app.js')]:
 t=path.read_text(encoding='utf-8')
 if marker=='SEED':t=t.replace('</','<\\/')
 s=s.replace('/*__'+marker+'__*/',t)
s='\n'.join(line.rstrip() for line in s.splitlines())+'\n'
(w/'index.html').write_text(s,encoding='utf-8',newline='\n')
print('Built',w/'index.html',len(s),'characters')
