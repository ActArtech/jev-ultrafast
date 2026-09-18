"""Real DOM regression checks for observed state/actions. No model calls."""
import json
from urllib.parse import quote

from jev_ultrafast.browser import Browser

HTML = '''<!doctype html><title>State/action checks</title>
<style>body{margin:30px}input,button{min-height:28px}</style>
<div style="display:none">Hidden success sentinel: completed successfully</div>
<label>Email <input id="email" type="email" required></label>
<label>Date <input id="date" type="date" required></label>
<label>Volume <input id="range" type="range" min="1" max="9" value="1"></label>
<label>Document <input id="file" type="file" accept=".txt"></label>
<label for="hidden-file">Hidden Document</label><input id="hidden-file" type="file" style="display:none">
<label>Photo <input id="photo" type="file" accept="image/*"></label>
<div><label>Email editor</label><div><div id="rich" contenteditable="true" style="min-height:35px"></div></div></div>
<svg width="300" height="30"><text x="0" y="20">1995 2017 37% 24%</text></svg>
<a id="styled" href="#ok"><style>.noise{color:red}</style>Clean label</a>
<div><label>Overlay document</label><div style="position:relative;width:180px;height:36px">
<input id="overlay" type="file" required
 style="position:absolute;inset:0;width:100%;height:100%;opacity:0"><span>Choose file</span></div></div>
<input id="noninteractive" type="file" style="opacity:0;pointer-events:none">
<div id="shadow"></div><iframe id="frame"></iframe>
<button id="offscreen" style="position:absolute;top:2400px" onclick="window.clicked=true">Far away</button>'''


def main():
    b = Browser('data:text/html,' + quote(HTML))
    passed = []
    try:
        b.evaluate("document.querySelector('#shadow').attachShadow({mode:'open'}).innerHTML=" + json.dumps(
            '<label>Shadow value <input id="inner"></label>'))
        b.evaluate("document.querySelector('#frame').srcdoc=" + json.dumps(
            '<label>Frame value <input></label><button onclick="window.hit=true">Frame button</button>'))
        page = b.observe(screenshot=False)
        assert page['focus'] is None
        assert 'Hidden success sentinel' not in json.dumps({
            k: page.get(k) for k in ['text', 'document_text', 'focus', 'actions']})
        passed.append('hidden success text never leaks through body focus or model state')
        for _ in range(10):
            if any(a['label'] == 'Frame value' for a in page['actions']):
                break
            page = b.observe(screenshot=False)
        assert any(a['label'] == 'Shadow value' and a['kind'] == 'fill' for a in page['actions'])
        assert any(a['label'] == 'Frame value' and a['kind'] == 'fill' for a in page['actions'])
        passed.append('shadow/frame controls observed')
        assert any(a['label'] == 'Clean label' for a in page['actions'])
        assert 'color:red' not in json.dumps(page['actions'])
        passed.append('CSS omitted from labels')
        assert '1995 2017 37% 24%' in page['document_text']
        passed.append('rendered SVG text is observed')
        assert any(a['label']=='Overlay document' and a['kind']=='upload' for a in page['actions'])
        hidden_id=b.evaluate("window.__jevFast.ids.get(document.querySelector('#noninteractive'))")
        assert not any(a.get('node')==hidden_id for a in page['actions'])
        passed.append('transparent native overlay is actionable; pointer-disabled hidden input is omitted')
        for label, value, assertion in [
            ('Date', '2026-09-20', "document.querySelector('#date').value==='2026-09-20'"),
            ('Volume', '9', "document.querySelector('#range').value==='9'"),
            ('Document', 'An authorized sample document.', "document.querySelector('#file').files[0].size===30"),
            ('Hidden Document', 'sample', "document.querySelector('#hidden-file').files[0].size===6"),
            ('Overlay document', 'sample', "document.querySelector('#overlay').files[0].size===6"),
            ('Photo', 'A sample image', "document.querySelector('#photo').files[0].type==='image/png'"),
            ('Email editor', 'person@example.com', "document.querySelector('#rich').innerText==='person@example.com'"),
            ('Shadow value', 'shadow entry',
             "document.querySelector('#shadow').shadowRoot.querySelector('input').value==='shadow entry'"),
            ('Frame value', 'frame entry',
             "document.querySelector('#frame').contentDocument.querySelector('input').value==='frame entry'"),
        ]:
            page = b.observe(screenshot=False)
            a = next(a for a in page['actions'] if a['label'] == label and a['kind'] != 'click')
            b.act(a, page, text=value)
            b.observe(screenshot=False)
            assert b.evaluate(assertion), label
            passed.append(label + ' executed correctly')
        page = b.observe(screenshot=False)
        a = next(a for a in page['actions'] if a['label'] == 'Frame button')
        b.act(a, page)
        assert b.evaluate("document.querySelector('#frame').contentWindow.hit")
        passed.append('frame click uses top-level coordinates')
        page = b.observe(screenshot=False)
        a = next(a for a in page['actions'] if a['kind'] == 'scroll_to' and 'Far away' in a['label'])
        b.act(a, page)
        page = b.observe(screenshot=False)
        a = next(a for a in page['actions'] if a['label'] == 'Far away' and a['kind'] == 'click')
        b.act(a, page)
        assert b.evaluate('window.clicked')
        passed.append('offscreen control scrolls into view before clicking')
        b.evaluate("scrollTo(0,0)")
        page=b.observe(screenshot=False)
        a=next(a for a in page['actions'] if a['label']=='Email' and a['kind']=='fill')
        b.evaluate("document.querySelector('#offscreen').textContent='Unrelated changing ticker'")
        assert b.fresh(page,a)
        b.act(a,page,text='fresh@example.com')
        assert b.evaluate("document.querySelector('#email').value")=='fresh@example.com'
        passed.append('unrelated text changes do not cancel generated field values')
        b.evaluate("""document.body.insertAdjacentHTML('beforeend',
          '<div id=visual-order style=position:absolute;top:100px></div>');
          document.querySelector('#visual-order').innerHTML=Array.from({length:250},(_,i)=>
          '<div style=position:absolute;top:'+(250-i)*20+'px>Row '+i+' '+('text '.repeat(30))+'</div>').join('')""")
        page=b.observe(screenshot=False)
        assert page['document_text'].find('Row 249 ') < page['document_text'].find('Row 248 ')
        assert 'Row 249 ' in page['text']
        passed.append('CSS visual order survives long-document truncation')
        print('PASS:', len(passed), 'state/action checks;', '; '.join(passed))
    finally:
        b.close()


if __name__ == '__main__':
    main()
