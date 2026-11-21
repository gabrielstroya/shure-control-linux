#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Gabriel Salas Troya and contributors
"""Opt-in real desktop singleton test. Close existing Shure Control Linux first."""
import argparse, os, subprocess, tempfile, time, json
from pathlib import Path
from gi.repository import Gio,GLib
ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--allow-desktop-test', action='store_true', required=True)
parser.parse_args()
bus=Gio.bus_get_sync(Gio.BusType.SESSION,None)
def dbus(method,signature,args,ret):
 return bus.call_sync('org.freedesktop.DBus','/org/freedesktop/DBus','org.freedesktop.DBus',method,GLib.Variant(signature,args),GLib.VariantType(ret),0,3000,None).unpack()[0]
def cpu(pid):
 parts=Path(f'/proc/{pid}/stat').read_text().split();return (int(parts[13])+int(parts[14]))/os.sysconf('SC_CLK_TCK')
assert not dbus('NameHasOwner','(s)',('io.github.shurecontrol.Linux',),'(b)'), 'Close Shure Control Linux before testing'
with tempfile.TemporaryDirectory(prefix='shure-singleton-') as td:
 env={**os.environ,'GSK_RENDERER':'cairo','XDG_CONFIG_HOME':td+'/config','XDG_STATE_HOME':td+'/state'}
 cmd=['/usr/bin/python3','-m','app.main']
 log=open(td+'/stderr','w+')
 process=subprocess.Popen(cmd+['--background'],cwd=ROOT,env=env,stdout=log,stderr=log)
 try:
  time.sleep(3)
  assert process.poll() is None
  owner=dbus('GetNameOwner','(s)',('io.github.shurecontrol.Linux',),'(s)')
  pid=dbus('GetConnectionUnixProcessID','(s)',(owner,),'(u)');assert pid==process.pid
  status=json.loads(subprocess.check_output(cmd+['--status'],cwd=ROOT,env=env,text=True,timeout=10))
  assert status['hardware']['model']=='MV7' and status['resident']
  items=bus.call_sync('org.kde.StatusNotifierWatcher','/StatusNotifierWatcher','org.freedesktop.DBus.Properties','Get',GLib.Variant('(ss)',('org.kde.StatusNotifierWatcher','RegisteredStatusNotifierItems')),GLib.VariantType('(v)'),0,3000,None).unpack()[0]
  ours=[]
  for entry in items:
   name=entry.split('@',1)[0].split('/',1)[0]
   if dbus('GetConnectionUnixProcessID','(s)',(name,),'(u)')==pid:ours.append(entry)
  assert len(ours)==1,ours
  before=cpu(pid);start=time.monotonic();time.sleep(8)
  usage=(cpu(pid)-before)/(time.monotonic()-start)*100
  rss=int(next(x.split()[1] for x in Path(f'/proc/{pid}/status').read_text().splitlines() if x.startswith('VmRSS:')))
  children=Path(f'/proc/{pid}/task/{pid}/children').read_text().split()
  captures=[x for x in children if b'parec' in Path(f'/proc/{x}/cmdline').read_bytes()]
  assert not captures
  restore=subprocess.run(cmd+['--restore-audio'],cwd=ROOT,env=env,capture_output=True,text=True,timeout=10)
  assert restore.returncode==1 and 'No hay cambios' in restore.stderr
  subprocess.run(cmd+['--show'],cwd=ROOT,env=env,check=True,timeout=10)
  assert dbus('GetNameOwner','(s)',('io.github.shurecontrol.Linux',),'(s)')==owner
  time.sleep(.5)
  rss_window=int(next(x.split()[1] for x in Path(f'/proc/{pid}/status').read_text().splitlines() if x.startswith('VmRSS:')))
  subprocess.run(cmd+['--quit'],cwd=ROOT,env=env,check=True,timeout=10)
  assert process.wait(timeout=10)==0
  assert not dbus('NameHasOwner','(s)',('io.github.shurecontrol.Linux',),'(b)')
  data={'passed':['fresh_hardware_status','single_dbus_owner','registered_in_real_gnome_host','restore_cli_uses_resident_worker_without_journal','remote_show_same_instance','complete_cli_quit','resident_has_no_capture_child'],'resident_cpu_percent_8s':round(usage,2),'resident_rss_mib':round(rss/1024,1),'gnome_visual_click_verified':False,'uid':os.getuid(),'window_rss_mib':round(rss_window/1024,1)}
  output=ROOT/'docs/evidence/resident-v02/singleton.json'
  output.parent.mkdir(parents=True,exist_ok=True)
  output.write_text(json.dumps(data,indent=2)+'\n');print(json.dumps(data))
 finally:
  if process.poll() is None:
   process.terminate();process.wait(timeout=10)
  log.seek(0);print(log.read()[-3000:])
