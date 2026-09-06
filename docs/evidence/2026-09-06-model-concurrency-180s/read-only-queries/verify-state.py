import json,os,pathlib,subprocess,sqlite3,time,urllib.request
h=pathlib.Path('/root/.openclaw/miloco');c=json.loads((h/'config.json').read_text());a=c['model']['omni'];profiles=[p for p in c['model']['omni_profiles'] if p.get('label')==a.get('label')]
assert a['concurrency']==8 and a['timeout']==180 and len(profiles)==1
assert profiles[0]['concurrency']==8 and profiles[0]['timeout']==180
q="import json;from miloco.perception.window_runtime import MAX_WINDOW_AGE_SECONDS;from miloco.config.settings import MilocoSettings;s=MilocoSettings();print(json.dumps({'window_age_seconds':MAX_WINDOW_AGE_SECONDS,'engine_timeout_seconds':s.perception.engine['omni']['timeout'],'loaded_concurrency':s.model.omni.concurrency}))"
r=json.loads(subprocess.check_output(['/root/.local/share/uv/tools/miloco/bin/python','-c',q],env={**os.environ,'MILOCO_HOME':str(h)},text=True).strip().splitlines()[-1]);assert r=={'window_age_seconds':180,'engine_timeout_seconds':180,'loaded_concurrency':8}
with urllib.request.urlopen('http://127.0.0.1:1810/health',timeout=5) as response:health=response.status
assert health==200
versions={}
for tool,pkg in [('miloco','miloco'),('miloco-cli','miloco-cli')]:versions[pkg]=subprocess.check_output(['/root/.local/share/uv/tools/'+tool+'/bin/python','-c','import importlib.metadata;print(importlib.metadata.version('+repr(pkg)+'))'],text=True).strip()
assert all(v.endswith('+g96c018f5d') for v in versions.values())
db=sqlite3.connect('file:'+str(h/'observability.db')+'?mode=ro',uri=True);db.execute('PRAGMA query_only=ON')
print(json.dumps({'as_of_ms':int(time.time()*1000),'health_http_status':health,'versions':versions,'model':a['model'],'base_url':a['base_url'],'concurrency':a['concurrency'],'timeout_seconds':a['timeout'],'matching_saved_profile':True,**r,'observability_schema_marker':db.execute('PRAGMA user_version').fetchone()[0],'rollback_performed':False},indent=2))
