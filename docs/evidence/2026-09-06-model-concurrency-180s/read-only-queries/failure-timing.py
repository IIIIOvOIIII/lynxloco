import json,sqlite3,sys,collections
c=sqlite3.connect('file:/root/.openclaw/miloco/observability.db?mode=ro',uri=True);c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON')
s,e=map(int,sys.argv[1:3]);out={}
rows=c.execute("SELECT cycle_total_ms,in_delay_ms,timing_detail FROM traces WHERE timestamp BETWEEN ? AND ? AND timestamp+COALESCE(cycle_total_ms,0)<=? AND metric_version>=2 AND omni_request_count>0 AND omni_request_error_count>0",(s,e,e)).fetchall()
v=collections.defaultdict(list)
for r in rows:
 for k in ['cycle_total_ms','in_delay_ms']:
  if isinstance(r[k],(int,float)):v[k].append(r[k])
 for k,n in json.loads(r['timing_detail'] or '{}').items():
  if not isinstance(n,(int,float)):continue
  for prefix,name in [('http_','http_ms'),('slot_wait_','slot_wait_ms'),('reorder_wait_','reorder_wait_ms')]:
   if '/'+prefix in k and k.endswith('_ms'):v[name].append(n)
def pct(a,p):
 a=sorted(a);i=(len(a)-1)*p;j=int(i);return round(a[j]+(a[min(j+1,len(a)-1)]-a[j])*(i-j),2)
print(json.dumps({'failed_request_windows':len(rows),'stage_ms':{k:{'n':len(a),'p50':pct(a,.5),'p95':pct(a,.95),'mean':round(sum(a)/len(a),2)} for k,a in v.items()}},indent=2))
