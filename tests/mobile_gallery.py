"""PhotoSwipe and agent submission against isolated FastAPI routes; no Telegram calls."""
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image, ImageDraw
import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server as s
from test_commission_photos import mock_telegram_files
from test_server import agent_profile, body, signed

OUT = ROOT / 'test-results'
OUT.mkdir(exist_ok=True)
errors = []
with tempfile.TemporaryDirectory(prefix='rent-gallery-', ignore_cleanup_errors=True) as tmp:
    s.DATA = Path(tmp)
    s.DB = s.DATA / 'test.sqlite3'
    s.LIVE = True
    s.TOKEN = 'test-token'
    s.BOT = 'test_bot'
    s.PUBLIC_URL = 'https://rent.test'
    s.ADMINS = {99}
    s.CHAT = s.PAID_CHAT = ''
    s.setup()
    client = TestClient(s.app)
    photos = [];contents={}
    patch=pytest.MonkeyPatch()
    for i, color in enumerate(['#365e79', '#658c73', '#c49a64', '#867795']):
        im = Image.new('RGB', (900, 1600) if i == 1 else (1600, 1000), color)
        draw = ImageDraw.Draw(im)
        draw.rectangle((100, 150, 700, 700), outline='white', width=6)
        draw.text((150, 250), f'TEST PHOTO {i+1}', fill='white', font_size=65)
        raw = io.BytesIO()
        im.save(raw, format='JPEG')
        full_size=im.size;im.thumbnail((800,800));preview=io.BytesIO();im.save(preview,format='JPEG')
        contents[f'full{i}']=raw.getvalue();contents[f'preview{i}']=preview.getvalue()
        variants=[{'file_id':name,'file_unique_id':name,'width':size[0],'height':size[1]} for name,size in [(f'full{i}',full_size),(f'preview{i}',im.size)]]
        photos.append(s.store_telegram_photo(variants,42))
    mock_telegram_files(patch,contents)
    agent_profile(client)
    listings = []
    for i in range(6):
        request = body(address=f'Тестовая {i+10}')
        request['listing']['photos'] = photos
        request['listing']['district'] = 'Кентрон' if i%2==0 else 'Арабкир'
        if i >= 4:
            request['listing'].update(role='agent', commission=80000, commission_type='fixed')
        response = client.post('/api/listings', headers=signed(), json=request)
        assert response.status_code == 200, response.text
        listings.append(response.json())
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=os.getenv('CHROMIUM_PATH') or None, args=['--no-sandbox'])
        page = browser.new_page(viewport={'width':390, 'height':844}, is_mobile=True, has_touch=True)
        page.on('pageerror', lambda error: errors.append(str(error)))
        init = {'initData':signed()['X-Telegram-Init-Data'], 'initDataUnsafe':{}, 'colorScheme':'light', 'themeParams':{}}
        page.add_init_script('window.Telegram={WebApp:'+json.dumps(init)+'};Object.assign(window.Telegram.WebApp,{ready(){},expand(){},onEvent(){},disableVerticalSwipes(){},enableVerticalSwipes(){},BackButton:{show(){window.backVisible=true},hide(){window.backVisible=false},onClick(fn){window.telegramBack=fn}}});')

        def route(req):
            url = urlparse(req.request.url)
            if url.hostname != 'rent.test':
                return req.fulfill(content_type='application/javascript', body='')
            path = url.path + ('?' + url.query if url.query else '')
            headers = {k:v for k,v in req.request.headers.items() if k in ('x-telegram-init-data', 'content-type')}
            response = client.request(req.request.method, path, headers=headers, content=req.request.post_data_buffer)
            req.fulfill(status=response.status_code, content_type=response.headers.get('content-type', 'application/json'), body=response.content)

        page.route('**/*', route)
        page.goto('https://rent.test/')
        expect(page.locator('.listing')).to_have_count(4)
        expect(page.locator('#dock')).to_be_hidden()
        assert not page.locator('#header [data-id=alerts],#main [data-action=follow]').count()
        def districts(total, each):
            page.locator('[data-action=district]').click()
            if page.locator('#filter-city').input_value()!='Ереван':
                page.locator('#filter-city').select_option('Ереван')
                page.wait_for_function('!sheetKind')
                page.locator('[data-action=district]').click()
            expect(page.locator('[data-action=select-district]')).to_have_count(13)
            assert not page.locator('.sheet details,.sheet .note').count()
            expect(page.locator('[data-action=select-district][data-id=""] .district-count')).to_have_text(str(total))
            for name in ['Кентрон','Арабкир']:
                expect(page.locator('[data-action=select-district][data-id="'+name+'"] .district-count')).to_have_text(str(each))
            page.locator('.sheet').evaluate('(x)=>Promise.all(x.getAnimations().map(a=>a.finished))')
            for width in [320,360,390,430]:
                page.set_viewport_size({'width':width,'height':844})
                assert page.evaluate('document.documentElement.scrollWidth')==width
                for box in page.locator('[data-action=select-district]').all():
                    rect=box.bounding_box()
                    if not (rect and rect['y']>=0 and rect['y']+rect['height']<=844):
                        page.screenshot(path=str(OUT/f'district-overflow-{total}-{width}.png'),animations='disabled')
                    assert rect and rect['y']>=0 and rect['y']+rect['height']<=844,(width,box.inner_text(),rect,page.locator('.sheet').evaluate('(x)=>x.getAnimations().map(a=>({playState:a.playState,currentTime:a.currentTime}))'))
            page.set_viewport_size({'width':390,'height':844})
            page.screenshot(path=str(OUT/'all-districts-390.png'),animations='disabled')
            page.locator('[data-action=select-district][data-id="Нубарашен"]').click()
            page.wait_for_function('!sheetKind')
            assert page.evaluate('state.filters.district')=='Нубарашен'
            expect(page.locator('.empty')).to_contain_text('Нет подходящих вариантов')
            page.locator('[data-action=reset-filters]').click()
        districts(4,2)

        assert page.locator('.listing .photo-tile').count() == 12
        for width in [320, 360, 390, 430]:
            page.set_viewport_size({'width':width, 'height':844})
            assert page.evaluate('document.documentElement.scrollWidth') == width
        page.set_viewport_size({'width':390, 'height':844})
        page.screenshot(path=str(OUT/'album-390.png'), animations='disabled')
        tile = page.locator('.listing').nth(1).locator('.photo-tile').nth(1)
        tile.scroll_into_view_if_needed()
        scroll = page.evaluate('scrollY')
        tile.click()
        page.wait_for_function('photoGallery?.opener.isOpen && photoGallery.currIndex===1 && photoGallery.currSlide.content.state==="loaded"')
        assert page.evaluate('backVisible')
        expect(page.locator('.gallery-thumbs [aria-pressed=true]')).to_have_attribute('data-gallery-index', '1')
        page.screenshot(path=str(OUT/'gallery-portrait-390.png'), animations='disabled')
        before = page.evaluate('photoGallery.currSlide.currZoomLevel')
        page.locator('.pswp__button--zoom').click()
        page.wait_for_timeout(400)
        assert page.evaluate('photoGallery.currSlide.currZoomLevel') > before
        page.locator('.gallery-thumbs [data-gallery-index="2"]').click()
        page.wait_for_function('photoGallery.currIndex===2')
        page.wait_for_timeout(350)
        cdp = page.context.new_cdp_session(page)
        def touch(kind, points):
            cdp.send('Input.dispatchTouchEvent', {'type':kind, 'touchPoints':[{'x':x,'y':y,'id':i,'radiusX':2,'radiusY':2,'force':1} for i,(x,y) in enumerate(points)]})
        touch('touchStart', [(330,350)])
        for x in range(310,40,-30):
            touch('touchMove', [(x,350)])
            page.wait_for_timeout(15)
        touch('touchEnd', [])
        page.wait_for_function('photoGallery.currIndex===3')
        page.wait_for_timeout(400)
        initial_zoom = page.evaluate('photoGallery.currSlide.currZoomLevel')
        touch('touchStart', [(150,330),(240,380)])
        for step in range(1,8):
            touch('touchMove', [(150-step*12,330-step*8),(240+step*12,380+step*8)])
            page.wait_for_timeout(25)
        touch('touchEnd', [])
        page.wait_for_timeout(400)
        assert page.evaluate('photoGallery.currSlide.currZoomLevel') > initial_zoom
        page.evaluate('telegramBack()')
        page.wait_for_function('!photoGallery && !galleryBackPending')
        assert abs(page.evaluate('scrollY') - scroll) < 3
        assert not page.evaluate('document.querySelector("#app").inert')
        tile.click()
        page.wait_for_function('photoGallery?.opener.isOpen && photoGallery.currIndex===1')
        page.go_back()
        page.wait_for_function('!photoGallery')
        tile.click()
        page.wait_for_function('photoGallery?.opener.isOpen')
        page.locator('[data-gallery-action=details]').click()
        page.locator('.detail-sheet').wait_for()
        page.locator('.detail-sheet .photo-tile').first.click()
        page.wait_for_function('photoGallery?.opener.isOpen')
        page.locator('.pswp__button--close').click()
        page.wait_for_function('!photoGallery && !galleryBackPending')
        assert page.locator('.detail-sheet').is_visible()
        assert page.evaluate('document.querySelector("#app").inert && !document.querySelector("#modal-root").inert')
        page.locator('.sheet [data-action=close]').click()
        page.wait_for_timeout(100)
        page.locator('[data-action=layout][data-id=grid]').click()
        assert page.locator('.compact-grid .photo-tile').count() == 4
        for width in [320, 360, 390, 430]:
            page.set_viewport_size({'width':width, 'height':844})
            assert page.evaluate('document.documentElement.scrollWidth') == width
            page.screenshot(path=str(OUT/f'grid-{width}.png'), animations='disabled')
        page.set_viewport_size({'width':390, 'height':844})
        page.reload()
        page.locator('.compact-grid .listing').first.wait_for()
        page.locator('[data-action=market][data-id=paid]').click()
        expect(page.locator('.listing')).to_have_count(2)
        districts(2,1)
        assert all('80' in text for text in page.locator('.commission-note').all_text_contents())
        page.evaluate('tg.colorScheme="dark";applyTheme()')
        for width in [320,390]:
            page.set_viewport_size({'width':width, 'height':844})
            assert page.evaluate('document.documentElement.scrollWidth') == width
            page.screenshot(path=str(OUT/f'paid-grid-dark-{width}.png'), animations='disabled')
        page.locator('#header [data-action=nav][data-id=add]').click()
        page.locator('[data-action=choose-kind][data-id=apartment]').click()
        page.locator('[data-action=choose-rooms][data-id="2"]').click()
        page.locator('#continue').click()
        page.locator('#address-input').fill('Подача 88')
        page.locator('#continue').click()
        page.locator('#budget-input').fill('300000')
        assert page.evaluate('state.draft.role') == 'agent'
        page.locator('[data-action=edit-commission]').click()
        page.locator('#commission-amount').fill('80000')
        page.locator('#commission-unit').select_option('AMD')
        page.locator('button[form=commission-form]').click()
        page.wait_for_function('!sheetKind')
        assert page.evaluate('state.draft.commission') == 80000
        page.locator('#continue').click()
        assert page.locator('#listing-text').input_value()==''
        page.locator('[data-action=edit-contact]').click()
        assert page.locator('#publisher-role option').count() == 1
        page.locator('button[form=contact-form]').click()
        page.wait_for_function('!sheetKind')
        page.locator('[data-action=publish]').click()
        page.wait_for_function('state.screen==="feed" && !state.busy')
        submitted = [x for x in client.get('/api/listings').json() if x['address']=='Подача 88'][0]
        assert submitted['description']=='' and submitted['phone']==''
        assert submitted['commission'] == 80000 and submitted['commission_type'] == 'fixed' and submitted['role'] == 'agent'
        assert page.evaluate('state.filters.market') == 'paid'
        assert not errors, errors
        print(json.dumps({'result':'passed', 'widths':[320,360,390,430], 'js_errors':errors, 'scope':'PhotoSwipe, touch swipe and pinch zoom, thumbnails, Telegram and browser Back, sheet return, scroll restoration, grid persistence, paid submission; mocked Telegram'}, ensure_ascii=False))
        browser.close()
