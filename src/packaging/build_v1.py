"""Maintain the direct-EXE V1 layout. Run with Python 3.14 after uv sync."""
from pathlib import Path
import argparse,hashlib,json,os,shutil,subprocess,sys,tempfile,time,tomllib
SOURCE=Path(__file__).resolve().parents[1]
ROOT=SOURCE.parent
BUILD=ROOT/'build'
def run(args,cwd=SOURCE,env=None):subprocess.run([str(a) for a in args],cwd=cwd,env=env,check=True)
def node():
    value=shutil.which('node')
    if not value:raise RuntimeError('Install Node.js 22 or newer')
    return value
def npm(args,cwd):
    entry=Path(node()).parent/'node_modules/npm/bin/npm-cli.js'
    if not entry.is_file():raise RuntimeError('Node.js/npm installation missing')
    run([node(),entry,*args],cwd)

def install_directory(source,target,required=()):
    """Copy and verify first; keep the current runtime until the final swap."""
    source,target=Path(source).resolve(),Path(target).absolute()
    if not target.resolve().is_relative_to(ROOT.resolve()) or target.resolve()==ROOT.resolve():
        raise RuntimeError('Runtime target must be inside this extracted development directory')
    for part in (target,*target.parents):
        if part.is_symlink() or part.is_junction():raise RuntimeError('Runtime target contains a link')
    def inventory(folder):
        if not folder.is_dir():raise RuntimeError(f'Build output missing: {folder}')
        result={}
        for item in sorted(folder.rglob('*')):
            if item.is_symlink() or item.is_junction():raise RuntimeError(f'Linked build output: {item}')
            if item.is_file():
                with item.open('rb') as stream:result[item.relative_to(folder).as_posix()]=hashlib.file_digest(stream,'sha256').hexdigest()
        if not result:raise RuntimeError('Build output is empty')
        return result
    expected=inventory(source)
    if any(name not in expected for name in required):raise RuntimeError('Build output lacks required runtime files')
    target.parent.mkdir(parents=True,exist_ok=True)
    transaction=Path(tempfile.mkdtemp(prefix=f'.{target.name}-install-',dir=target.parent))
    staged,previous=transaction/'new',transaction/'previous'
    preserve=False
    try:
        shutil.copytree(source,staged)
        if inventory(staged)!=expected or inventory(source)!=expected:raise RuntimeError('Runtime copy hash mismatch')
        had_previous=target.exists()
        if had_previous:target.rename(previous)
        try:staged.rename(target)
        except BaseException:
            if had_previous:
                try:previous.rename(target)
                except OSError as error:
                    preserve=True
                    raise RuntimeError(f'Automatic rollback could not finish. Previous runtime retained at {previous}; restore it to {target} before launching.') from error
            raise
    finally:
        # Delete only this function's generated transaction directory, never target.
        if not preserve:
            owned=transaction.resolve()
            if owned.parent!=target.parent.resolve() or not owned.name.startswith(f'.{target.name}-install-'):
                raise RuntimeError('Unexpected transaction cleanup path')
            try:shutil.rmtree(owned)
            except OSError:print(f'Runtime transaction retained for manual cleanup: {owned}',file=sys.stderr)
def frontend(apply):
    f=SOURCE/'frontend'
    if not (f/'node_modules').is_dir():npm(['ci'],f)
    npm(['run','build'],f)
    run([node(),f/'node_modules/vite/bin/vite.js','build','--config','vite.manager.config.ts'],f)
    ui=SOURCE/'app/static/ui'
    install_directory(f/'dist',ui,('index.html',))
    w=SOURCE/'workbench'
    if not (w/'node_modules').is_dir():npm(['ci'],w)
    npm(['run','build'],w)
    install_directory(f/'dist-manager',SOURCE/'app/static/program-manager',('manager.html',))
    if apply:
        dest=ROOT/'resources/backend/_internal/app/static/ui'
        install_directory(ui,dest,('index.html',))
        install_directory(f/'dist-manager',dest.parent/'program-manager',('manager.html',))
def backend(apply):
    sys.path.insert(0,str(SOURCE/'packaging'))
    import media_dependencies,program_tools_bundle
    output=BUILD/('backend-'+time.strftime('%Y%m%d-%H%M%S'))
    output.mkdir(parents=True)
    runtime=ROOT/'resources/backend/_internal'
    tools=SOURCE/'packaging/_build/staging'/('program-tools-'+output.name)
    program_tools_bundle.prepare_tools(SOURCE,tools)
    version=tomllib.loads((SOURCE/'pyproject.toml').read_text('utf-8'))['project']['version']
    nums=tuple(map(int,version.split('.')))+(0,)
    pe=output/'version.txt'
    pe.write_text(f"VSVersionInfo(ffi=FixedFileInfo(filevers={nums!r},prodvers={nums!r},mask=0x3f,flags=0,OS=0x40004,fileType=1,subtype=0,date=(0,0)),kids=[StringFileInfo([StringTable('040904B0',[StringStruct('FileVersion','{version}.0'),StringStruct('ProductVersion','{version}.0'),StringStruct('ProductName','Lingxi')])]),VarFileInfo([VarStruct('Translation',[1033,1200])])])",encoding='utf-8')
    context={'version':version,'source':str(SOURCE),'runtime':str(runtime),'program_tools':str(tools),
        'media_bundle':str(runtime/'media-tools'),'media_arguments':media_dependencies.pyinstaller_arguments(runtime/'media-tools'),
        'pe_version_file':str(pe)}
    info=output/'context.json';info.write_text(json.dumps(context),encoding='utf-8')
    env={**os.environ,'LINGXI_BACKEND_CONTEXT':str(info),'LINGXI_DATA_DIR':str(output/'isolated-data'),
         'PYTHON_KEYRING_BACKEND':'keyring.backends.null.Keyring','PYTHONDONTWRITEBYTECODE':'1'}
    run([sys.executable,'-m','PyInstaller','--noconfirm','--distpath',output/'dist','--workpath',output/'work',SOURCE/'packaging/v1_backend.spec'],env=env)
    result=output/'dist/LingxiServer'
    # Reuse Electron's Node only after probing the actual bundled driver.
    cli=result/'_internal/playwright/driver/package/cli.js'
    run([ROOT/'Lingxi.exe',cli,'--version'],env={**env,'ELECTRON_RUN_AS_NODE':'1'})
    (result/'_internal/playwright/driver/node.exe').unlink(missing_ok=True)
    if apply:
        target=ROOT/'resources/backend'
        install_directory(result,target,('LingxiServer.exe','_internal/python314.dll','_internal/app/static/ui/index.html'))
def desktop(apply):
    if not apply:print('Desktop source: src/packaging/shell; use --apply to copy it');return
    shutil.copytree(SOURCE/'packaging/shell',ROOT/'resources/app',dirs_exist_ok=True)
    f=SOURCE/'packaging'
    if not (f/'node_modules/resedit').is_dir():npm(['ci','--ignore-scripts'],f)
    version=tomllib.loads((SOURCE/'pyproject.toml').read_text('utf-8'))['project']['version']
    run([node(),f/'patch-exe-icon.js','--exe',ROOT/'Lingxi.exe','--ico',f/'shell/assets/lingxi.ico','--version',version])
def main():
    p=argparse.ArgumentParser();p.add_argument('target',choices=['frontend','backend','desktop','all']);p.add_argument('--apply',action='store_true',help='Replace run files only after closing every Lingxi process')
    a=p.parse_args();BUILD.mkdir(exist_ok=True)
    for name,fn in [('frontend',frontend),('backend',backend),('desktop',desktop)]:
        if a.target in (name,'all'):fn(a.apply)
    print('Build complete. Run Lingxi.exe. build/ is disposable; never ship it.')
if __name__=='__main__':main()
