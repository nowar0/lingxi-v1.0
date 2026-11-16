from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT.parent / "lingxi-ui-prototype-windows.zip"
INCLUDE = [
    "src",
    "public",
    "dist",
    "screenshots",
    "scripts",
    "index.html",
    "package.json",
    "package-lock.json",
    "tsconfig.json",
    "vite.config.ts",
    "components.json",
    ".env.example",
    ".gitignore",
]

with ZipFile(OUTPUT, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
    for name in INCLUDE:
        source = ROOT / name
        if source.is_dir():
            for file in sorted(source.rglob("*")):
                if file.is_file() and "__pycache__" not in file.parts:
                    archive.write(file, Path("lingxi-ui-prototype") / file.relative_to(ROOT))
        elif source.is_file():
            archive.write(source, Path("lingxi-ui-prototype") / name)

with ZipFile(OUTPUT) as archive:
    failed = archive.testzip()
    if failed:
        raise SystemExit(f"ZIP 完整性检查失败：{failed}")
    print(f"OK {OUTPUT}")
    print(f"FILES {len(archive.namelist())}")
    print(f"SIZE {OUTPUT.stat().st_size}")
