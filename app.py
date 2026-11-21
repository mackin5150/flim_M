"""Local single-user film studio. Run with python app.py."""
import io
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps
from pydantic import BaseModel, Field, ValidationError

from core import Project, Scene, PRESETS, SCENE_PRESETS, compile_prompt, shot_lengths, uid, validate_locks
from local_provider import OpenAIPlanner, ComfyWAN
import media

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('FILM_DATA_DIR', ROOT / 'data')).resolve()
DATA.mkdir(parents=True, exist_ok=True)
MEDIA = DATA / 'media'
MEDIA.mkdir(exist_ok=True)
DB = DATA / 'studio.sqlite3'


def connection():
    db = sqlite3.connect(DB, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with connection() as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, revision INTEGER, document TEXT, updated REAL)')
        db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, project_id TEXT, scene_id TEXT, state TEXT, document TEXT, updated REAL)')
    # A create request interrupted before saving its task ID is ambiguous: never resubmit it.
    with connection() as db:
        rows = db.execute("SELECT * FROM jobs WHERE state IN ('submitting','assembling')").fetchall()
        for row in rows:
            job = json.loads(row['document'])
            job.update(state='needs_attention', error='Server stopped during submission or assembly. Check provider history before creating another take.')
            put_job(job, db)


def get_project(pid):
    with connection() as db:
        row = db.execute('SELECT document FROM projects WHERE id=?', (pid,)).fetchone()
    if not row:
        raise HTTPException(404, 'Project not found')
    return Project.model_validate_json(row['document'])


def save_project(project, old=None):
    with connection() as db:
        if old:
            validate_locks(old, project)
            previous = project.revision
            project.revision += 1
            result = db.execute('UPDATE projects SET revision=?, document=?, updated=? WHERE id=? AND revision=?',
                                (project.revision, project.model_dump_json(), time.time(), project.id, previous))
            if result.rowcount != 1:
                raise HTTPException(409, 'Project changed. Reload before saving.')
        else:
            db.execute('INSERT INTO projects VALUES (?,?,?,?)', (project.id, project.revision, project.model_dump_json(), time.time()))
    return project


def put_job(job, db=None):
    values = (job['id'], job['project_id'], job.get('scene_id', ''), job['state'], json.dumps(job), time.time())
    if db:
        db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?)', values)
    else:
        with connection() as conn:
            put_job(job, conn)


def get_job(jid):
    with connection() as db:
        row = db.execute('SELECT document FROM jobs WHERE id=?', (jid,)).fetchone()
    if not row:
        raise HTTPException(404, 'Take not found')
    return json.loads(row['document'])


def refs(project, scene):
    files = []
    for character in project.characters:
        if character.id in scene.cast:
            if not character.locked or not character.references or not character.wardrobe or not character.appearance:
                raise ValueError(f'{character.name}: add appearance, wardrobe and reference sheets, then lock the character')
            for ref in character.references:
                if not re.fullmatch(r'[a-f0-9]{32}\.jpg', ref) or not (MEDIA / ref).is_file():
                    raise ValueError('Missing character reference')
                files.append(MEDIA / ref)
    if len(files) > 9:
        raise ValueError('Maximum nine reference images per shot')
    return files


def advance(job):
    if job['kind'] == 'export':
        job['state'] = 'assembling'
        put_job(job)
        output = MEDIA / (job['id'] + '.mp4')
        items = []
        for item in job['items']:
            take = get_job(item['take_id'])
            items.append({**item, 'path': MEDIA / take['file']})
        media.assemble(items, output)
        job.update(state='complete', file=output.name)
        put_job(job)
        return
    provider = ComfyWAN()
    scene = Scene.model_validate(job['scene'])
    project = Project.model_validate(job['snapshot'])
    files = refs(project, scene)
    if len(job['shots']) > 1 and not all(s.get('direction') for s in job['shots']):
        job['state'] = 'submitting'
        put_job(job)
        directions = OpenAIPlanner().shot_directions(scene.direction, [s['duration'] for s in job['shots']]) if os.getenv('OPENAI_API_KEY') else [f'{scene.direction}\nSequential shot {i+1} of {len(job["shots"])}; render only this segment.' for i, _ in enumerate(job['shots'])]
        for shot, direction in zip(job['shots'], directions):
            shot['direction'] = direction
        job['state'] = 'queued'
        put_job(job)
        return
    pending = next((s for s in job['shots'] if s['state'] != 'complete'), None)
    if pending:
        if not pending.get('task_id'):
            job['state'] = 'submitting'
            put_job(job)
            prompt = job['prompt'] + f"\nShot {pending['index'] + 1} of {len(job['shots'])}; render only this sequential portion of the scene, progressing the action rather than repeating the entire scene. Duration {pending['duration']} seconds."
            prompt += '\nSHOT-SPECIFIC ACTION (render this beat only): ' + pending.get('direction', scene.direction)
            task_id = provider.submit(prompt, files, pending['duration'])
            pending.update(task_id=task_id, state='running')
            job['state'] = 'running'
            put_job(job)
            return
        result = provider.poll(pending['task_id'])
        if result['status'] in ('failed', 'cancelled'):
            raise ValueError('Provider task ' + result['status'] + '. Other shots and task IDs remain in history.')
        if result['status'] != 'succeeded':
            return
        file = MEDIA / (uid() + '.mp4')
        provider.download(result['video'], file)
        pending.update(state='complete', file=file.name)
        put_job(job)
        return
    job['state'] = 'assembling'
    put_job(job)
    output = MEDIA / (job['id'] + '.mp4')
    media.assemble([{'path': MEDIA / s['file'], 'end': s['duration']} for s in job['shots']], output)
    job.update(state='complete', file=output.name, review='unreviewed')
    put_job(job)


STOP = threading.Event()


def worker():
    while not STOP.is_set():
        with connection() as db:
            row = db.execute("SELECT document FROM jobs WHERE state IN ('queued','running') ORDER BY updated LIMIT 1").fetchone()
        if row:
            job = json.loads(row['document'])
            try:
                advance(job)
            except Exception as error:
                # Submission failures may have incurred a charge; no automatic POST retries.
                uncertain = job['state'] == 'submitting'
                job.update(state='needs_attention' if uncertain else 'failed', error=str(error)[:1200])
                put_job(job)
        STOP.wait(5)


@asynccontextmanager
async def lifespan(app):
    init_db()
    STOP.clear()
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    yield
    STOP.set()
    thread.join(timeout=1)


app = FastAPI(title='FLIM / M Studio', lifespan=lifespan)


@app.middleware('http')
async def local_guard(request: Request, call_next):
    # Local application: prevent DNS rebinding and cross-origin paid generation.
    host = request.url.hostname
    if host not in {'localhost', '127.0.0.1', '::1', 'testserver'}:
        return JSONResponse({'detail': 'Use localhost or an SSH port forward'}, status_code=403)
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        origin = request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            return JSONResponse({'detail': 'Cross-origin writes are blocked'}, status_code=403)
        if request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'detail': 'Cross-site writes are blocked'}, status_code=403)
    return await call_next(request)


@app.exception_handler(ValueError)
async def invalid(request, error):
    return JSONResponse({'detail': str(error)}, status_code=400)


@app.get('/api/status')
def status():
    import shutil
    return {'ai_ready': bool(os.getenv('COMFYUI_WORKFLOW') and os.getenv('COMFYUI_PROMPT_NODE')), 'planning_ready': bool(os.getenv('OPENAI_API_KEY')), 'ffmpeg': bool(shutil.which('ffmpeg') and shutil.which('ffprobe')), 'engine': 'Local WAN 2.2 via ComfyUI', 'genres': PRESETS, 'scene_types': SCENE_PRESETS}


@app.get('/api/projects')
def projects():
    with connection() as db:
        rows = db.execute('SELECT document FROM projects ORDER BY updated DESC').fetchall()
    return [json.loads(r['document']) for r in rows]


class NewProject(BaseModel):
    title: str = Field(min_length=1, max_length=120)


@app.post('/api/projects')
def create(body: NewProject):
    return save_project(Project(title=body.title))


@app.get('/api/projects/{pid}')
def read(pid: str):
    return get_project(pid)


@app.put('/api/projects/{pid}')
def update(pid: str, body: Project):
    old = get_project(pid)
    if body.id != pid:
        raise ValueError('Project ID mismatch')
    # References must be server-issued media files, not arbitrary paths.
    for char in body.characters:
        for ref in char.references:
            if not re.fullmatch(r'[a-f0-9]{32}\.jpg', ref) or not (MEDIA / ref).is_file():
                raise ValueError('Invalid reference image')
    return save_project(body, old)


@app.post('/api/references')
async def upload(request: Request):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 12 * 1024 * 1024:
            raise HTTPException(413, 'Reference image must be under 12 MB')
    try:
        image = Image.open(io.BytesIO(data))
        if image.width * image.height > 32_000_000:
            raise ValueError('Image is too large')
        image = ImageOps.exif_transpose(image).convert('RGB')
        if min(image.size) < 256 or not .4 <= image.width / image.height <= 2.5:
            raise ValueError('References must be at least 256 px on each side, with aspect ratio 0.4–2.5')
        image.thumbnail((2048, 2048))
        filename = uid() + '.jpg'
        image.save(MEDIA / filename, quality=94)
    except (OSError, Image.DecompressionBombError):
        raise ValueError('Upload a valid PNG, JPEG or WebP image')
    return {'file': filename}


@app.post('/api/projects/{pid}/plan')
def plan(pid: str):
    project = get_project(pid)
    if not project.script.strip():
        raise ValueError('Paste a script first')
    scenes = [Scene.model_validate(s) for s in OpenAIPlanner().plan(project)]
    if not scenes or len(scenes) > 30:
        raise ValueError('AI returned an invalid scene count')
    project.scenes = scenes
    Project.model_validate(project.model_dump())
    return save_project(project, get_project(pid))


class SceneRequest(BaseModel):
    scene_id: str


@app.post('/api/projects/{pid}/prompt')
def prompt(pid: str, body: SceneRequest):
    project = get_project(pid)
    scene = next((s for s in project.scenes if s.id == body.scene_id), None)
    if not scene:
        raise HTTPException(404, 'Scene not found')
    return {'prompt': compile_prompt(project, scene), 'shots': shot_lengths(scene.duration)}


@app.post('/api/projects/{pid}/generate')
def generate(pid: str, body: SceneRequest):
    if not os.getenv('COMFYUI_WORKFLOW') or not os.getenv('COMFYUI_PROMPT_NODE'):
        raise ValueError('Configure COMFYUI_WORKFLOW and COMFYUI_PROMPT_NODE before generating')
    import shutil
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise ValueError('Install FFmpeg before generating')
    project = get_project(pid)
    if not project.style_locked or not project.style.strip():
        raise ValueError('Save and lock the visual bible first')
    scene = next((s for s in project.scenes if s.id == body.scene_id), None)
    if not scene or not scene.direction.strip():
        raise ValueError('Add scene direction first')
    refs(project, scene)
    job = {'id': uid(), 'kind': 'generation', 'project_id': pid, 'scene_id': scene.id,
           'state': 'queued', 'created': time.time(), 'snapshot': project.model_dump(), 'scene': scene.model_dump(),
           'prompt': compile_prompt(project, scene), 'review': 'unreviewed',
           'shots': [{'index': i, 'duration': seconds, 'state': 'queued'} for i, seconds in enumerate([min(5, scene.duration-i) for i in range(0, scene.duration, 5)])]}
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        active = db.execute("SELECT id FROM jobs WHERE project_id=? AND scene_id=? AND state IN ('queued','running','submitting','assembling')", (pid, scene.id)).fetchone()
        if active:
            raise HTTPException(409, 'This scene already has an active generation')
        put_job(job, db)
    return job


@app.get('/api/projects/{pid}/jobs')
def jobs(pid: str):
    get_project(pid)
    with connection() as db:
        rows = db.execute('SELECT document FROM jobs WHERE project_id=? ORDER BY updated DESC', (pid,)).fetchall()
    return [json.loads(r['document']) for r in rows]


class Review(BaseModel):
    review: str = Field(pattern='^(approved|rejected|unreviewed)$')


@app.post('/api/jobs/{jid}/review')
def review(jid: str, body: Review):
    job = get_job(jid)
    if job['state'] != 'complete' or job['kind'] != 'generation':
        raise ValueError('Only completed takes can be reviewed')
    job['review'] = body.review
    put_job(job)
    return job


class Edit(BaseModel):
    take_id: str
    start: float = Field(default=0, ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    mute: bool = False


class Export(BaseModel):
    items: list[Edit] = Field(min_length=1, max_length=100)


@app.post('/api/projects/{pid}/export')
def export(pid: str, body: Export):
    get_project(pid)
    for item in body.items:
        take = get_job(item.take_id)
        if take['project_id'] != pid or take['kind'] != 'generation' or take['state'] != 'complete' or take.get('review') != 'approved':
            raise ValueError('Use completed, continuity-approved takes from this project')
        if item.start >= item.end or item.end > take['scene']['duration']:
            raise ValueError('Invalid trim range')
    job = {'id': uid(), 'kind': 'export', 'project_id': pid, 'state': 'queued', 'created': time.time(), 'items': [i.model_dump() for i in body.items]}
    put_job(job)
    return job


@app.get('/media/{filename}')
def file(filename: str):
    if not re.fullmatch(r'[a-f0-9]{32}\.(jpg|mp4)', filename) or not (MEDIA / filename).is_file():
        raise HTTPException(404)
    return FileResponse(MEDIA / filename)


@app.get('/')
def index():
    return FileResponse(ROOT / 'web' / 'index.html')


app.mount('/static', StaticFiles(directory=ROOT / 'web'), name='static')

if __name__ == '__main__':
    import uvicorn
    import argparse
    parser = argparse.ArgumentParser(description='Local FLIM / M studio')
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '7865')))
    parser.add_argument('--host', choices=['127.0.0.1', 'localhost'], default='127.0.0.1')
    parser.add_argument('--strictPort', action='store_true', help=argparse.SUPPRESS)
    options = parser.parse_args()
    uvicorn.run(app, host=options.host, port=options.port)
