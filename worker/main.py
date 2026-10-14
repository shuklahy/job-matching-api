import os, json, time, logging
import psycopg
from redis import Redis
from prometheus_client import Counter, start_http_server

logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
log=logging.getLogger('profile-worker')
processed=Counter('profile_worker_jobs_processed_total','Processed jobs')
failed=Counter('profile_worker_jobs_failed_total','Failed jobs')

start_http_server(9101)
r=Redis.from_url(os.environ['REDIS_URL'])

while True:
    try:
        item=r.brpop('resume_jobs',timeout=5)
        if not item: continue
        job=json.loads(item[1]); job_id=job['job_id']; profile_id=job['profile_id']
        with psycopg.connect(os.environ['DATABASE_URL']) as conn:
            conn.execute('UPDATE jobs SET status=%s WHERE id=%s',('processing',job_id))
            conn.execute('UPDATE profiles SET status=%s WHERE id=%s',('processing',profile_id))
        time.sleep(2)
        with psycopg.connect(os.environ['DATABASE_URL']) as conn:
            conn.execute('UPDATE jobs SET status=%s WHERE id=%s',('processed',job_id))
            conn.execute('UPDATE profiles SET status=%s WHERE id=%s',('processed',profile_id))
        processed.inc()
        log.info('Processed job=%s profile=%s',job_id,profile_id)
    
    except Exception:
        failed.inc()
        log.exception('Worker processing failed')
        time.sleep(2)
