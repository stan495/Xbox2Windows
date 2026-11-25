"""Publish a synthetic Xbox envelope, then load the source-built desktop stages."""

import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import zlib

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'tools' / 'startup'))
import mesh86_cache


def source_envelope(stage):
    # The envelope exercises cache transport only. Desktop rendering uses
    # the separate shader sources below.
    image = b'FSSM' + bytes(192)
    compressed = zlib.compress(image)
    native = b'XBCH' + struct.pack('<IQQ', 3, len(compressed), len(image)) + compressed
    program = (struct.pack('<I', (stage << 16) | 0x65) + bytes(20) +
               b'XDAT' + struct.pack('<IIIII', 1, 36, 13, 48, len(native)) + native)
    part = b'DXIL' + struct.pack('<I', len(program)) + program
    return b'DXBC' + bytes(16) + struct.pack('<IIII', 1, 36 + len(part), 1, 36) + part


def run(dxc, fixture, backend, work, depth_only):
    for suffix, stage in (('ms', 13), ('ps', 0)):
        (work / f'source.{suffix}.dxbc').write_bytes(source_envelope(stage))
    environment = dict(os.environ, X2W_MESH86_FIXTURE_DIR=str(work), X2W_SHADER_CACHE=str(work))
    environment.pop('X2W_MESH86_FIXTURE_READY', None)
    environment.pop('X2W_MESH86_FIXTURE_DEPTH', None)
    if depth_only:
        environment['X2W_MESH86_FIXTURE_DEPTH'] = '1'
    cmd = [fixture, backend, 'mesh']
    capture = subprocess.run(cmd, env=environment)
    if capture.returncode:
        return capture.returncode
    marker, = work.glob('req_*.m86')
    key = int(marker.stem.removeprefix('req_'), 16)
    source_ms, source_ps, root, stream = mesh86_cache.load_request(work, key)
    assert source_ms == source_envelope(13) and source_ps == (b'' if depth_only else source_envelope(0))
    assert root.startswith(b'DXBC') and stream[:16] == bytes(16)
    ms, ps = (work / f'desktop.{suffix}.cso' for suffix in ('ms', 'ps'))
    subprocess.run([dxc, '-T', 'ms_6_5', '-E', 'mesh', '-Fo', ms, HERE / 'startup_indirect_mesh.hlsl'], check=True)
    if not depth_only:
        subprocess.run([dxc, '-T', 'ps_6_0', '-E', 'main', '-Fo', ps, HERE / 'startup_indirect_mesh_cache.hlsl'], check=True)
    mesh86_cache.publish(work, key, ms, None if depth_only else ps)
    assert (work / f'{key:016x}.ps.cso').is_file() != depth_only
    ready = work / f'{key:016x}.mesh86.txt'
    correct = ready.read_bytes()
    ready.write_bytes(correct.replace(b'mesh86 1', b'mesh86 0', 1))
    stale = subprocess.run(cmd, env=environment)
    if stale.returncode:
        return stale.returncode
    ready.write_bytes(correct)
    # WARP removes the device on this depth-only workload despite accepting
    # PSO creation. Confirm the ready stage is guarded on software adapters.
    if not depth_only or backend == 'hardware':
        environment['X2W_MESH86_FIXTURE_READY'] = '1'
    return subprocess.run(cmd, env=environment).returncode


if __name__ == '__main__':
    for depth_only in (False, True):
        with tempfile.TemporaryDirectory(prefix='mesh86-') as directory:
            result = run(*sys.argv[1:], Path(directory), depth_only)
            if result:
                sys.exit(result)
