"""Offline image-memory smoke test. Run in a disposable 256 MiB container."""
import asyncio, io, json, resource
from pathlib import Path
from PIL import Image
from starlette.datastructures import UploadFile
import server
server.setup()
source=Image.new('RGB',(4000,3000),(50,100,150))
encoded=io.BytesIO();source.save(encoded,'JPEG');source.close()
raw=encoded.getvalue();encoded.close()
async def main():
    for _ in range(10):
        result=await server.upload_photo(UploadFile(io.BytesIO(raw),filename='test.jpg'),{'id':42})
        with Image.open(server.DATA/'photos'/Path(result['url']).name) as image:
            assert max(image.size)<=1600
    print(json.dumps({'photos':10,'input_dimensions':[4000,3000],'max_rss_mib':round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,1)}))
asyncio.run(main())
