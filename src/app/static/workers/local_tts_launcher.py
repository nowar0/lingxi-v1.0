"""Start the external TTS package with consistent CPU fallback for ONNX G2PW."""
from pathlib import Path
import os
import runpy
import sys

if sys.platform == 'win32':
    # External Python must not inherit the frozen app's bundled DLL directory.
    import ctypes
    ctypes.windll.kernel32.SetDllDirectoryW(None)
root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
import torch
import onnxruntime

# The supplied torch build's fused attention kernel rejects this machine's GPU
# architecture. Math SDPA preserves behavior and avoids that invalid kernel.
torch.backends.cuda.enable_flash_sdp(False)
torch.backends.cuda.enable_mem_efficient_sdp(False)
torch.backends.cuda.enable_math_sdp(True)
if os.environ.get('LINGXI_TTS_DEVICE') == 'cuda':
    # This external torch build's NVRTC does not accept the current GPU's arch.
    for option in ('_jit_set_texpr_fuser_enabled', '_jit_override_can_fuse_on_gpu',
                   '_jit_set_profiling_executor', '_jit_set_profiling_mode'):
        configure = getattr(torch._C, option, None)
        if configure is not None:
            configure(False)

# Some bundled G2PW versions inspect compiled providers, which can advertise CUDA
# even when the current Python runtime cannot use a CUDA device.
# Keep phoneme conversion on CPU even when acoustic synthesis uses CUDA. The
# supplied ONNX GPU attention kernel is incompatible with the current GPU.
onnxruntime.get_available_providers = lambda: ['CPUExecutionProvider']
sys.argv = [str(root / 'api_v2.py'), *sys.argv[2:]]
runpy.run_path(str(root / 'api_v2.py'), run_name='__main__')
