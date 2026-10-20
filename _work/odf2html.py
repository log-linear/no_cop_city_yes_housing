#!/usr/bin/env python3
"""Convert selected ODP slides to standalone HTML (pandoc has no odp reader)."""
import os, re, shutil, struct, sys
import xml.etree.ElementTree as ET

OFF='{urn:oasis:names:tc:opendocument:xmlns:office:1.0}'
D  ='{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}'
ST ='{urn:oasis:names:tc:opendocument:xmlns:style:1.0}'
TX ='{urn:oasis:names:tc:opendocument:xmlns:text:1.0}'
FO ='{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}'
SV ='{urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0}'
XL ='{urn:oasis:names:tc:opendocument:xmlns:presentation:1.0}'
LK ='{http://www.w3.org/1999/xlink}'

PAGE_W_CM, PAGE_H_CM = 33.867, 19.05
PAGE_W_PX = 1280.0
CM = PAGE_W_PX / PAGE_W_CM          # 37.7953 px per cm (== CSS 1cm)

def px(v, dflt=0.0):
    if v is None: return dflt
    m = re.match(r'^\s*(-?[\d.]+)\s*(cm|mm|pt|in|px|%)?\s*$', str(v))
    if not m: return dflt
    n, u = float(m.group(1)), m.group(2) or 'cm'
    return n * {'cm':CM,'mm':CM/10,'pt':96/72,'in':96,'px':1,'%':1}[u]

def pt2px(v):
    m = re.match(r'^\s*([\d.]+)\s*(pt|cm)?\s*$', str(v or ''))
    if not m: return None
    return '%.1fpx' % (float(m.group(1)) * (96/72 if m.group(2) != 'cm' else CM))

def esc(s): return s.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')

class Styles:
    def __init__(self, files):
        self.defs, self.lists = {}, {}
        for f in files:
            for st in ET.parse(f).getroot().iter(ST+'style'):
                n = st.get(ST+'name')
                if n: self.defs.setdefault(n, st)
            for ls in ET.parse(f).getroot().iter(TX+'list-style'):
                n = ls.get(TX+'style-name')
                if not n: continue
                lv = {}
                for e in ls.findall(TX+'list-level-style-bullet'):
                    p = e.find(ST+'list-level-properties'); t = e.find(ST+'text-properties')
                    lv[int(e.get(TX+'level','1'))] = dict(
                        char=e.get(TX+'bullet-char','•'),
                        before=px(p.get(TX+'space-before') if p is not None else None),
                        label=px(p.get(TX+'min-label-width') if p is not None else None),
                        color=t.get(FO+'color') if t is not None else None)
                self.lists[n] = lv
        self.seen = set()
    def merged(self, name, _chain=None):
        if not name or name in (_chain or ()): return {}
        st = self.defs.get(name)
        if st is None: return {}
        chain = (_chain or frozenset()) | {name}
        out = self.merged(st.get(ST+'parent-style-name'), chain)
        for grp in st:
            tag = grp.tag[len(ST):]
            if tag in ('graphic-properties','text-properties','paragraph-properties'):
                out.update(grp.attrib)
        return out

def css_graphic(a):
    c=[]
    fill=a.get(D+'fill'); col=a.get(D+'fill-color')
    if fill=='solid' and col: c.append('background-color:%s'%col)
    elif fill=='none': c.append('background-color:transparent')
    if a.get(D+'stroke')=='solid' and px(a.get(SV+'stroke-width'))>0:
        c.append('border:%.2fpx solid %s'%(px(a.get(SV+'stroke-width')), a.get(SV+'stroke-color') or '#000'))
    v={'middle':'center','bottom':'flex-end'}.get(a.get(D+'textarea-vertical-align'),'flex-start')
    c.append('display:flex;flex-direction:column;justify-content:%s'%v)
    for k in ('padding-top','padding-right','padding-bottom','padding-left'):
        if a.get(FO+k) is not None: c.append('%s:%.2fpx'%(k,px(a.get(FO+k))))
    return c

def css_para(a):
    c=[]
    al=a.get(FO+'text-align')
    if al: c.append('text-align:'+{'start':'left','end':'right'}.get(al,al))
    lh=a.get(FO+'line-height')
    if lh: c.append('line-height:%s'%(lh if lh.endswith('%') else '%.2fpx'%px(lh)))
    for k in ('margin-top','margin-bottom','margin-left'):
        if a.get(FO+k) is not None: c.append('%s:%.2fpx'%(k,px(a.get(FO+k))))
    fs=pt2px(a.get(FO+'font-size'))
    if fs: c.append('font-size:'+fs)
    return c

def css_text(a):
    c=[]
    for k,o in (('color',FO+'color'),('font-style',FO+'font-style'),
                ('font-variant',FO+'font-variant'),('text-transform',FO+'text-transform'),
                ('letter-spacing',FO+'letter-spacing')):
        v=a.get(o)
        if v and v!='normal': c.append('%s:%s'%(k,v))
    w=a.get(FO+'font-weight')
    if w and w!='normal': c.append('font-weight:%s'%w)
    fam=a.get(ST+'font-name') or a.get(FO+'font-family')
    if fam:
        f=fam.lower()
        sub='Georgia,serif' if 'bowlby' in f else ('Arial,Helvetica,sans-serif' if ('calibri' in f or 'arial' in f) else "'Public Sans','Helvetica Neue',Arial,sans-serif")
        c.append("font-family:'%s',%s"%(fam,sub))
    fs=pt2px(a.get(FO+'font-size'))
    if fs: c.append('font-size:'+fs)
    if a.get(ST+'text-underline-style') not in (None,'none'): c.append('text-decoration:underline')
    pos=a.get(ST+'text-position')
    if pos:
        n=int(pos.split()[0].rstrip('%'))
        if n>0: c.append('vertical-align:super')
        elif n<0: c.append('vertical-align:sub')
    return c

def clip_rect(a):
    m=re.match(r'rect\(([^)]*)\)', a or '')
    if not m: return (0,0,0,0)
    p=[px(x) for x in m.group(1).split(',')]
    return p if len(p)==4 else (0,0,0,0)

def png_size(path):
    with open(path,'rb') as f: d=f.read(24)
    return struct.unpack('>II', d[16:24])

class Slide:
    def __init__(self, el, num, styles, media, outdir):
        self.el, self.num, self.st = el, num, styles
        self.media, self.outdir = media, outdir
        self.used_css, self.img_names, self._seq = [], {}, 0

    # ---- images ----
    def save_image(self, href):
        if href in self.img_names: return self.img_names[href]
        self._seq += 1
        ext = (os.path.splitext(href)[1] or '.png').lower()
        name = '%d_img_%d%s' % (self.num, self._seq, ext)
        src = os.path.join(self.media, href)
        if os.path.exists(src): shutil.copy2(src, os.path.join(self.outdir, name))
        else: sys.stderr.write('WARN missing media %s\n' % href)
        self.img_names[href] = name
        return name

    def image_html(self, frame):
        img = frame.find(D+'image')
        if img is None: return None
        href = img.get(LK+'href') if False else img.get('{http://www.w3.org/1999/xlink}href')
        if not href: return None
        ga = self.st.merged(frame.get(D+'style-name'))
        fw, fh = px(frame.get(SV+'width')), px(frame.get(SV+'height'))
        t, r, b, l = clip_rect(ga.get(FO+'clip'))
        nw = nh = None
        path = os.path.join(self.media, href)
        if href.lower().endswith('.png') and os.path.exists(path):
            try:
                wpx, hpx = png_size(path)
                nw, nh = wpx/96*2.54*CM, hpx/96*2.54*CM     # natural px on screen
            except Exception: pass
        if not nw:                                    # no crop info: stretch
            return '<img src="%s" alt="" style="width:%.1fpx;height:%.1fpx;display:block">' % (self.save_image(href), fw, fh)
        vw, vh = nw - l - r, nh - t - b
        s = (fw/vw) if vw > 0 else 1
        if abs(vw/vh - fw/fh) > 0.02: s = min(fw/vw, fh/vh)
        style = ('position:absolute;left:%.2fpx;top:%.2fpx;width:%.2fpx;height:%.2fpx;'
                 'max-width:none;display:block' % (-l*s, -t*s, nw*s, nh*s))
        return ('<div class="clip" style="position:absolute;inset:0;overflow:hidden">'
                '<img src="%s" alt="" style="%s"></div>' % (self.save_image(href), style))

    # ---- inline text ----
    def inline(self, node, css=''):
        buf = []
        if node.text: buf.append(esc(node.text))
        for ch in node:
            tag = ch.tag
            extra = ''
            if tag == TX+'span':
                extra = ';'.join(css_text(self.st.merged(ch.get(TX+'style-name'))))
                inner = self.inline(ch, extra)
                buf.append('<span style="%s">%s</span>' % (extra, inner) if extra else inner)
            elif tag == TX+'a':
                href = ch.get('{http://www.w3.org/1999/xlink}href') or '#'
                buf.append('<a href="%s" style="color:inherit">%s</a>' % (esc(href), self.inline(ch, css)))
            elif tag == TX+'s':
                buf.append('&nbsp;' * int(ch.text or 1))
            elif tag == TX+'tab':
                buf.append('&nbsp;&nbsp;&nbsp;&nbsp;')
            elif tag in (TX+'line-break',):
                buf.append('<br>')
            elif tag == TX+'page-number':
                buf.append(str(self.num))
            else:
                buf.append(self.inline(ch, css))
            if ch.tail: buf.append(esc(ch.tail))
        return ''.join(buf)

    def para(self, p, base=None):
        # ODF: shape's draw:text-style-name is the default paragraph style;
        # the paragraph's own style-name layers on top of it.
        a = self.st.merged(base)
        a.update(self.st.merged(p.get(TX+'style-name')))
        parts = [s for s in css_para(a) if s]
        parts.append('margin-left:%.2fpx' % px(a.get(FO+'margin-left'), 0))
        parts.append('margin-right:0;margin-top:0;margin-bottom:0')
        txt = self.inline(p)
        if not txt.strip(): txt = '&nbsp;'
        return '<p style="%s">%s</p>' % (';'.join(parts), txt)

    def list_html(self, lst, depth=1):
        ls = self.st.lists.get(lst.get(TX+'style-name'), {})
        info = ls.get(depth) or (ls and list(ls.values())[0]) or dict(char='•', before=4, label=18, color=None)
        items = []
        for li in lst.findall(TX+'list-item'):
            inner = []
            for sub in li:
                if sub.tag == TX+'p': inner.append(self.para(sub))
                elif sub.tag == TX+'list': inner.append(self.list_html(sub, depth+1))
            items.append('<li>%s</li>' % ''.join(inner))
        cls = 'l%d_%s' % (depth, re.sub(r'\W','_',lst.get(TX+'style-name') or 'x'))
        pad = (info['before'] or 0) + (info['label'] or 18)
        self.used_css.append(
            '.%s{list-style:none;margin:0;padding-left:%.2fpx}'
            '.%s>li{position:relative}'
            '.%s>li::before{content:"%s";position:absolute;left:%.2fpx;top:0;color:%s}'
            % (cls, pad, cls, cls, info['char'], -pad, info['color'] or 'inherit'))
        return '<ul class="%s">%s</ul>' % (cls, ''.join(items))

    # ---- shapes ----
    def shape_html(self, sh):
        ga = self.st.merged(sh.get(D+'style-name'))
        x, y = px(sh.get(SV+'x')), px(sh.get(SV+'y'))
        w, h = px(sh.get(SV+'width')), px(sh.get(SV+'height'))
        css = ['position:absolute;left:%.2fpx;top:%.2fpx;width:%.2fpx;height:%.2fpx;box-sizing:border-box' % (x, y, w, h)]
        css += css_graphic(ga)
        eg = sh.find(D+'enhanced-geometry')
        if eg is not None and 'round' in (eg.get(D+'type') or ''):
            css.append('border-radius:5px')
        ta = sh.find(D+'text-box')
        if sh.tag == D+'frame':
            im = self.image_html(sh)
            if im is not None: return '<div class="sh" style="%s">%s</div>' % (';'.join(css), im)
            if ta is None: return None
        body = []
        src = ta if ta is not None else sh      # Google exports put text:p directly
        fs = pt2px(ga.get(FO+'font-size'))
        if fs: css.append('font-size:'+fs)
        # base paragraph style declared on the shape (draw:text-style-name)
        base_style = sh.get(D+'text-style-name')
        for ch in src:
            if ch.tag == TX+'p': body.append(self.para(ch, base_style))
            elif ch.tag == TX+'list': body.append(self.list_html(ch))
        title = sh.get(D+'name') or ''
        return '<div class="sh" style="%s" data-odf="%s">%s</div>' % (';'.join(css), esc(title), ''.join(body))

    def render(self):
        pages = self.el
        dp = self.st.merged(pages.get(D+'style-name'))
        bg = dp.get(D+'fill-color') if dp.get(D+'fill') == 'solid' else '#ffffff'
        blocks = []
        for sh in pages:
            if sh.tag in (D+'custom-shape', D+'frame'):
                out = self.shape_html(sh)
                if out: blocks.append(out)
        css = '\n'.join(self.used_css)
        return TEMPLATE % dict(bg=bg, css=css, body='\n'.join(blocks), num=self.num)

TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Budget Tour 2.0: Bonds &amp; County Taxes - Slide %(num)s</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:#2b2f2c}
body{display:flex;align-items:center;justify-content:center;min-height:100vh}
.stage{width:1280px;height:720px;transform-origin:center center}
.slide{position:relative;width:1280px;height:720px;background:%(bg)s;overflow:hidden;
  font-family:'Public Sans','Helvetica Neue',Arial,sans-serif;color:#3f4a42}
.sh{overflow:hidden}
p,ul,li{font-size:inherit}
a{color:inherit}
img{image-rendering:auto}
%%(css)s
@media print{body{background:#fff}.stage{transform:none!important}}
</style>
</head>
<body>
<div class="stage" id="stage">
<div class="slide" data-slide="%(num)s">
%(body)s
</div>
</div>
<script>
function fit(){var s=document.getElementById('stage');
  var k=Math.min(innerWidth/1280,innerHeight/720);
  s.style.transform='scale('+k+')';}
addEventListener('resize',fit);fit();
</script>
</body>
</html>
'''

def main():
    odp_dir, outdir, nums = sys.argv[1], sys.argv[2], [int(x) for x in sys.argv[3:]]
    content = os.path.join(odp_dir, 'content.xml')
    styles_path = os.path.join(odp_dir, 'styles.xml')
    st = Styles([styles_path, content])
    root = ET.parse(content).getroot()
    pages = root.find(OFF+'body/'+OFF+'presentation').findall(D+'page')
    os.makedirs(outdir, exist_ok=True)
    for n in nums:
        if not (1 <= n <= len(pages)):
            sys.exit('slide %d out of range (1..%d)' % (n, len(pages)))
        p = pages[n-1]
        assert p.get(D+'name') == 'page%d' % n, (p.get(D+'name'), n)
        sl = Slide(p, n, st, odp_dir, outdir)
        html = sl.render()
        dest = os.path.join(outdir, '%d.html' % n)
        with open(dest, 'w', encoding='utf-8') as f:
            f.write(html)
        print('wrote %-10s shapes=%-3d images=%s' % (
            dest, html.count('class="sh"'),
            ', '.join(sorted(sl.img_names.values())) or 'none'))

if __name__ == '__main__':
    main()
