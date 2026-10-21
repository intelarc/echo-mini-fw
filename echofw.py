#!/usr/bin/env python3
"""Unpack / repack firmware for the FiiO SNOWSKY Echo Mini (Rockchip RKnano, ECHOVxxx.IMG).

    python echofw.py info   ECHOV190.IMG
    python echofw.py unpack ECHOV190.IMG work/
    python echofw.py pack   work/ ECHOECHO.IMG

`unpack` writes editable PNGs (UI graphics) and TSVs (menu text, 21 languages) plus
the raw code segments. `pack` only rewrites bytes you changed, in place, so every
offset and size in the image stays the same. It then refreshes the tail header copy
and the trailing RKCRC32. Needs Pillow (`pip install pillow`).
"""
import argparse, json, os, re, shutil, struct, sys

HDR_SIZE = 0x200
MAGIC_TAG = b'RKnanoFW'          # at 0x1F8, the only "signature" the device checks

# Header groups: (offset of count word, name of each blob)
GROUPS = {
    0x50: ['code0', 'code1', 'code2', 'system'],   # first three unused (size 0)
    0x74: ['font12', 'font16'],
    0xC8: ['charsets'],
    0xF4: ['menu'],
    0x148: ['images'],
}

LANGS = ['zh-CN', 'zh-TW', 'en', 'ja', 'ko', 'fr', 'de', 'it', 'es', 'pt', 'ru',
         'sv', 'th', 'pl', 'da', 'nl', 'el', 'cs', 'tr', 'he', 'ar']
MENU_REC = 258                   # 29 x u16 tree fields + 100 UTF-16 code units
MENU_HDR = 58
MENU_TXT = MENU_REC - MENU_HDR
IMG_META_REC = 108               # packer leftovers: name[64], junk[32], off, w, h


# --- RKCRC32: CRC-32, poly 0x04C10DB7, MSB-first, init 0, no final xor -----------

def _crc_table():
    t = []
    for i in range(256):
        c = i << 24
        for _ in range(8):
            c = ((c << 1) ^ 0x04C10DB7) if c & 0x80000000 else (c << 1)
            c &= 0xFFFFFFFF
        t.append(c)
    return t

_CRC_T = _crc_table()


def rkcrc32(buf, crc=0):
    t = _CRC_T
    for b in buf:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ t[(crc >> 24) ^ b]
    return crc


# --- container ---------------------------------------------------------------------

def u32(b, o):
    return struct.unpack_from('<I', b, o)[0]


def parse_header(img):
    if img[0x1F8:0x200] != MAGIC_TAG or u32(img, 0xC) != 0x76543210:
        sys.exit('not an RKnano firmware image')
    blobs = {}
    for goff, names in GROUPS.items():
        n = u32(img, goff)
        if n != len(names):
            sys.exit(f'unexpected header group at {goff:#x}: count {n}')
        for i, name in enumerate(names):
            off, size = struct.unpack_from('<II', img, goff + 4 + 8 * i)
            if size:
                blobs[name] = (off, size)
    total = u32(img, 0x1F4)
    y, md = struct.unpack_from('<HH', img, 0)
    return {
        'date': f'{y:04x}-{md >> 8:02x}-{md & 0xFF:02x}',
        'vendor': img[0x10:0x30].rstrip(b'\0').decode(),
        'sdk': img[0x30:0x50].rstrip(b'\0').decode(),
        'total': total,
        'blobs': blobs,
    }


def check_image(img):
    h = parse_header(img)
    total = h['total']
    return {
        'tail header matches': img[total:total + HDR_SIZE] == img[:HDR_SIZE],
        'crc stored': u32(img, len(img) - 4),
    }


# --- code segment table (start of the 'system' blob) -------------------------------

def segments(img, blobs):
    off, _ = blobs['system']
    base, n = struct.unpack_from('<II', img, off)
    segs = []
    for i in range(n):
        cl, ci, cn, dl, di, dn, bi, bn = struct.unpack_from('<8I', img, off + 8 + 32 * i)
        segs.append({'i': i, 'code_file': off + cl - base, 'code_addr': ci, 'code_len': cn,
                     'data_file': off + dl - base, 'data_addr': di, 'data_len': dn,
                     'bss_addr': bi, 'bss_len': bn})
    return segs


# --- images: 'ROCK26IMAGERES' table of big-endian RGB565 bitmaps -------------------

def image_table(img, blobs):
    off, size = blobs['images']
    if img[off:off + 14] != b'ROCK26IMAGERES':
        sys.exit('image resource magic missing')
    n = u32(img, off + 0x10)
    ents = []
    for i in range(n):
        w, h, x, y, length, doff = struct.unpack_from('<HHHHII', img, off + 0x20 + 16 * i)
        ents.append({'w': w, 'h': h, 'x': x, 'y': y, 'file': off + doff, 'len': length})
    # Names live in leftover packer records right after the last bitmap.
    meta = max(e['file'] + e['len'] for e in ents)
    for i, e in enumerate(ents):
        raw = img[meta + IMG_META_REC * i: meta + IMG_META_REC * i + 64].split(b'\0')[0]
        name = raw.decode('ascii', 'replace').strip()
        name = re.sub(r'_*\(\d+[,.]\d+\)\.BMP$', '', name, flags=re.I)
        e['name'] = re.sub(r'[^A-Za-z0-9_-]', '_', name) or f'img{i}'
    return ents


def rgb565be_to_png(data, w, h, path):
    from PIL import Image
    px = struct.unpack(f'>{w * h}H', data)
    rgb = bytearray(w * h * 3)
    for i, p in enumerate(px):
        r, g, b = p >> 11, (p >> 5) & 63, p & 31
        rgb[3 * i] = (r << 3) | (r >> 2)
        rgb[3 * i + 1] = (g << 2) | (g >> 4)
        rgb[3 * i + 2] = (b << 3) | (b >> 2)
    Image.frombytes('RGB', (w, h), bytes(rgb)).save(path, optimize=True)


def png_to_rgb565be(path, w, h):
    from PIL import Image
    im = Image.open(path).convert('RGB')
    if im.size != (w, h):
        sys.exit(f'{path}: is {im.size[0]}x{im.size[1]}, must stay {w}x{h}')
    rgb = im.tobytes()
    out = bytearray(w * h * 2)
    for i in range(w * h):
        r, g, b = rgb[3 * i], rgb[3 * i + 1], rgb[3 * i + 2]
        struct.pack_into('>H', out, 2 * i, ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3))
    return bytes(out)


# --- menu text: 21 language blocks of fixed 258-byte tree records ------------------

def menu_layout(img, blobs):
    off, size = blobs['menu']
    n = struct.unpack_from('<H', img, off)[0]
    starts = struct.unpack_from(f'<{n}I', img, off + 2)
    per_lang = starts[1] - starts[0]
    if per_lang % MENU_REC:
        sys.exit('menu block size is not a whole number of records')
    return [off + s for s in starts], per_lang // MENU_REC


def menu_records(img, lang_off, count):
    for r in range(count):
        o = lang_off + r * MENU_REC
        fields = struct.unpack_from('<29H', img, o)
        txt = img[o + MENU_HDR:o + MENU_REC].decode('utf-16le', 'replace').split('\0')[0]
        yield r, o + MENU_HDR, fields, txt


def tsv_escape(s):
    return s.replace('\\', '\\\\').replace('\t', '\\t').replace('\n', '\\n').replace('\r', '\\r')


def tsv_unescape(s):
    return re.sub(r'\\(.)', lambda m: {'t': '\t', 'n': '\n', 'r': '\r'}.get(m[1], m[1]), s)


def read_tsv(path):
    rows = {}
    with open(path, encoding='utf-8') as f:
        next(f)
        for line in f:
            line = line.rstrip('\n')
            if not line:
                continue
            idx, parent, text = line.split('\t', 2)
            rows[int(idx)] = tsv_unescape(text)
    return rows


# --- commands ------------------------------------------------------------------------

def cmd_info(args):
    img = open(args.image, 'rb').read()
    h = parse_header(img)
    print(f"{h['vendor']} / {h['sdk']}, build date {h['date']}, {len(img):,} bytes")
    for name, (off, size) in h['blobs'].items():
        print(f'  {name:9s} @ {off:#010x}  {size:>10,} bytes')
    print(f"  firmware body ends at {h['total']:#x}; tail = header copy + padding + RKCRC32")
    chk = check_image(img)
    print(f"  tail header matches: {chk['tail header matches']}")
    calc = rkcrc32(img[:-4])
    print(f"  RKCRC32 stored {chk['crc stored']:#010x}, computed {calc:#010x} "
          f"-> {'OK' if calc == chk['crc stored'] else 'MISMATCH'}")
    segs = segments(img, h['blobs'])
    print(f'  {len(segs)} code segments (system core 0x03xxxxxx, codec core 0x01xxxxxx)')
    print(f"  {len(image_table(img, h['blobs']))} images, "
          f"{len(menu_layout(img, h['blobs'])[0])} languages")


def cmd_unpack(args):
    img = open(args.image, 'rb').read()
    h = parse_header(img)
    out = args.outdir
    os.makedirs(out, exist_ok=True)
    shutil.copyfile(args.image, os.path.join(out, 'base.img'))

    # code segments, for loading into Ghidra/IDA at their execution addresses
    segs = segments(img, h['blobs'])
    os.makedirs(os.path.join(out, 'code'), exist_ok=True)
    with open(os.path.join(out, 'code', 'segments.tsv'), 'w', encoding='utf-8') as f:
        f.write('seg\tcode_addr\tcode_len\tdata_addr\tdata_len\tbss_addr\tbss_len\tfile\n')
        for s in segs:
            fn = ''
            if s['code_len']:
                fn = f"seg{s['i']:02d}_{s['code_addr']:08x}.bin"
                with open(os.path.join(out, 'code', fn), 'wb') as g:
                    g.write(img[s['code_file']:s['code_file'] + s['code_len']])
            if s['data_len']:
                with open(os.path.join(out, 'code', f"seg{s['i']:02d}_{s['data_addr']:08x}.data.bin"), 'wb') as g:
                    g.write(img[s['data_file']:s['data_file'] + s['data_len']])
            f.write(f"{s['i']}\t{s['code_addr']:#010x}\t{s['code_len']:#x}\t{s['data_addr']:#010x}\t"
                    f"{s['data_len']:#x}\t{s['bss_addr']:#010x}\t{s['bss_len']:#x}\t{fn}\n")

    # raw resource blobs (images are covered by the PNGs below)
    os.makedirs(os.path.join(out, 'raw'), exist_ok=True)
    for name, (off, size) in h['blobs'].items():
        if name == 'images':
            continue
        with open(os.path.join(out, 'raw', f'{name}.bin'), 'wb') as g:
            g.write(img[off:off + size])

    # images
    ents = image_table(img, h['blobs'])
    idir = os.path.join(out, 'images')
    os.makedirs(idir, exist_ok=True)
    index = []
    for i, e in enumerate(ents):
        fn = f"{i:03d}_{e['name']}.png"
        rgb565be_to_png(img[e['file']:e['file'] + e['len']], e['w'], e['h'], os.path.join(idir, fn))
        index.append({'i': i, 'png': fn, 'w': e['w'], 'h': e['h'], 'x': e['x'], 'y': e['y']})
        if i % 100 == 0:
            print(f'  images {i}/{len(ents)}', flush=True)

    # menu text
    sdir = os.path.join(out, 'strings')
    os.makedirs(sdir, exist_ok=True)
    langs, count = menu_layout(img, h['blobs'])
    for li, loff in enumerate(langs):
        code = LANGS[li] if li < len(LANGS) else f'lang{li}'
        with open(os.path.join(sdir, f'{li:02d}_{code}.tsv'), 'w', encoding='utf-8', newline='\n') as f:
            f.write('index\tparent\ttext\n')
            for r, _, fields, txt in menu_records(img, loff, count):
                parent = '' if fields[1] == 0xFFFF else str(fields[1])
                f.write(f'{r}\t{parent}\t{tsv_escape(txt)}\n')

    with open(os.path.join(out, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump({'source': os.path.basename(args.image), 'date': h['date'],
                   'blobs': {k: list(v) for k, v in h['blobs'].items()},
                   'images': index}, f, indent=1, ensure_ascii=False)
    print(f'unpacked to {out}: {len(ents)} images, {len(langs)} languages x {count} strings, '
          f'{sum(1 for s in segs if s["code_len"])} code segments')


def read_text_patch(path):
    """lang<TAB>index<TAB>text rows; lang '*' applies to every language."""
    patch = {}
    with open(path, encoding='utf-8') as f:
        next(f)
        for line in f:
            line = line.rstrip('\n')
            if not line or line.startswith('#'):
                continue
            lang, idx, text = line.split('\t', 2)
            for code in (LANGS if lang == '*' else [lang]):
                patch.setdefault(code, {})[int(idx)] = tsv_unescape(text)
    return patch


def cmd_pack(args):
    src = args.workdir
    img = bytearray(open(os.path.join(src, 'base.img'), 'rb').read())
    h = parse_header(img)
    changed = []
    patch = read_text_patch(args.text_patch) if args.text_patch else {}

    ents = image_table(img, h['blobs'])
    manifest = json.load(open(os.path.join(src, 'manifest.json'), encoding='utf-8'))
    for e, m in zip(ents, manifest['images']):
        path = os.path.join(src, 'images', m['png'])
        if args.images and os.path.exists(os.path.join(args.images, m['png'])):
            path = os.path.join(args.images, m['png'])
        if not os.path.exists(path):
            continue
        new = png_to_rgb565be(path, e['w'], e['h'])
        if new != img[e['file']:e['file'] + e['len']]:
            img[e['file']:e['file'] + e['len']] = new
            changed.append(f"image {m['png']}")

    langs, count = menu_layout(img, h['blobs'])
    for li, loff in enumerate(langs):
        code = LANGS[li] if li < len(LANGS) else f'lang{li}'
        path = os.path.join(src, 'strings', f'{li:02d}_{code}.tsv')
        edits = read_tsv(path) if os.path.exists(path) else {}
        edits.update(patch.get(code, {}))
        for r, toff, _, txt in menu_records(img, loff, count):
            new = edits.get(r, txt)
            if new == txt:
                continue
            enc = new.encode('utf-16le')
            if len(enc) + 2 > MENU_TXT:
                sys.exit(f'{code} #{r}: {len(new)} chars, max {MENU_TXT // 2 - 1}')
            img[toff:toff + MENU_TXT] = enc + b'\0' * (MENU_TXT - len(enc))
            changed.append(f'{code} #{r}: {txt!r} -> {new!r}')

    # Tail: header copy at 'total' must equal the head (device checks this), then CRC.
    total = h['total']
    img[total:total + HDR_SIZE] = img[:HDR_SIZE]
    print('computing RKCRC32 ...', flush=True)
    struct.pack_into('<I', img, len(img) - 4, rkcrc32(memoryview(img)[:-4]))
    with open(args.output, 'wb') as f:
        f.write(img)
    for c in changed:
        print('  changed', c)
    print(f'wrote {args.output} ({len(changed)} change(s))')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('info'); p.add_argument('image'); p.set_defaults(fn=cmd_info)
    p = sub.add_parser('unpack'); p.add_argument('image'); p.add_argument('outdir'); p.set_defaults(fn=cmd_unpack)
    p = sub.add_parser('pack'); p.add_argument('workdir'); p.add_argument('output')
    p.add_argument('--text-patch', help='TSV of lang/index/text overrides applied on top of strings/*.tsv')
    p.add_argument('--images', help='folder of PNGs (same names as images/) that override the workdir ones')
    p.set_defaults(fn=cmd_pack)
    args = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    args.fn(args)


if __name__ == '__main__':
    main()
