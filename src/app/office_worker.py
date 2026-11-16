"""Local Office automation against copies in a single temporary directory."""
import json
from pathlib import Path
import sys
import time

_PROGRAMS = {'word': 'Word.Application', 'excel': 'Excel.Application', 'powerpoint': 'PowerPoint.Application'}


def word(app, action, source, output, fields):
    app.DisplayAlerts = 0
    app.AutomationSecurity = 3
    if action == 'create':
        document = app.Documents.Add()
    else:
        document = app.Documents.Open(str(source), ConfirmConversions=False, ReadOnly=action == 'render',
                                      AddToRecentFiles=False, Visible=False)
    try:
        if action == 'create':
            title = fields.get('title', '')
            lines = ([title] if title else []) + fields.get('paragraphs', [])
            document.Content.Text = '\r'.join(lines)
            if title:
                document.Paragraphs(1).Range.Style = -2  # Word heading 1
        elif action == 'edit':
            needle, replacement = fields['find'], fields['replace']
            text_range = document.Content
            search = text_range.Find
            search.ClearFormatting()
            search.Text = needle
            search.MatchCase = True
            search.MatchWildcards = False
            search.Forward = True
            search.Wrap = 0
            if not search.Execute():
                raise ValueError('Word 文档中没有找到要替换的文字')
            text_range.Text = replacement
        if action == 'render':
            document.ExportAsFixedFormat(str(output), 17)
        else:
            document.SaveAs2(str(output), FileFormat=16, AddToRecentFiles=False)
        return {'paragraphs': document.Paragraphs.Count}
    finally:
        document.Close(0)


def excel(app, action, source, output, fields):
    app.DisplayAlerts = False
    app.AutomationSecurity = 3
    app.AskToUpdateLinks = False
    app.EnableEvents = False
    book = (app.Workbooks.Add() if action == 'create' else
            app.Workbooks.Open(str(source), UpdateLinks=0, ReadOnly=action == 'render',
                               IgnoreReadOnlyRecommended=True, AddToMru=False))
    try:
        calculations = []
        if action != 'render':
            sheet = book.Worksheets(fields.get('sheet') or 1)
            for cell in fields['cells']:
                target = sheet.Range(cell['address'])
                if cell.get('formula'):
                    target.Formula = cell['value']
                    calculations.append((cell['address'], target))
                else:
                    if isinstance(cell['value'], str):
                        target.NumberFormat = '@'
                    target.Value2 = cell['value']
            app.CalculateFull()
        if action == 'render':
            book.ExportAsFixedFormat(0, str(output))
        else:
            book.SaveAs(str(output), FileFormat=51, AddToMru=False)
        return {'sheets': book.Worksheets.Count,
                'formula_results': [{'cell': address, 'value': cell.Value2, 'text': cell.Text}
                                    for address, cell in calculations]}
    finally:
        book.Close(False)


def powerpoint(app, action, source, output, fields):
    app.AutomationSecurity = 3
    app.DisplayAlerts = 1
    presentation = (app.Presentations.Add(False) if action == 'create' else
                    app.Presentations.Open(str(source), ReadOnly=action == 'render', Untitled=False, WithWindow=False))
    try:
        if action == 'create':
            for index, slide in enumerate(fields['slides'], 1):
                page = presentation.Slides.Add(index, 2)
                page.Shapes.Title.TextFrame.TextRange.Text = slide['title']
                page.Shapes.Placeholders(2).TextFrame.TextRange.Text = slide['body'].replace('\n', '\r')
        elif action == 'edit':
            page = presentation.Slides(fields['slide'])
            if not page.Shapes.HasTitle:
                shape = page.Shapes.AddTextbox(1, 36, 24, 620, 50)
            else:
                shape = page.Shapes.Title
            shape.TextFrame.TextRange.Text = fields['title']
        if action == 'render':
            presentation.SaveAs(str(output), 32)
        else:
            presentation.SaveAs(str(output), 24)
        return {'slides': presentation.Slides.Count}
    finally:
        presentation.Saved = True
        presentation.Close()


def main():
    import pythoncom
    import win32com.client
    from pywintypes import com_error

    request = json.loads(sys.stdin.buffer.read(256_000))
    stage = Path(request['stage']).resolve(strict=True)
    source = stage/request['source'] if request['source'] else None
    output = stage/request['output']
    if any(path.name != name or path.parent != stage for path, name in
           ((output, request['output']), *(([(source, request['source'])] if source else [])))):
        raise ValueError('工作副本路径越界')
    app = None
    owned = False
    try:
        pythoncom.CoInitialize()
        name = _PROGRAMS[request['kind']]
        # Office may share a running instance, particularly PowerPoint.
        # Refuse to automate any application the user already has open.
        import subprocess
        executable = {'word': 'WINWORD.EXE', 'excel': 'EXCEL.EXE', 'powerpoint': 'POWERPNT.EXE'}[request['kind']]
        system = Path(__import__('os').environ['SystemRoot'])/'System32/tasklist.exe'
        deadline = time.monotonic() + 14
        while True:
            running = subprocess.run([str(system), '/FI', 'IMAGENAME eq '+executable, '/FO', 'CSV', '/NH'],
                capture_output=True, check=True, timeout=8, creationflags=subprocess.CREATE_NO_WINDOW).stdout
            if executable.encode().lower() not in running.lower():
                break
            if time.monotonic() >= deadline:
                raise ValueError(f'请关闭正在运行的 {request["kind"]} 应用后重试')
            time.sleep(.5)
        try:
            existing = win32com.client.GetActiveObject(name)
        except com_error:
            existing = None
        if existing is not None:
            raise ValueError('检测到正在使用的 Office 实例，请退出后重试')
        app = win32com.client.DispatchEx(name)
        collection = {'word': 'Documents', 'excel': 'Workbooks', 'powerpoint': 'Presentations'}[request['kind']]
        if getattr(app, collection).Count:
            raise ValueError('Office 正在使用其他文档，请退出后重试')
        owned = True
        operation = {'word': word, 'excel': excel, 'powerpoint': powerpoint}[request['kind']]
        result = operation(app, request['action'], source, output, request['fields'])
        reply = {'ok': True, 'result': result}
    except Exception as error:
        reply = {'ok': False, 'error': str(error)[:600]}
    finally:
        if owned:
            try:
                if request['kind'] == 'word':
                    app.Quit(0)
                else:
                    app.Quit()
            except com_error:
                pass
        app = None
        pythoncom.CoUninitialize()
    sys.stdout.buffer.write(json.dumps(reply, ensure_ascii=False, default=str).encode('utf-8'))
