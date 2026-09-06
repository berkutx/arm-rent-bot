import argparse,collections,json,time
import httpx


def main():
    parser=argparse.ArgumentParser(description='Разово дополнить опубликованные объявления Еревана координатами.')
    parser.add_argument('--url',default='http://127.0.0.1:8000');args=parser.parse_args()
    counts=collections.Counter();failed=[]
    with httpx.Client(base_url=args.url.rstrip('/'),timeout=30) as client:
        response=client.get('/api/listings');response.raise_for_status()
        listings=[l for l in response.json() if l.get('city')=='Ереван' and l.get('status')=='active']
        for i,listing in enumerate(listings,1):
            response=client.get('/api/listings/'+listing['id']+'/map')
            if response.status_code==429:
                time.sleep(2);response=client.get('/api/listings/'+listing['id']+'/map')
            if response.is_success:
                points=response.json()['points'];counts[points[0]['precision'] if points else 'not_found']+=1
            else:
                failed.append({'id':listing['id'],'status':response.status_code})
                if response.status_code in (429,503):break
            if i%5==0:print(json.dumps({'processed':i,'total':len(listings),'locations':dict(counts)},ensure_ascii=False),flush=True)
            time.sleep(1.2)
    print(json.dumps({'processed':sum(counts.values()),'total':len(listings),'locations':dict(counts),'failed':failed},ensure_ascii=False),flush=True)
    if failed:raise SystemExit(1)


if __name__=='__main__':main()
