"""Generate two Nginx configs for a public IPv4. Does not install or change the OS.
python deploy/render_ip_config.py REAL_PUBLIC_IPV4 --out deploy/generated
Obtain the certificate using Certbot 5.4+ webroot before installing the HTTPS config.
"""
import argparse,ipaddress
from pathlib import Path

def render(address:str,out:Path,port:int=18080):
    if not 1<=port<=65535:raise ValueError("Invalid upstream port")
    ip=ipaddress.ip_address(address)
    if ip.version!=4 or not ip.is_global:raise ValueError('Нужен настоящий глобальный IPv4, не локальный/примерный адрес')
    common='''    client_max_body_size 11m;
    location ^~ /.well-known/acme-challenge/ {
        root /var/www/acme;
        default_type text/plain;
        try_files $uri =404;
    }
'''
    bootstrap='''# Install only BEFORE obtaining the certificate. Check existing port/site conflicts.
server {
    listen 80;
    server_name IP_ADDRESS;
COMMON    location / { return 200 "Svoi ACME bootstrap ready\\n"; }
}
'''.replace('IP_ADDRESS',str(ip)).replace('COMMON',common)
    https='''# Install only AFTER a publicly trusted (not staging) IP certificate exists.
server {
    listen 80;
    server_name IP_ADDRESS;
COMMON    location / { return 301 https://IP_ADDRESS$request_uri; }
}
server {
    listen 443 ssl;
    server_name IP_ADDRESS;
    ssl_certificate /etc/letsencrypt/live/IP_ADDRESS/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/IP_ADDRESS/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    client_max_body_size 11m;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy no-referrer always;
    location / {
        proxy_pass http://127.0.0.1:UPSTREAM_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 60s;
    }
}
'''.replace('IP_ADDRESS',str(ip)).replace('COMMON',common).replace('UPSTREAM_PORT',str(port))
    out.mkdir(exist_ok=True,parents=True)
    for name,data in [('nginx-http.conf',bootstrap),('nginx-https.conf',https)]:
        p=out/name
        if p.exists():raise FileExistsError(f'Refusing to overwrite {p}')
        p.write_text(data,encoding='utf-8')
    print('Generated:',out,'— inspect before installing; no OS changes made.')

if __name__=='__main__':
    a=argparse.ArgumentParser();a.add_argument('ip');a.add_argument('--out',type=Path,default=Path(__file__).parent/'generated');a.add_argument('--upstream-port',type=int,default=18080);x=a.parse_args()
    try:render(x.ip,x.out,x.upstream_port)
    except (ValueError,FileExistsError) as e:raise SystemExit(str(e))
