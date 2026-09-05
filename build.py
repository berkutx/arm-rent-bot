from pathlib import Path
r=Path(__file__).parent;w=r/'web'
s=(w/'index.template.html').read_text(encoding='utf-8')
for marker,path in [('CSS',w/'style.css'),('SEED',r/'seed.json'),('CORE',w/'core.js'),('APP',w/'app.js')]:
 t=path.read_text(encoding='utf-8')
 if marker=='SEED':t=t.replace('</','<\\/')
 s=s.replace('/*__'+marker+'__*/',t)
(w/'index.html').write_text(s, encoding='utf-8')
print('Built',w/'index.html',len(s),'characters')
