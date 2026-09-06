import datetime
import json
import pathlib
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request

home = pathlib.Path('/root/.openclaw/miloco')
now = int(time.time() * 1000)
since = int(sys.argv[1])
until = min(int(sys.argv[2]), now) if len(sys.argv) > 2 else now
cfg = json.loads((home / 'config.json').read_text())
conn = sqlite3.connect(f'file:{home}/observability.db?mode=ro', uri=True, timeout=3)
conn.row_factory = sqlite3.Row
conn.execute('PRAGMA query_only=ON')
conn.execute('BEGIN')


def query(sql, args=()):
    return [dict(row) for row in conn.execute(sql, args)]


def percentile(values, fraction):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    index = (len(values) - 1) * fraction
    lower = int(index)
    return round(values[lower] + (values[min(lower + 1, len(values) - 1)] - values[lower]) * (index - lower), 2)


where = "timestamp BETWEEN ? AND ? AND timestamp + COALESCE(cycle_total_ms,0) <= ? AND metric_version >= 2"
args = (since, until, until)
totals = query(
    "SELECT COUNT(*) cycles, SUM(device_count) camera_windows, SUM(dropped_windows_total) dropped_windows, "
    "SUM(partial_windows_total) partial_windows, SUM(skipped) gate_skipped, SUM(omni_request_count) requests, "
    "SUM(omni_request_error_count) request_errors, "
    "SUM(CASE WHEN omni_call_count>0 AND omni_error_count=0 AND COALESCE(cycle_error_msg,'')='' AND skipped=0 THEN device_count ELSE 0 END) success_windows, "
    "SUM(CASE WHEN omni_request_count>0 AND (omni_request_error_count>0 OR COALESCE(cycle_error_msg,'')!='') THEN 1 ELSE 0 END) problem_request_windows, "
    "SUM(CASE WHEN COALESCE(cycle_error_msg,'')!='' THEN 1 ELSE 0 END) window_gaps "
    "FROM traces WHERE " + where, args,
)[0]
durations = query("SELECT cycle_total_ms, in_delay_ms, omni_wall_ms, timing_detail FROM traces WHERE " + where + " AND omni_call_count>0 AND omni_error_count=0 AND skipped=0 AND COALESCE(cycle_error_msg,'')=''", args)
stages = {'http_ms': [], 'slot_wait_ms': [], 'reorder_wait_ms': []}
for row in durations:
    for key, value in json.loads(row['timing_detail'] or '{}').items():
        if not isinstance(value, (int, float)):
            continue
        for prefix, name in [('http_', 'http_ms'), ('slot_wait_', 'slot_wait_ms'), ('reorder_wait_', 'reorder_wait_ms')]:
            if '/' + prefix in key and key.endswith('_ms'):
                stages[name].append(value)
out = {
    'as_of_ms': now, 'since_ms': since, 'until_ms': until,
    'scope': 'completed new-definition traces started in fixed window; in-flight excluded',
    'model': cfg.get('model', {}).get('omni', {}).get('model'),
    'configured_concurrency': cfg.get('model', {}).get('omni', {}).get('concurrency', 1),
    'totals': {key: value or 0 for key, value in totals.items()},
    'success_windows_per_minute': round((totals['success_windows'] or 0) / max((until-since)/60000, .001), 3),
    'problem_request_rate': round((totals['problem_request_windows'] or 0) / max(totals['requests'] or 0, 1), 4),
    'success_latency': {key: {'p50': percentile([r[key] for r in durations], .5), 'p95': percentile([r[key] for r in durations], .95)} for key in ['cycle_total_ms','in_delay_ms','omni_wall_ms']},
    'stage_latency': {key: {'n': len(values), 'p50': percentile(values,.5), 'p95': percentile(values,.95)} for key,values in stages.items()},
}
out['cameras'] = query(
    "SELECT d.room_name,COUNT(*) windows,SUM(d.video_frame_count=0) zero_video_windows,AVG(d.video_frame_count) mean_video_frames,MAX(d.video_frame_count) max_video_frames "
    "FROM traces_device d JOIN traces t ON d.cycle_id=t.trace_id WHERE t.timestamp BETWEEN ? AND ? "
    "AND t.timestamp+COALESCE(t.cycle_total_ms,0)<=? AND t.metric_version>=2 GROUP BY d.device_id", args,
)
out['errors'] = query(
    "SELECT d.omni_error_code,COUNT(*) n FROM traces_device d JOIN traces t ON d.cycle_id=t.trace_id "
    "WHERE t.timestamp BETWEEN ? AND ? AND t.timestamp+COALESCE(t.cycle_total_ms,0)<=? AND t.metric_version>=2 AND d.omni_error_code IS NOT NULL GROUP BY d.omni_error_code", (since,until,until),
)
out['agent'] = query("SELECT source,success,COUNT(*) n FROM agent_runs WHERE timestamp BETWEEN ? AND ? GROUP BY source,success",(since,until))
out['events'] = query("SELECT event_type,COUNT(*) n FROM events WHERE timestamp BETWEEN ? AND ? GROUP BY event_type",(since,until))
out['input_buffer'] = query("SELECT MAX(d.max_buffer_depth) max_buffer_depth,SUM(d.overflow_count) overflow_events FROM traces_device d JOIN traces t ON d.cycle_id=t.trace_id WHERE t.timestamp BETWEEN ? AND ? AND t.metric_version>=2",(since,until))
conn.close()
try:
    with urllib.request.urlopen('http://127.0.0.1:1810/health',timeout=3) as response:
        out['health_http_status'] = response.status
except Exception as exc:
    out['health_error'] = type(exc).__name__
for line in pathlib.Path('/proc/meminfo').read_text().splitlines():
    if line.startswith('MemAvailable:'):
        out['available_memory_mb'] = int(line.split()[1]) // 1024
out['memory_pressure'] = pathlib.Path('/proc/pressure/memory').read_text().strip()
pid = subprocess.check_output(['/root/.local/bin/supervisorctl','-c',str(home/'supervisord.conf'),'pid','miloco-backend'],text=True).strip()
if pid.isdigit() and int(pid)>0:
    out['pid'] = int(pid)
    for line in pathlib.Path('/proc',pid,'status').read_text().splitlines():
        if line.startswith('VmRSS:'):
            out['backend_rss_mb'] = int(line.split()[1])//1024
lines = subprocess.check_output(['tail','-n','20000',str(home/'log/miloco-backend.log')],text=True).splitlines()
peaks = []
for line in lines:
    if '[omni-concurrency]' not in line:
        continue
    try:
        ts = datetime.datetime.strptime(line[:19],'%Y-%m-%d %H:%M:%S').replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8))).timestamp()*1000
    except ValueError:
        continue
    if since-120000 <= ts <= until:
        fields = {k:int(v) for k,v in re.findall(r'\b(limit|peak|active|waiting|bytes|submitted|completed)=([0-9]+)',line)}
        if fields and fields.get('limit') == out['configured_concurrency']:
            peaks.append(fields)
out['concurrency_peak_log'] = peaks[-20:]
print(json.dumps(out,ensure_ascii=False,indent=2))
