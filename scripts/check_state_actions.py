"""Real DOM regression checks for observed state/actions. No model calls."""
import json
from urllib.parse import quote

from jev_ultrafast.browser import Browser

HTML = '''<!doctype html><title>State/action checks</title>
<style>body{margin:30px}input,button{min-height:28px}</style>
<label>Email <input id="email" type="email" required></label>
<label>Date <input id="date" type="date" required></label>
<label>Volume <input id="range" type="range" min="1" max="9" value="1"></label>
<label>Document <input id="file" type="file" accept=".txt"></label>
<a id="styled" href="#ok"><style>.noise{color:red}</style>Clean label</a>
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
        for label, value, assertion in [
            ('Date', '2026-09-20', "document.querySelector('#date').value==='2026-09-20'"),
            ('Volume', '9', "document.querySelector('#range').value==='9'"),
            ('Document', 'An authorized sample document.', "document.querySelector('#file').files[0].size===30"),
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
        print('PASS:', len(passed), 'state/action checks;', '; '.join(passed))
    finally:
        b.close()


if __name__ == '__main__':
    main()
