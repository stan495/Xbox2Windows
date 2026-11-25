"""Refusals on two integer operations that the translator cannot model.

Both raises are reached before any register is written: ``v_cvt_i32_f32`` refuses an
output modifier (a clamp or a mul would change the value it converts) and
``v_readfirstlane_b32`` refuses a source it cannot prove uniform. Neither was exercised
by a check, so replacing either raise with ``pass`` left the gfx2hlsl checks green.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tools', 'startup'))
import gfx2hlsl  # noqa: E402


def stage(**state):
    value = gfx2hlsl.Stage.__new__(gfx2hlsl.Stage)
    value.sym = {}
    value.used_s = set()
    value.used_v = set()
    value.uniform_v = set()
    value.exec_known = True
    value.has_branches = False
    for key, item in state.items():
        setattr(value, key, item)
    return value


def refused(call, needle):
    try:
        call()
    except gfx2hlsl.Unsupported as error:
        assert needle in str(error), error
        return
    raise AssertionError('accepted: %s' % (needle,))


def main():
    # A clamp on the integer conversion is refused rather than dropped.
    refused(lambda: stage().op_v_cvt_i32_f32(['v1', 'v2'], ['clamp']), 'v_cvt_i32_f32 with modifiers')
    # A read-first-lane needs a proven uniform source: an empty uniform set is not one.
    refused(lambda: stage().op_v_readfirstlane_b32(['s1', 'v2'], []),
            'v_readfirstlane_b32 requires a proven uniform source')
    # A modifier on the read-first-lane is refused for the same reason as on the conversion.
    refused(lambda: stage(uniform_v={2}).op_v_readfirstlane_b32(['s1', 'v2'], ['clamp']),
            'v_readfirstlane_b32 requires a proven uniform source')
    print('gfx2hlsl op refusals: 3 cases passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())