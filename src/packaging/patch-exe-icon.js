'use strict';
// 写入图标及 PE 产品元数据。应用版本由显式参数传入，Electron 版本保持独立。
const fs = require('node:fs');
const path = require('node:path');
// resedit 2.0.3 为 ESM，使用原生动态 import，避免隐式依赖全局模块。
let ResEdit;

function usage() {
  console.error('用法: node patch-exe-icon.js --exe <exe> --ico <ico> --version <0.4.0> [--product-name 零汐]');
}
function args(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    const name = argv[i];
    const map = { '--exe': 'exe', '--ico': 'ico', '--version': 'version', '--product-name': 'productName' };
    if (!map[name] || !argv[i + 1]) throw Error(`未知或缺值参数：${name}`);
    const property = map[name] === 'exe' ? 'exePath' : map[name] === 'ico' ? 'icoPath' : map[name];
    out[property] = argv[++i];
  }
  if (!out.exePath || !out.icoPath || !out.version) throw Error('必须指定 --exe、--ico、--version');
  if (!/^\d+\.\d+\.\d+$/.test(out.version)) throw Error('PE 版本必须为三段数字版本，例如 0.4.0');
  if (!out.productName) out.productName = '零汐';
  return out;
}
function setVersionInfo(res, version, productName, library = ResEdit, filename = 'Lingxi.exe') {
  if (!/^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.test(version)) throw Error('应用版本必须为规范三段数字版本');
  const [major, minor, patch] = version.split('.').map(Number);
  if ([major, minor, patch].some(value => value > 65535)) throw Error('PE 版本字段不得大于 65535');
  const fileVersion = `${major}.${minor}.${patch}.0`;
  const infos = library.Resource.VersionInfo.fromEntries(res.entries);
  if (!infos.length) infos.push(library.Resource.VersionInfo.createEmpty());
  for (const info of infos) {
    let languages = info.getAllLanguagesForStringValues();
    if (!languages.length) languages = [{ lang: 1033, codepage: 1200 }];
    for (const language of languages) {
      info.setFileVersion(major, minor, patch, 0, language.lang);
      info.setProductVersion(major, minor, patch, 0, language.lang);
      info.setStringValues(language, { FileDescription: productName, ProductName: productName,
        FileVersion: fileVersion, ProductVersion: fileVersion, CompanyName: 'Lingxi',
        OriginalFilename: filename, InternalName: path.parse(filename).name });
    }
    info.outputToResourceEntries(res.entries);
  }
  return fileVersion;
}
async function patch({ exePath, icoPath, version, productName = '零汐' }) {
  if (!/^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.test(version) || version.split('.').some(value => Number(value) > 65535)) throw Error('无效的 PE 应用版本');
  ResEdit = await import('resedit');
  const raw = fs.readFileSync(exePath);
  const exe = ResEdit.NtExecutable.from(raw, { ignoreCert: true });
  const res = ResEdit.NtExecutableResource.from(exe);
  const iconFile = ResEdit.Data.IconFile.from(fs.readFileSync(icoPath));
  const groups = ResEdit.Resource.IconGroupEntry.fromEntries(res.entries);
  if (!groups.length) throw Error('PE 中没有图标组资源');
  ResEdit.Resource.IconGroupEntry.replaceIconsForResource(
    res.entries, groups[0].id, groups[0].lang, iconFile.icons.map(item => item.data),
  );
  for (const group of groups.slice(1)) {
    const index = res.entries.findIndex(e => e.type === 14 && e.id === group.id);
    if (index >= 0) res.entries.splice(index, 1);
  }
  const fileVersion = setVersionInfo(res, version, productName, ResEdit, path.basename(exePath));
  res.outputResource(exe);
  const output = Buffer.from(exe.generate());
  // 写入前重新解析输出，确认数值字段和所有语言的版本字符串确实已变更。
  const checkExe = ResEdit.NtExecutable.from(output, { ignoreCert: true });
  const checkRes = ResEdit.NtExecutableResource.from(checkExe);
  const infos = ResEdit.Resource.VersionInfo.fromEntries(checkRes.entries);
  const [major, minor, patchNumber] = version.split('.').map(Number);
  const ms = (major * 65536 + minor) >>> 0;
  const ls = (patchNumber * 65536) >>> 0;
  if (!infos.length || infos.some(info => (info.fixedInfo.fileVersionMS >>> 0) !== ms ||
      (info.fixedInfo.fileVersionLS >>> 0) !== ls || (info.fixedInfo.productVersionMS >>> 0) !== ms ||
      (info.fixedInfo.productVersionLS >>> 0) !== ls || info.getAllLanguagesForStringValues().some(language => {
        const strings = info.getStringValues(language);
        return strings.FileVersion !== fileVersion || strings.ProductVersion !== fileVersion || strings.ProductName !== productName;
      }))) throw Error('生成后的 PE 版本回读验证失败');
  fs.writeFileSync(exePath, output);
  return { exePath: path.resolve(exePath), fileVersion };
}

if (require.main === module) {
  Promise.resolve().then(() => patch(args(process.argv.slice(2)))).then(result => console.log('icon/version patched:', result))
    .catch(error => { console.error('PE 元数据更新失败:', error.message); usage(); process.exitCode = 1; });
}
module.exports = { args, patch, setVersionInfo };
