import sqlite3,json,sys
c=sqlite3.connect('file:/root/.openclaw/miloco/observability.db?mode=ro',uri=True);c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON')
s,e=map(int,sys.argv[1:3])
q="""SELECT d.room_name,CASE WHEN d.video_frame_count=0 THEN 'no_video' ELSE 'with_video' END input_group,COUNT(*) windows,SUM(t.omni_request_count) requests,SUM(t.omni_request_error_count) request_errors,ROUND(AVG(CASE WHEN t.omni_request_count>0 THEN t.omni_wall_ms END),1) mean_omni_ms,ROUND(AVG(d.video_frame_count),2) mean_video_frames FROM traces_device d JOIN traces t ON d.cycle_id=t.trace_id WHERE t.timestamp BETWEEN ? AND ? AND t.timestamp+COALESCE(t.cycle_total_ms,0)<=? AND t.metric_version>=2 GROUP BY d.device_id,input_group"""
print(json.dumps({'groups':[dict(r) for r in c.execute(q,(s,e,e))]},ensure_ascii=False,indent=2))
