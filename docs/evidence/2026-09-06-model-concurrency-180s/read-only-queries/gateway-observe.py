import sqlite3,json,sys,datetime
s,e=[datetime.datetime.fromtimestamp(int(x)/1000,datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S') for x in sys.argv[1:3]]
c=sqlite3.connect('file:/var/lib/docker/volumes/grok2api_grok2api-data/_data/backend.db?mode=ro',uri=True)
c.row_factory=sqlite3.Row
c.execute('PRAGMA query_only=ON')
where="substr(created_at,1,19) BETWEEN ? AND ? AND lower(request_headers_json) LIKE '%miloco%'"
rows=[dict(r) for r in c.execute('SELECT status_code,COUNT(*) n,ROUND(AVG(duration_ms),1) mean_ms,ROUND(AVG(input_tokens),1) mean_input_tokens,ROUND(AVG(output_tokens),1) mean_output_tokens,ROUND(AVG(reasoning_tokens),1) mean_reasoning_tokens,MAX(attempt_count) max_attempts FROM request_audits WHERE '+where+' GROUP BY status_code',(s,e))]
errors=[dict(r) for r in c.execute('SELECT error_code,COUNT(*) n FROM request_audits WHERE '+where+' AND error_code IS NOT NULL GROUP BY error_code',(s,e))]
print(json.dumps({'since_utc':s,'until_utc':e,'client_filter':'Miloco user agent marker','status_groups':rows,'errors':errors},indent=2))
