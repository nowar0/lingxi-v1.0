"""Persistent noninteractive PowerShell, bounded output and owned process cleanup.

This is a command terminal, not a PTY or an operating-system sandbox.
"""
import base64
import codecs
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
from threading import Event, RLock, Thread
import time
from uuid import uuid4

OUTPUT_LIMIT=1_000_000


class ProcessJob:
    def __init__(self, process):
        class Basic(ctypes.Structure):
            _fields_=[('process_time',ctypes.c_longlong),('job_time',ctypes.c_longlong),('flags',wintypes.DWORD),
                      ('min_working',ctypes.c_size_t),('max_working',ctypes.c_size_t),('active_limit',wintypes.DWORD),
                      ('affinity',ctypes.c_size_t),('priority',wintypes.DWORD),('scheduling',wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_=[(name,ctypes.c_ulonglong) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
        class Extended(ctypes.Structure):
            _fields_=[('basic',Basic),('io',IO),('process_memory',ctypes.c_size_t),('job_memory',ctypes.c_size_t),('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
        self.api=ctypes.WinDLL('kernel32',use_last_error=True)
        self.api.CreateJobObjectW.argtypes=[ctypes.c_void_p,wintypes.LPCWSTR];self.api.CreateJobObjectW.restype=wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD];self.api.SetInformationJobObject.restype=wintypes.BOOL
        self.api.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE];self.api.AssignProcessToJobObject.restype=wintypes.BOOL
        self.api.CloseHandle.argtypes=[wintypes.HANDLE];self.api.CloseHandle.restype=wintypes.BOOL
        self.api.QueryInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD,ctypes.c_void_p];self.api.QueryInformationJobObject.restype=wintypes.BOOL
        self.handle=self.api.CreateJobObjectW(None,None)
        try:
            if not self.handle:raise ctypes.WinError(ctypes.get_last_error())
            info=Extended();info.basic.flags=0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.api.SetInformationJobObject(self.handle,9,ctypes.byref(info),ctypes.sizeof(info)):raise ctypes.WinError(ctypes.get_last_error())
            if not self.api.AssignProcessToJobObject(self.handle,wintypes.HANDLE(int(process._handle))):raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close();raise
    def close(self):
        if self.handle:
            handle,self.handle=self.handle,None
            self.api.CloseHandle(handle)
    def active_count(self):
        class Accounting(ctypes.Structure):
            _fields_=[(n,ctypes.c_longlong) for n in ('user','kernel','period_user','period_kernel')]+[(n,wintypes.DWORD) for n in ('faults','total','active','terminated')]
        value=Accounting()
        if self.handle and self.api.QueryInformationJobObject(self.handle,1,ctypes.byref(value),ctypes.sizeof(value),None):return value.active
        return 0


def encoded(text):
    return base64.b64encode(text.encode('utf-8')).decode('ascii')


class Shell:
    def __init__(self,cwd,revision,on_finish):
        self.lock=RLock();self.ready=Event();self.finished=Event()
        self.generation=uuid4().hex;self.sequence=0;self.cwd=cwd;self.root=cwd;self.workspace_revision=revision
        self.active=None;self.output='';self.limited=False;self.closed=False;self.last_used=time.monotonic();self.on_finish=on_finish
        self.marker='__LINGXI_'+uuid4().hex+'__:'
        exe=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
        self.process=subprocess.Popen([str(exe),'-NoLogo','-NoProfile','-NonInteractive','-Command','-'],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,cwd=cwd,creationflags=subprocess.CREATE_NO_WINDOW,bufsize=0)
        self.job=None
        try:
            self.job=ProcessJob(self.process)
            self.reader=Thread(target=self._read,name='lingxi-powershell-output',daemon=True);self.reader.start()
            self._write("[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false); $OutputEncoding = [Console]::OutputEncoding; $ProgressPreference = 'SilentlyContinue'; [Console]::WriteLine('"+self.marker+"ready')")
            if not self.ready.wait(8):raise RuntimeError('PowerShell启动未确认')
        except BaseException:
            self.close();raise
    def _write(self,line):
        self.process.stdin.write((line+'\n').encode('ascii'));self.process.stdin.flush()
    def _append(self,text):
        with self.lock:
            if self.active:
                self.output+=text
                if len(self.output)>OUTPUT_LIMIT:self.output=self.output[-OUTPUT_LIMIT:];self.limited=True
    def _line(self,line):
        if line.rstrip('\r\n')==self.marker+'ready':self.ready.set();return
        if line.startswith(self.marker):
            try: result=json.loads(base64.b64decode(line[len(self.marker):].strip()).decode('utf-8'))
            except (ValueError,UnicodeError):self._append(line);return
            with self.lock:
                if not self.active or result.get('request')!=self.active:return
                token=self.active;self.active=None;self.cwd=result['cwd'];self.sequence+=1;self.last_used=time.monotonic()
                result={'cwd':self.cwd,'exit_code':result['exit_code'],'output':self.output,'limited':self.limited}
            self.on_finish(token,'complete',result);self.finished.set();return
        self._append(line)
    def _read(self):
        decoder=codecs.getincrementaldecoder('utf-8')(errors='replace');pending=''
        try:
            while True:
                raw=os.read(self.process.stdout.fileno(),8192)
                if not raw:break
                pending+=decoder.decode(raw)
                while '\n' in pending:
                    line,pending=pending.split('\n',1);self._line(line+'\n')
                if len(pending)>16384:self._append(pending[:-1024]);pending=pending[-1024:]
            if pending:self._append(pending)
        except OSError:
            pass
        finally:
            with self.lock:
                token=self.active;self.active=None
                result={'cwd':self.cwd,'exit_code':None,'output':self.output,'limited':self.limited}
            if token:self.on_finish(token,'unknown',result)
            self.finished.set()
    def run(self,token,command):
        with self.lock:
            if self.active or self.closed or self.process.poll() is not None:raise RuntimeError('终端不可用或仍在执行')
            self.active=token;self.output='';self.limited=False;self.finished.clear();self.last_used=time.monotonic()
            # The exact approved command is decoded as data, then dot-sourced
            # into this shell. No command text is interpolated into its wrapper.
            code=("$global:LASTEXITCODE=0; $__lingxi_ok=$true; try { . ([scriptblock]::Create([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"+encoded(command)+"')))); $__lingxi_ok=$? } catch { $__lingxi_ok=$false; Write-Output ($_ | Out-String) }; "
                  "$__lingxi_code=0; if ($LASTEXITCODE) { $__lingxi_code=$LASTEXITCODE } elseif (-not $__lingxi_ok) { $__lingxi_code=1 }; "
                  "$__lingxi_result=@{request='"+token+"';cwd=(Get-Location).Path;exit_code=$__lingxi_code} | ConvertTo-Json -Compress; "
                  "[Console]::WriteLine(); [Console]::WriteLine('"+self.marker+"'+[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($__lingxi_result)))")
            self._write(code)
    def snapshot(self):
        with self.lock:return {'generation':self.generation,'sequence':self.sequence,'cwd':self.cwd,'active':self.active,'output':self.output,'limited':self.limited,'background':bool(self.job and self.job.active_count()>1),'alive':not self.closed and self.process.poll() is None}
    def close(self):
        with self.lock:
            if self.closed:return
            self.closed=True;token=self.active;self.active=None
            result={'cwd':self.cwd,'exit_code':None,'output':self.output,'limited':self.limited}
        if self.job:self.job.close()
        if self.process.poll() is None:self.process.kill()
        try:self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:pass
        if token:self.on_finish(token,'stopped',result)
        self.finished.set()
        for pipe in (self.process.stdin,self.process.stdout):
            try:pipe.close()
            except OSError:pass
