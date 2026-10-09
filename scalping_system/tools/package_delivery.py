"""Build a clean source ZIP and verify the preserved baseline hashes."""
from pathlib import Path
import zipfile
import hashlib
import json

root=Path(__file__).resolve().parents[1]
baseline=json.loads((root/'legacy/baseline_manifest.json').read_text())
for name,digest in baseline.items():
    frozen_name='README.original.md' if name=='README.md' else name
    assert hashlib.sha256((root/'legacy'/frozen_name).read_bytes()).hexdigest()==digest,name
archive=root/'legacy/baseline-v0.zip'
if not archive.exists():
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as stream:
        for name in baseline:
            frozen_name='README.original.md' if name=='README.md' else name
            stream.write(root/'legacy'/frozen_name,f'scalping_system/{name}')
destination=root.parent/'scalping_system_delivery.zip'
excluded={'.venv','.pytest_cache','__pycache__','results','generated','build','.test-tmp'}
with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED) as stream:
    for path in sorted(root.rglob('*')):
        rel=path.relative_to(root)
        if path.is_file() and not any(p in excluded or p.endswith('.egg-info') or p.startswith('.test-tmp') for p in rel.parts) and path.suffix!='.pyc':
            stream.write(path,Path('scalping_system')/rel)
print(f'{destination}: {destination.stat().st_size} bytes')
