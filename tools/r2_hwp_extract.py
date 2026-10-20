"""Read-only bounded HWP5 paragraph extraction for official requirement evidence.

Does not render, edit or fill application/report forms. Local extracted text is
research evidence and is excluded from published packages.
"""
import argparse
from pathlib import Path
import struct
import zlib
import olefile


def extract(path):
    paragraphs=[]
    with olefile.OleFileIO(path) as o:
        flags=struct.unpack_from('<I',o.openstream('FileHeader').read(),36)[0]
        if flags & 2: raise ValueError('Encrypted HWP is unsupported')
        for name in sorted(o.listdir()):
            if name[0]!='BodyText': continue
            data=o.openstream(name).read()
            if flags & 1: data=zlib.decompress(data,-15)
            if len(data)>16*1024**2: raise ValueError('Section exceeds extraction bound')
            pos=0
            while pos+4<=len(data):
                h=struct.unpack_from('<I',data,pos)[0];pos+=4;tag=h&1023;size=h>>20
                if size==4095: size=struct.unpack_from('<I',data,pos)[0];pos+=4
                if pos+size>len(data): raise ValueError('Truncated HWP record')
                payload=data[pos:pos+size];pos+=size
                if tag!=67: continue
                words=struct.unpack('<'+'H'*(len(payload)//2),payload[:len(payload)//2*2]);clean=[];i=0
                while i<len(words):
                    w=words[i]
                    if w in (9,10,13): clean.append(' ' if w==9 else '\n');i+=1
                    elif w<32: i+=8 if w in (1,2,3,11,12,14,15,16,17,18,19,20,21,22,23) else 1
                    else: clean.append(chr(w));i+=1
                paragraphs.append(''.join(clean))
    return '\n'.join(paragraphs)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('input');p.add_argument('output');a=p.parse_args()
    with Path(a.output).open('x',encoding='utf-8',newline='\n') as f:f.write(extract(a.input))
