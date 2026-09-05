"""Stream ten Telegram photos without decoding or disk copies."""
import asyncio,json,resource,sys
import httpx
import server
server.setup()
class PhotoStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        for _ in range(80):yield b'x'*65536
async def telegram(method,args):return {'file_path':'photos/full.jpg'}
original=httpx.AsyncClient
server.tg=telegram
server.httpx.AsyncClient=lambda **kw:original(transport=httpx.MockTransport(lambda request:httpx.Response(200,stream=PhotoStream())),**kw)
async def one(i):
    photo=server.store_telegram_photo([{'file_id':str(i),'file_unique_id':str(i),'width':2560,'height':1920}],42)
    response=await server.media(photo['url'].rsplit('/',1)[-1]);count=0
    async for chunk in response.body_iterator:
        assert len(chunk)<=65536;count+=len(chunk)
    assert count==80*65536
async def main():
    await asyncio.gather(*(one(i) for i in range(10)))
    assert 'PIL' not in sys.modules and not list(server.DATA.rglob('*.jpg'))
    print(json.dumps({'concurrent_photos':10,'streamed_mib':50,'disk_photo_bytes':0,'max_rss_mib':round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,1)}))
asyncio.run(main())
