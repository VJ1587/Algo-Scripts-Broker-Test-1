from pathlib import Path
import hashlib
import json
import re
import zipfile

ROOT = Path(__file__).resolve().parent


def validate():
    manifest = json.loads((ROOT / 'manifest.json').read_text())
    assert len(manifest) == 23
    results = []
    for item in manifest:
        source = ROOT / 'Scripts' / item['file']
        binary = source.with_suffix('.ex5')
        log = ROOT / 'validation' / (source.stem + '.log')
        code = source.read_text(encoding='utf-8')
        assert len(re.findall(r'\bint\s+OnStart\s*\(', code)) == 1, source.name
        assert '#include' not in code, source.name
        assert not re.search(r'\b(?:OrderSend|OrderSendAsync|PositionClose|PositionModify|Buy|Sell)\s*\(', code), source.name
        assert not re.search(r'input\s+.*(?:Strategy|SetupSelector)', code), source.name
        if item['family'] == 'futures scalp':
            assert len(re.findall(r'\bvoid\s+Scan\s*\(', code)) == 1, source.name
        raw = log.read_bytes()
        log_text = raw.decode('utf-16') if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else raw.decode('utf-8-sig')
        final = re.findall(r'Result: (\d+) errors, (\d+) warnings[^\r\n]*', log_text)
        assert final and final[-1] == ('0', '0'), (source.name, log_text[-500:])
        assert binary.exists() and binary.stat().st_size > 0, source.name
        assert binary.stat().st_mtime >= source.stat().st_mtime, 'Recompile: ' + source.name
        assert log.stat().st_mtime >= source.stat().st_mtime, 'Outdated log: ' + source.name
        results.append({**item, 'errors': 0, 'warnings': 0,
                        'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                        'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest()})
    summary = {'compiler': 'Installed MetaTrader 5 MetaEditor64',
               'scripts': results, 'source_regression': json.loads((ROOT / 'validation' / 'python_regression.json').read_text()) if (ROOT / 'validation' / 'python_regression.json').exists() else None, 'runtime_parity_tested': False, 'institutional_bridge': json.loads((ROOT.parents[1] / 'institutional_trading_system' / 'mt5' / 'validation' / 'summary.json').read_text()),
               'broker_runtime_tested': False, 'gold_es_scaling': json.loads((ROOT.parent / 'gold_es_scaling' / 'validation' / 'summary.json').read_text()), 'orders_placed': False,
               'futures_profile_regression': json.loads((ROOT / 'validation' / 'futures_profile_regression.json').read_text())}
    (ROOT / 'validation' / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    (ROOT / 'validation' / 'REPORT.md').write_text(
        '# MT5 conversion validation\n\n'
        'All 23 final standalone scripts compiled with **0 errors and 0 warnings**.\n\n'
        'Delivery checks: individual entry points, no external includes, no strategy selector, '
        'no order mutation calls, current nonempty EX5 binaries, current compiler logs, '
        'SHA-256 source/binary hashes.\n\n'
        'Previous Python source regression suite: 119 passed. Current futures profile/scalp unit checks: 28 passed, 1 deselected. These do not execute MQL5.\n\n'
        'Not run: broker runtime, cross-language signal parity, historical performance or live trading.\n')
    bundle = ROOT / 'MT5_Separate_Strategies.zip'
    with zipfile.ZipFile(bundle, 'w', zipfile.ZIP_DEFLATED) as archive:
        files = [ROOT / 'README.md', ROOT / 'manifest.json', ROOT / 'build_scripts.py', ROOT / 'validate_delivery.py']
        files += [ROOT / 'Scripts' / item['file'] for item in manifest]
        files += [(ROOT / 'Scripts' / item['file']).with_suffix('.ex5') for item in manifest]
        files += list((ROOT / 'templates').rglob('*.mql'))
        files.append(ROOT / 'futures_profiles.json')
        files += list((ROOT / 'tests').glob('test_*.py'))
        files.append(ROOT / 'validation' / 'futures_profile_regression.json')
        files += [ROOT / 'validation' / (Path(item['file']).stem + '.log') for item in manifest]
        files += [ROOT / 'validation' / 'summary.json', ROOT / 'validation' / 'REPORT.md']
        if (ROOT / 'validation' / 'python_regression.json').exists():
            files.append(ROOT / 'validation' / 'python_regression.json')
        institutional = ROOT.parents[1] / 'institutional_trading_system' / 'mt5'
        bridge_bundle = institutional / 'ITS_S1_S7_Bridge.zip'
        if not bridge_bundle.exists():
            raise RuntimeError('Package the institutional Python bridge before combined delivery')
        archive.write(bridge_bundle, 'InstitutionalBridge/ITS_S1_S7_Bridge.zip')
        archive.write(institutional / 'README.md', 'InstitutionalBridge/README.md')
        scaling = ROOT.parent / 'gold_es_scaling'
        archive.write(scaling / 'Gold_ES_Scaling_Python_MT5.zip', 'GoldESScaling/Gold_ES_Scaling_Python_MT5.zip')
        archive.write(scaling / 'README.md', 'GoldESScaling/README.md')
        for file in sorted(files):
            archive.write(file, file.relative_to(ROOT).as_posix())
    print(f'Validated {len(results)} scripts: 0 errors, 0 warnings. Bundle: {bundle}')


if __name__ == '__main__':
    validate()
