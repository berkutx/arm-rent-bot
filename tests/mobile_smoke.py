"""Offline HTML browser checks. No real Telegram or official services are contacted.
python tests/mobile_smoke.py
Requires: pip install -r requirements-dev.txt; playwright install chromium
"""
import json,os,shutil
from pathlib import Path
from playwright.sync_api import sync_playwright
R=Path(__file__).resolve().parents[1];O=R/'test-results';O.mkdir(exist_ok=True)
with sync_playwright() as p:
 args={'headless':True,'args':['--no-sandbox']}
 if os.getenv('CHROMIUM_PATH') or shutil.which('chromium'):args['executable_path']=os.getenv('CHROMIUM_PATH') or shutil.which('chromium')
 b=p.chromium.launch(**args)
 page=b.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True);errors=[]
 page.on('pageerror',lambda e:errors.append(str(e)))
 # Container browser blocks localhost navigation. set_content tests the exact built HTML;
 # persistent browser storage is mocked because about:blank has no origin.
 page.evaluate("""() => { window.__testStorage={};Object.defineProperty(window,'localStorage',{configurable:true,value:{getItem:k=>window.__testStorage[k]||null,setItem:(k,v)=>{window.__testStorage[k]=String(v)},clear:()=>{window.__testStorage={}}}}); }""")
 page.set_content((R/'web/index.html').read_text(encoding='utf-8'));page.wait_for_timeout(200)
 assert page.locator('.listing').count()==24
 widths=[]
 for w in [320,360,390,430]:
  page.set_viewport_size({'width':w,'height':844})
  assert page.evaluate('document.documentElement.scrollWidth')==w
  page.screenshot(path=str(O/f'feed-{w}.png'));widths.append(w)
 page.set_viewport_size({'width':390,'height':844})
 page.locator('[data-action=district]').click()
 assert page.locator('[data-action=select-district]').count()==13
 assert {'Дилижан','Севан','Цахкадзор','Гюмри','Ванадзор','Джермук'} <= set(page.locator('#filter-city option').all_text_contents())
 page.locator('.sheet [data-action=close]').click();page.wait_for_timeout(100)
 page.locator('.listing-main').first.click();page.screenshot(path=str(O/'detail-390.png'))
 assert 'Собственник — со слов автора' in page.locator('.sheet').inner_text()
 page.locator('.sheet [data-action=close]').click();page.wait_for_timeout(100)
 page.locator('[data-action=follow]').click();assert page.locator('[data-action=follow]').get_attribute('aria-pressed')=='true'
 page.locator('[data-action=nav][data-id=add]').click();page.locator('[data-action=example]').click()
 source_description='С 15 числа сдается дом до мая месяца, стоимость 600.000 + ком услуги, дом находится в центре Еревана на улице Таирова, за подробностями в ЛС'
 page.locator('#listing-text').fill(source_description)
 page.evaluate("() => { C.parse=()=>{throw Error('Description parsing must not run')}; }")
 page.screenshot(path=str(O/'add-390.png'));page.locator('#continue').click()
 assert page.locator('.edit-row').count()==3
 draft=page.evaluate('state.draft')
 assert draft['address']=='' and draft['kind']=='' and draft['prices'][0]['amount']==0
 assert draft['available'] is None and draft['available_until'] is None
 page.locator('[data-action=edit-price]').click()
 page.locator('#budget-input').fill('600000')
 page.locator('button[form=edit-price-form]').click();page.wait_for_timeout(100)
 page.locator('[data-action=edit-rooms]').click()
 page.locator('[data-action=set-rooms][data-id=house]').click();page.wait_for_timeout(100)
 page.locator('[data-action=edit-address]').click()
 page.locator('#address-input').fill('Таирова')
 page.locator('#city-input').select_option('Дилижан')
 assert not page.locator('#district-row').is_visible()
 assert page.locator('#metro-minutes').is_disabled()
 page.locator('.travel-fields summary').click()
 page.locator('#center-minutes').fill('15')
 page.screenshot(path=str(O/'city-dilijan-390.png'))
 page.locator('button[form=address-form]').click();page.wait_for_timeout(100)
 assert page.evaluate('state.draft.city')=='Дилижан'
 assert page.evaluate('state.draft.center_drive_minutes')==15
 assert page.evaluate('state.draft.metro_walk_minutes') is None
 page.locator('[data-action=edit-address]').click()
 page.locator('#city-input').select_option('other')
 page.locator('#custom-city').fill('Иджеван')
 page.locator('button[form=address-form]').click();page.wait_for_timeout(100)
 assert page.evaluate('state.draft.city')=='Иджеван'
 page.locator('[data-action=edit-address]').click()
 page.locator('#city-input').select_option('Ереван')
 assert page.locator('#district-row').is_visible()
 page.locator('#district-input').select_option('Арабкир')
 page.locator('.travel-fields summary').click()
 page.locator('#metro-minutes').fill('10')
 page.locator('#center-minutes').fill('20')
 for w in [320,360,390,430]:
  page.set_viewport_size({'width':w,'height':844})
  assert page.evaluate('document.documentElement.scrollWidth')==w
 page.set_viewport_size({'width':390,'height':844})
 page.screenshot(path=str(O/'city-yerevan-390.png'))
 page.locator('button[form=address-form]').click();page.wait_for_timeout(100)
 assert page.evaluate('state.text')==source_description
 page.locator('#available-from').fill('2026-09-15')
 page.locator('#available-until').fill('2026-09-01')
 assert page.locator('#availability-error').is_visible()
 page.locator('#available-until').fill('2027-05-01')
 assert not page.locator('#availability-error').is_visible()
 page.locator('#available-until').fill('')
 assert page.evaluate('state.draft.available_until') is None
 page.locator('#available-until').fill('2027-05-01')
 for w in [320,360,390,430]:
  page.set_viewport_size({'width':w,'height':844})
  assert page.evaluate('document.documentElement.scrollWidth')==w
 page.set_viewport_size({'width':390,'height':844})
 page.screenshot(path=str(O/'manual-dates-390.png'))
 page.locator('[data-action=edit-text]').click()
 source_description+=' Описание остаётся как есть.'
 page.locator('#listing-text').fill(source_description)
 page.locator('#continue').click()
 assert page.evaluate('state.draft.prices[0].amount')==600000
 assert page.evaluate('state.draft.address')=='Таирова'
 assert page.evaluate('state.draft.available_until')=='2027-05-01'
 assert page.evaluate('state.text')==source_description
 assert page.evaluate('state.text')==source_description
 page.locator('.optional-block summary').first.click()
 page.locator('#opt-contract').select_option('yes');page.locator('#opt-registration').select_option('ask')
 page.locator('#wishes').fill('Тестовое пожелание: написать вечером.')
 page.locator('.optional-block summary').first.click()
 page.screenshot(path=str(O/'review-390.png'))
 page.locator('#publish').click();page.wait_for_timeout(100)
 assert 'Объявление добавлено' in page.locator('.sheet').inner_text()
 page.locator('[data-action=verify]').click();page.screenshot(path=str(O/'verification-390.png'))
 page.locator('#applicant-name').fill('Тестовый пользователь')
 page.locator('#document-number').fill('DEMO-NOT-A-REAL-DOCUMENT')
 page.locator('#document-password').fill('PRIVATE-SHOULD-NOT-PERSIST')
 page.locator('[name=consent]').check()
 page.locator('button[form=verification-form]').click();page.wait_for_timeout(150)
 store=page.evaluate('JSON.stringify(window.__testStorage)')
 assert 'PRIVATE-SHOULD-NOT-PERSIST' not in store
 own=page.evaluate('state.own');assert own[-1]['contract']=='yes' and own[-1]['residence_registration']=='ask'
 assert own[-1]['metro_walk_minutes']==10 and own[-1]['center_drive_minutes']==20
 assert own[-1]['available']=='2026-09-15' and own[-1]['available_until']=='2027-05-01'
 assert own[-1]['description']==source_description
 assert 'Сдаётся с' in page.locator('[data-listing="'+own[-1]['id']+'"]').inner_text()
 assert own[-1].get('document_status') not in ('owner_verified','document_checked')
 assert page.locator('.listing').count()==25
 # No scores, favorites or expiry controls.
 assert page.locator('[data-action=fav], [data-action=like], [data-action=renew]').count()==0
 assert not errors,errors
 page.emulate_media(color_scheme='dark');page.wait_for_timeout(100);page.screenshot(path=str(O/'dark-390.png'))
 # Live bootstrap is tested with intercepted API responses: no Telegram writes.
 import re
 live_page=b.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
 live_page.on('pageerror',lambda e:errors.append(str(e)))
 live_html=re.sub(r'(<script id="seed-data" type="application/json">).*?(</script>)',r'\1[]\2',(R/'web/index.html').read_text(encoding='utf-8'),flags=re.S)
 examples=json.loads((R/'seed.json').read_text(encoding='utf8'));requests=[];live_records=[];author_records=[]
 def serve(route):
  from urllib.parse import urlparse
  path=urlparse(route.request.url).path;requests.append((route.request.method,path))
  if route.request.url=='https://rent.test/':return route.fulfill(content_type='text/html',body=live_html)
  payload={'/api/config':{'live':True,'bot_username':'test_bot'},'/api/listings':live_records, '/api/examples':examples}
  for record in live_records:
   payload['/api/listings/'+record['id']]=record
   payload['/api/listings/'+record['id']+'/author-listings']={'available':True,'listings':author_records}
  if path in payload:return route.fulfill(content_type='application/json',body=json.dumps(payload[path]))
  route.abort()
 live_page.route('**/*',serve);live_page.goto('https://rent.test/')
 live_page.locator('.listing').first.wait_for()
 assert live_page.locator('.listing').count()==24
 assert 'Демокаталог' in live_page.locator('.catalog-note').inner_text()
 assert live_page.locator('[data-action=follow]').is_disabled()
 for w in [320,360,390,430]:
  live_page.set_viewport_size({'width':w,'height':844})
  assert live_page.evaluate('document.documentElement.scrollWidth')==w
  live_page.screenshot(path=str(O/f'live-examples-{w}.png'))
 live_page.locator('.listing-main').first.click()
 assert 'Учебный пример' in live_page.locator('.sheet').inner_text()
 assert live_page.locator('.sheet-footer [data-action=source]').count()==1
 assert live_page.locator('.publication-links [data-action=listing-post]').is_disabled()
 assert live_page.locator('.publication-links [data-action=author-listings]').is_disabled()
 assert live_page.locator('.publication-links [data-action=listing-comments]').is_disabled()
 live_page.locator('.publication-links').scroll_into_view_if_needed()
 live_page.screenshot(path=str(O/'publication-example-390.png'))
 assert not any(path.startswith('/api/listings/') for method,path in requests)
 live_page.locator('.sheet [data-action=close]').click();live_page.wait_for_timeout(100)
 live_page.locator('[data-action=show-live]').click()
 assert live_page.locator('.listing').count()==0
 assert 'Пока нет объявлений' in live_page.locator('.empty').inner_text()
 assert 'Снимите ограничение' not in live_page.locator('.empty').inner_text()
 assert live_page.locator('[data-action=show-live]').get_attribute('aria-pressed')=='true'
 # A real author's catalog works even when the publication channel is absent.
 first={**examples[0],'id':'live-first','address':'Тестовая 10','city':'Ереван','sample':False,'is_mine':False,'author_listings_available':True,'telegram_post_url':'','source_url':''}
 second={**first,'id':'live-second','address':'Тестовая 20','city':'Дилижан','district':'','telegram_post_url':'https://t.me/test_channel/45'}
 live_records.extend([first,second]);author_records.append(second)
 live_page.evaluate('refreshVisible()')
 live_page.locator('.listing-main').first.click()
 assert live_page.locator('.publication-links [data-action=listing-post]').is_disabled()
 assert 'Канал ещё не подключён' in live_page.locator('.publication-links').inner_text()
 assert live_page.locator('.publication-links [data-action=author-listings]').is_enabled()
 assert live_page.locator('.publication-links [data-action=listing-comments]').is_disabled()
 for w in [320,360,390,430]:
  live_page.set_viewport_size({'width':w,'height':844})
  live_page.locator('.publication-links').scroll_into_view_if_needed()
  assert live_page.evaluate('document.documentElement.scrollWidth')==w
  assert live_page.locator('.sheet-body').evaluate('(x)=>x.scrollWidth<=x.clientWidth')
  live_page.screenshot(path=str(O/f'publication-disconnected-{w}.png'))
 live_page.set_viewport_size({'width':390,'height':844})
 live_page.locator('[data-action=author-listings]').click()
 live_page.locator('.author-listing-row').wait_for()
 assert live_page.locator('.author-listing-row').count()==1
 assert 'Дилижан' in live_page.locator('.author-listing-row').inner_text()
 live_page.screenshot(path=str(O/'author-listings-390.png'))
 live_page.locator('.author-listing-row').click()
 assert 'Тестовая 20' in live_page.locator('.sheet .meta').first.inner_text()
 assert live_page.locator('.publication-links [data-action=listing-post]').is_enabled()
 live_page.evaluate("() => { safeOpen=url=>{window.__opened=url}; }")
 live_page.locator('.publication-links [data-action=listing-post]').click()
 assert live_page.evaluate('window.__opened')==second['telegram_post_url']
 author_records.clear()
 live_page.locator('[data-action=author-listings]').click()
 live_page.locator('.author-empty').wait_for()
 assert 'Других объявлений пока нет' in live_page.locator('.author-empty').inner_text()
 live_page.locator('.sheet-footer [data-action=detail]').click()
 assert 'Тестовая 20' in live_page.locator('.sheet .meta').first.inner_text()
 live_page.emulate_media(color_scheme='dark')
 live_page.locator('.publication-links').scroll_into_view_if_needed()
 live_page.screenshot(path=str(O/'publication-dark-390.png'))
 assert all(method=='GET' for method,path in requests)
 assert not errors,errors
 report={'result':'passed' ,'mobile_widths':widths,'initial_cards':24,'seed_records':39,'js_errors':errors,
         'scenarios':['disabled channel and discussion placeholders','actual Telegram post link','same author listings across city filters','empty author catalog and return navigation','feed','12 districts','source detail','follow filter','city choices and custom city','Yerevan-only metro and district','author travel estimate persistence','live empty catalog and isolated demo examples','manual fields without description parsing','exact optional rental dates','date order validation','description preserved when fields change','three review fields','optional conditions','local publish','private form does not persist credentials','dark theme'],
         'scope':'Chromium mobile emulation of built HTML and intercepted live API responses; not real Android/iOS Telegram; backend separately tested with TestClient.'}
 report['scenarios']=list(dict.fromkeys(report['scenarios']))
 (O/'mobile.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False))
 b.close()
