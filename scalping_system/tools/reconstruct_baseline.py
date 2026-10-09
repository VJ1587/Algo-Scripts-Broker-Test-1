"""Reconstruct the 13 Appendix B members into a new directory, verifying hashes."""
from pathlib import Path
import argparse
import hashlib
import re

def reconstruct(markdown,output):
    source=Path(markdown).read_text(encoding='utf-8')
    root=Path(output).resolve()
    matches=re.findall(r'<!-- BEGIN_FILE path=(\S+) bytes=(\d+) sha256=([0-9a-f]{64}) -->\n(.*?)<!-- END_FILE -->',source,re.S)
    if len(matches)!=13: raise ValueError(f'Expected 13 members; found {len(matches)}')
    verified=[]
    for relative,length,digest,block in matches:
        if block.startswith('````'):
            block=block.split('\n',1)[1]
        data=block.encode('utf-8')[:int(length)]
        target=(root/relative).resolve()
        if root not in target.parents or target.exists(): raise ValueError(f'Unsafe/existing destination: {target}')
        if len(data)!=int(length) or hashlib.sha256(data).hexdigest()!=digest: raise ValueError(f'Invalid hash: {relative}')
        verified.append((target,data))
    for path,data in verified:
        path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(data)
    return len(verified)

if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('markdown'); parser.add_argument('output'); args=parser.parse_args()
    print(f'Reconstructed {reconstruct(args.markdown,args.output)} hash-verified baseline members.')
