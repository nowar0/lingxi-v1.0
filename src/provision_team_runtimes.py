"""Build explicitly requested, independently pinned SDK environments with uv."""
from pathlib import Path
import subprocess
import os

ROOT = Path(__file__).resolve().parent
for engine, directory in [('agno', 'integration-env'), ('agentscope', 'agentscope-env'), ('camel', 'camel-env')]:
    runtime = ROOT.parent / directory
    subprocess.run(['uv', 'venv', str(runtime), '--python', '3.14'], check=True)
    subprocess.run(['uv', 'pip', 'sync', '--python', str(runtime / 'Scripts/python.exe'),
                    str(ROOT / f'requirements-{engine}.lock')], check=True)
    if engine == 'camel':
        # Upstream 0.2.91a7 creates /tmp at import time, which is invalid on
        # portable Windows. Keep this one deterministic compatibility patch.
        path = runtime / 'Lib/site-packages/camel/toolkits/earth_science_toolkit.py'
        text = path.read_text(encoding='utf-8')
        text = text.replace("TEMP_DIR = Path('/tmp/earth_science')",
                            "import tempfile\nTEMP_DIR = Path(tempfile.gettempdir()) / 'lingxi-earth-science'")
        path.write_text(text, encoding='utf-8')
        env = dict(os.environ, TIKTOKEN_CACHE_DIR=str(runtime / 'tiktoken-cache'))
        subprocess.run([str(runtime / 'Scripts/python.exe'), '-c',
                        "import tiktoken; tiktoken.get_encoding('o200k_base'); tiktoken.get_encoding('cl100k_base')"],
                       env=env, check=True)
