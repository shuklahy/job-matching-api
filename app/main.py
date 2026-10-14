import os, json, uuid, time, logging
from contextlib import asynccontextmanager
import psycopg
from redis import Redis
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from prometheus_client import Counter, Histogram, make_asgi_app

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('profile-api')
DB = os.environ['DATABASE_URL']
QUEUE = os.environ.get('REDIS_URL', 'redis://redis:6379/0')
REQ = Counter('profile_api_requests_total','API calls',['endpoint'])

def connect(): return psycopg.connect(DB)

def initialize():
    for attempt in range(30):
        try:
            with connect() as conn:
                conn.execute('CREATE TABLE IF NOT EXISTS profiles (id TEXT PRIMARY KEY, name TEXT NOT NULL, skills JSONB NOT NULL, status TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT NOW())')
                conn.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES profiles(id), status TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT NOW())')
                conn.execute('CREATE TABLE IF NOT EXISTS positions (id SERIAL PRIMARY KEY, title TEXT UNIQUE NOT NULL, skills JSONB NOT NULL)')
                for title, skills in [('Python Backend Engineer',['python','postgresql','redis','docker']),('DevOps Engineer',['docker','nginx','prometheus','grafana']),('Data Engineer',['python','sql','etl','postgresql']),('Platform Engineer',['kubernetes','docker','python','linux']),('Observability Engineer',['prometheus','grafana','elasticsearch','linux'])]:
                    conn.execute('INSERT INTO positions(title,skills) VALUES (%s,%s) ON CONFLICT(title) DO NOTHING',(title,json.dumps(skills)))
            return
        except psycopg.OperationalError:
            time.sleep(2)
    raise RuntimeError('Database not ready')

@asynccontextmanager
async def lifespan(app):
    initialize()
    yield

app = FastAPI(title='Profile Matching Demo', lifespan=lifespan)
app.mount('/metrics',make_asgi_app())

class Resume(BaseModel):
    name: str = Field(min_length=1)
    skills: list[str] = Field(min_length=1)

@app.get('/health')
def health():
    with connect() as conn: conn.execute('SELECT 1').fetchone()
    Redis.from_url(QUEUE).ping()
    return {'status':'ok'}

@app.post('/submitResume',status_code=202)
def submit_resume(resume: Resume):
    REQ.labels('submitResume').inc()
    profile_id, job_id = str(uuid.uuid4()), str(uuid.uuid4())
    skills = sorted(set(s.strip().lower() for s in resume.skills if s.strip()))
    if not skills: raise HTTPException(422,'At least one nonempty skill required')
    with connect() as conn:
        conn.execute('INSERT INTO profiles(id,name,skills,status) VALUES (%s,%s,%s,%s)',(profile_id,resume.name,json.dumps(skills),'queued'))
        conn.execute('INSERT INTO jobs(id,profile_id,status) VALUES (%s,%s,%s)',(job_id,profile_id,'queued'))
    try: Redis.from_url(QUEUE).lpush('resume_jobs',json.dumps({'job_id':job_id,'profile_id':profile_id}))
    except Exception:
        log.exception('Failed to enqueue job %s',job_id)
        with connect() as conn:
            conn.execute('UPDATE jobs SET status=%s WHERE id=%s',('enqueue_failed',job_id))
            conn.execute('UPDATE profiles SET status=%s WHERE id=%s',('enqueue_failed',profile_id))
        raise HTTPException(503,'Queue unavailable')
    log.info('Queued job=%s profile=%s',job_id,profile_id)
    return {'job_id':job_id,'profile_id':profile_id,'status':'queued'}

@app.get('/profile/{profile_id}')
def profile(profile_id: str):
    REQ.labels('profile').inc()
    with connect() as conn:
        row=conn.execute('SELECT id,name,skills,status FROM profiles WHERE id=%s',(profile_id,)).fetchone()
    if not row: raise HTTPException(404,'Profile not found')
    return dict(zip(('id','name','skills','status'),row))

@app.get('/top3Matching/{profile_id}')
def top3_matching(profile_id: str):
    REQ.labels('top3Matching').inc()
    with connect() as conn:
        row=conn.execute('SELECT skills,status FROM profiles WHERE id=%s',(profile_id,)).fetchone()
        if not row: raise HTTPException(404,'Profile not found')
        if row[1]!='processed': raise HTTPException(409,f'Profile not ready: {row[1]}')
        positions=conn.execute('SELECT title,skills FROM positions').fetchall()
    candidate=set(row[0])
    results=[{'title':title,'score':round(100*len(candidate & set(skills))/len(skills),1),'matched_skills':sorted(candidate & set(skills))} for title,skills in positions]
    return {'profile_id':profile_id,'matches':sorted(results,key=lambda r:(-r['score'],r['title']))[:3]}
